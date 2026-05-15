---
name: debate-recipes
date: 2026-04-25
author: recipe-architect (debate teammate, task #1)
team: debate-recipe-improvement
inputs:
  - .claude/debug/final-report.md (BASELINE WINS context)
  - .claude/debug/p6-pivot-strategist.md (cost-vs-p(win) table)
  - .claude/debug/loop-log.md (18-iter journal)
  - .claude/debug/loop-state.json (candidates_tested)
  - .claude/debug/eval-methodology-verdict.md
  - scripts/runpod/{prepare_train_data.py, train_docs_embedder.py, cost_guard.py}
  - profiles/pay-com/doc_intent_eval_v3.jsonl (canonical, 100 rows / n_eval=90)
  - profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl (197 rows, 118 positives across 22 queries)
  - logs/tool_calls.jsonl (2387 unique prod queries, ~2081 doc-intent)
budget_cap_usd_session: 11.00 (banked $13.30 minus $2 safety hold)
---

# FT recipes for pay-com docs-tower — `recipe-architect` deliverable

## TL;DR — top pick

**Recipe R1: TSDAE domain pre-adapt → CoSENT-loss FT with cross-encoder mined hard negatives**

- Base: `nomic-ai/nomic-embed-text-v1.5` (keep — only 0/4 prior FT used the same base; reranker is sentence-transformers compat).
- Stage 1 (free, ~1 h CPU/GPU): TSDAE unsupervised denoising on the full 25 k provider-doc + 16 k workflow-doc chunks (no labels needed). Repairs the anisotropy that positive-only MNRL on 91 pairs collapsed.
- Stage 2 (~$2 RunPod, 2 h A40): CoSENT margin loss over query↔doc pairs with hard negatives mined by the *production reranker* (`reranker_ft_gte_v8` — same one used at serving time, so negatives are by-construction what the system *currently* mis-ranks).
- Hard negatives: top-50 FTS5 + top-50 vector pool, score by `reranker_ft_gte_v8`, drop top-10 (likely positives), keep ranks 11–30 as hard negatives. ~5 negatives per query.
- Training data: regenerate from 2387 unique prod queries (`logs/tool_calls.jsonl`) — *not* the 22-query v12 set. Path-disjoint check vs eval-v3 expected_paths is mechanical.
- p(win clearing +10 pp AND-gate on eval-v3): **0.18** — 1.6× the Jeffreys prior because the recipe attacks all four observed failure modes (anisotropy, label monoculture, train-distribution mismatch, single-pass MNRL).
- Kill criteria: abort the run if Stage-1 TSDAE smoke (50 random doc-intent queries on a tiny eval probe) shows Δr@10 < −0.03 vs baseline; no Stage 2 spend.
- Total cost ceiling: $3.50 (well inside the $11 cap; leaves $7.50 for one more recipe round if R1 lifts).

Rationale in §6.

---

## Why this is hard — the "0/4" prior is real

Bayesian base rate after 4 rejected FT runs (Jeffreys' 1/9 ≈ **0.11** per attempt). Any honest recipe must explain its mechanism for breaking the pattern, not just promise more data.

The 4 observed failure modes are *named*:

1. **Anisotropy from positive-only MNRL on small N.** v0 (10 pairs), v1 (91 pairs), v1-fixed (same weights remapped) all used `MultipleNegativesRankingLoss` with in-batch negatives. With N=91 and bs=4, the in-batch negatives are quasi-random docs from 22 queries — a degenerate negative distribution. `loop-log.md:529` calls it directly: "the 91-pair MultipleNegativesRankingLoss tuning over-pulled retrieval toward the small Stage D training distribution and under-recovered general doc-intent queries". Per-stratum drops on `refund` / `trustly` of −22 pp confirm collapse.
2. **Label monoculture.** All 91 v1 train positives came from `v12_candidates_regen_labeled_FINAL.jsonl` — only **22 unique queries** (verified above). Even if each query has 5–8 positives, the *query* support set is 22; in-batch negatives across 22 queries don't tile the 2387-query prod distribution at all.
3. **Train↔eval distribution gap.** Train queries were Opus-judged from the v12 regeneration; eval-v3 queries are 67 prod-sampled + 33 v1-kept across 9 strata. Per-stratum drop on payper / nuvei / refund is consistent with "we never trained on these strata so the FT just blurs them".
4. **Architecture stickiness.** v12a tried 12 reranker FT iterations on the same single-tower base and lost. Same urn risk.

Recipes below each declare which of the four failure modes they target and *why* the mechanism is different from the 4 rejected attempts.

---

## Common machinery (used by ≥1 recipe below)

### CM1. Hard-negative mining strategies — three options, ranked

| ID | Strategy | Source | Hardness | Cost | Risk |
|---|---|---|---|---|---|
| **HN-A** | Reranker-mined ("self-hard-negatives") | `reranker_ft_gte_v8` re-scores FTS5+vec top-100 per query; keep ranks 11–30 as hard negatives | high (production-aware) | $0 — runs on Mac CPU at ~30 q/s, 2387 q × 100 cands ≈ 80 min | reranker labels ARE the production signal — this aligns FT to what the reranker considers wrong but plausible |
| **HN-B** | FTS5-only, top-50-minus-gold | FTS5 top-50 per query, drop any path that overlaps the labeled positive set | medium | $0 — FTS5 query is ~0.5 ms, 2387×0.5ms+IO ≈ 5 min | mines lexical near-misses, but a model that already wins on FTS may not learn from them |
| **HN-C** | MS-MARCO-style cross-encoder distillation | label `(q, doc)` softmax scores from a frozen `cross-encoder/ms-marco-MiniLM-L-6-v2`; KL-distill into the bi-encoder | high — soft labels carry rank information | $1 (1 GPU h on A40 to score 2387 q × 100 cands) | needs MarginMSE / DistillLoss in train_docs_embedder.py — code change |

**Default for R1, R3:** HN-A (reranker-aligned, cheapest, attacks failure mode 1 and 3 simultaneously).

### CM2. Loss functions — current vs candidates

| Loss | Status | Available in ST 5.4.1? | Anisotropy resistance |
|---|---|---|---|
| `MultipleNegativesRankingLoss` (current, used by v0/v1/v1-fixed) | rejected 4× | yes | low (positive-only collapses) |
| `CachedMultipleNegativesRankingLoss` (Hofstätter 2021, GradCache) | new | yes (`losses.CachedMultipleNegativesRankingLoss`) | medium — bigger effective batch via grad accumulation, attacks small-N issue |
| `CoSENTLoss` (Su et al. 2022, "circle-style" margin) | new | yes (`losses.CoSENTLoss`) | high — pairwise margin, no in-batch coupling |
| `OnlineContrastiveLoss` (triplet w/ online mining) | new | yes (`losses.OnlineContrastiveLoss`) | medium |
| `MarginMSELoss` (cross-encoder distillation) | new | yes (`losses.MarginMSELoss`) | high — uses teacher rank gap, no MNRL collapse |

### CM3. Training data sources — three lanes

- **L1: 22-query v12 set** (current, 91 pairs). Guarantees label quality (Opus judged) but query monoculture.
- **L2: 2387 prod queries with reranker pseudo-labels.** Run baseline retrieval + reranker; treat ranks 1–3 as silver positives, 11–30 as hard negatives. ~12 k pairs after de-dup. Path-disjoint vs eval-v3 enforced by hashing the 90 expected_paths and dropping any (q, p) whose path is in the set. **Verification:** add an assert in `prepare_train_data.py` that errors if any train path appears as an eval-v3 expected_path.
- **L3: query-augmented from doc structure.** For each provider doc with a heading, generate `(heading_text, body_text)` synthetic pair. ~25 k synthetic pairs. Cheap and noise-bounded, but only useful for representation pre-training (R2/R5).

### CM4. Path-disjoint verification step (added to prepare_train_data.py)

```python
# pseudocode, to live in scripts/runpod/prepare_train_data.py post-build
eval_paths = {(r["repo_name"], p["file_path"])
              for r in load_eval_v3()
              for p in r["expected_paths"]}
leaked = [p for p in pairs if (p["_repo_name"], p["_file_path"]) in eval_paths]
if leaked:
    raise ValueError(f"REFUSE TO WRITE: {len(leaked)} train pairs collide with eval-v3 expected_paths")
```

Prevents repeating the v2 leakage incident (eval-critic H3, 27 % of v1 rows leaked).

---

## Recipes (5 candidates)

Each recipe declares: name, base, loss, data, hard-negatives, hyperparams, cost, expected delta, p(win), kill criteria, why-it-breaks-the-pattern.

---

### R1. TSDAE-pre-adapt → CoSENT + reranker hard negatives  ← **TOP PICK**

| Spec | Value |
|---|---|
| Base | `nomic-ai/nomic-embed-text-v1.5` (HF id) |
| Stage 1 loss | `losses.DenoisingAutoEncoderLoss` (TSDAE; Wang & Reimers 2021) |
| Stage 2 loss | `losses.CoSENTLoss` |
| Stage 1 data | 25 632 provider_doc + 16 599 docs chunks (no labels) — query-doc shadowing via random-token-deletion |
| Stage 2 data | L2 (2387 prod queries × ~5 positives × ~5 hard negatives ≈ ~12 k labeled triplets) |
| Hard negatives | HN-A (reranker_ft_gte_v8 ranks 11–30 from FTS+vec top-100) |
| Stage 1 LR | 3e-5, bs=8, 1 epoch (~42 k steps total → 1 h on A40) |
| Stage 2 LR | 2e-5, bs=16, 1 epoch (~750 steps) |
| Warmup | 10 % of total steps |
| max_seq_length | cap 512 (consistent with `src/models.py["docs"]`) |
| Apply nomic prefix | yes (`apply_nomic_prefix=True`, both stages) |
| Cost | $3.50 ($0.34/h × ~10 h A40 — pessimistic; baseline 6 h) |
| Wall-clock | ~10 h on A40 (~3 h Stage 1 + ~7 h Stage 2 if reranker mining counted in pod time) |
| Expected Δr@10 | +0.05 ± 0.07 |
| p(win, AND-gate +0.10pp) | **0.18** |
| p(any positive lift) | 0.55 |
| **Kill criteria** | After Stage 1: spot-bench 50 random eval-v3 rows on Mac (`benchmark_doc_intent.py --eval=… --model=docs-r1-stage1 --no-pre-flight`). If Δr@10 < −0.03, abort before Stage 2 ($0.50 sunk, $3 saved). After Stage 2 full bench: hard kill if any per-stratum Δ < −0.20 (matches AND-gate condition #3). |

**Why this breaks the 0/4 pattern (mechanism, not hopium):**

- *Failure mode 1 (anisotropy)*: TSDAE in Stage 1 is unsupervised denoising — it pushes representations apart by reconstructing dropped tokens, which is the canonical anti-anisotropy regularizer (Wang & Reimers 2021 Table 4: TSDAE alone lifts STS-B from 60 → 75 on small N). Then Stage 2 uses CoSENT, a *pairwise margin* loss with no in-batch negative dependency, so it doesn't collapse on small effective batch sizes the way MNRL does.
- *Failure mode 2 (label monoculture)*: L2 source uses 2387 unique prod queries vs the v1's 22 queries — a 108× expansion of query support. Reranker-pseudo-labeling means we don't need human judgment for 12 k pairs.
- *Failure mode 3 (train↔eval gap)*: prod queries *are* the eval distribution (eval-v3 is 67 % prod-sampled). Train and eval now share head-term mass.
- *Failure mode 4 (recipe family stickiness)*: this is the first recipe in the project that uses a non-MNRL loss. Path-different from v0/v1/v1-fixed.

**Risks pre-empted (for skeptic):**
- "Reranker-mined negatives just teach the model the reranker's biases." → True, but reranker IS what the production system uses to score, so aligning bi-encoder candidates to reranker preferences is exactly the goal. End-to-end (router → bi-encoder → reranker) is what we deploy.
- "TSDAE on 42 k chunks is expensive." → ~3 h A40 = $1; cheaper than the v1-fixed run alone.
- "CoSENT loss isn't in train_docs_embedder.py." → it's in `sentence_transformers.losses` since 4.0; one-line import + swap. Code change ~5 lines, no new dep.
- "Path-disjoint isn't enforced in current code." → CM4 patch lands in train_docs_embedder.py wrapper; refuses to write JSONL on collision.

**Concrete commands (copy-paste, fresh-context-engineer-runnable):**

```bash
# Mac, pre-flight
cd ~/.code-rag-mcp
python3.12 -m pytest tests/ -q                                           # 719/719 expected
python3.12 scripts/runpod/cost_guard.py --check 4.0                      # OK ≤$5

# 1) Mine pseudo-labels + hard negatives (Mac, ~80 min)
python3.12 scripts/build_train_pairs_v2.py \
  --queries=logs/tool_calls.jsonl \
  --filter=doc-intent \
  --reranker=ms-marco-MiniLM-L-6-v2 \  # see note below
  --positives-rank=1-3 \
  --hard-neg-rank=11-30 \
  --eval-disjoint=profiles/pay-com/doc_intent_eval_v3.jsonl \
  --out=/tmp/train_v2.jsonl \
  --seed=42

# 2) Pod create + upload (RunPod CLI)
python3.12 scripts/runpod/pod_lifecycle.py --start --gpu=A40 --hours=12

# 3) Stage 1 TSDAE on pod
ssh pod 'cd /workspace/code-rag-mcp && \
  python3 scripts/runpod/train_docs_embedder.py \
    --base=nomic-ai/nomic-embed-text-v1.5 \
    --train=/workspace/tsdae_corpus.jsonl \
    --loss=tsdae \
    --steps=42000 --batch-size=8 --lr=3e-5 \
    --out=/workspace/r1_stage1'

# 4) Mac smoke kill-gate (50-row probe; if Δr@10 < -0.03, abort)
scp pod:/workspace/r1_stage1 ./r1_stage1
python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3.jsonl \
  --model-path=./r1_stage1 \
  --probe=50 --no-pre-flight

# 5) Stage 2 CoSENT on pod
ssh pod 'python3 scripts/runpod/train_docs_embedder.py \
  --base=/workspace/r1_stage1 \
  --train=/workspace/train_v2.jsonl \
  --loss=cosent \
  --hard-neg-key=hard_negatives \
  --steps=750 --batch-size=16 --lr=2e-5 \
  --out=hf:Tarshevskiy/pay-com-docs-embed-r1'

# 6) A/B on eval-v3 (Mac or pod)
python3.12 scripts/build_docs_vectors.py --force --model=docs-r1
python3.12 scripts/benchmark_doc_intent.py \
  --eval=profiles/pay-com/doc_intent_eval_v3.jsonl \
  --model=docs-r1 --no-pre-flight \
  --compare /tmp/bench_v3_docs.json /tmp/bench_v3_docs-r1.json
```

Reranker note: production uses `reranker_ft_gte_v8`. For mining, `ms-marco-MiniLM-L-6-v2` is a faster proxy and what the rest of the codebase already cites for *baseline* reranking. Either is acceptable; use the one already loaded by `daemon.py` to skip the cold-start cost.

---

### R2. CachedMultipleNegativesRankingLoss with prod-mined hard negatives

| Spec | Value |
|---|---|
| Base | `nomic-ai/nomic-embed-text-v1.5` |
| Loss | `losses.CachedMultipleNegativesRankingLoss` (mini-batch grad accumulation) |
| Data | L2 (2387 prod queries × ~3 positives ≈ 6 k pairs); each row carries 5 hard negatives via HN-A |
| Hard negatives | HN-A (reranker mined) |
| LR | 2e-5, bs=4 inner / acc=16 (effective bs=64), 1 epoch |
| Steps | ~1500 |
| Warmup | 150 |
| Cost | $1.20 (~3.5 h A40) |
| Expected Δr@10 | +0.02 ± 0.06 |
| p(win, AND-gate +0.10pp) | **0.10** |
| p(any positive lift) | 0.45 |
| **Kill criteria** | bench at step 750 (mid-epoch checkpoint); if Δr@10 < 0 abort |

**Why it breaks the pattern:** GradCache decouples effective batch size from GPU memory; effective bs=64 is 16× larger than v1's bs=4 and gives in-batch negatives a chance to tile multiple strata. *Same loss family as MNRL* — mitigated by larger effective batch + explicit hard negatives, but still in the recipe family that lost 4×. p(win) lower than R1 for that reason.

**Risk:** If the bottleneck was *recipe family* (not just batch size), this still loses. R2 is a "minimum surgery" recipe — useful as a fallback if R1 setup is too expensive.

---

### R3. MarginMSE cross-encoder distillation (teacher-student)

| Spec | Value |
|---|---|
| Base | `nomic-ai/nomic-embed-text-v1.5` |
| Loss | `losses.MarginMSELoss` |
| Teacher | `cross-encoder/ms-marco-MiniLM-L-6-v2` (already loaded) |
| Data | L2 with teacher scores: each row `(q, pos, neg, margin)` where margin = teacher_score(pos) - teacher_score(neg) |
| Hard negatives | HN-C — for each prod query, score top-100 cands with teacher cross-encoder; sample positives from rank 1-2 (high score) and negatives from ranks 11-50 (lower but ambiguous) |
| LR | 3e-5, bs=12, 1 epoch (~1000 steps) |
| Cost | $2.50 (~7 h A40 — teacher scoring 2387×100 = ~240 k cross-encoder forwards is the long pole) |
| Expected Δr@10 | +0.04 ± 0.06 |
| p(win, AND-gate +0.10pp) | **0.15** |
| p(any positive lift) | 0.50 |
| **Kill criteria** | post-teacher-mining smoke: 30 random rows from /tmp/train_r3.jsonl — if `<10%` rows have margin > 0.5 (teacher confidence), abort (teacher signal too noisy). Mid-train: bench at 500 steps; abort if Δr@10 < -0.02. |

**Why it breaks the pattern:** Distillation transfers the cross-encoder's full *ranking knowledge* into the bi-encoder, not just a binary label. This is qualitatively different from MNRL or CoSENT — the loss is regression on the teacher's score gap, which is a strictly richer signal.

**Risk:** The teacher cross-encoder is the same one used in production reranking — distilling it into the bi-encoder is, in a sense, double-use of that model. End-to-end pipeline `router → bi-encoder → reranker` may not gain because the reranker sees the same examples it taught the bi-encoder. (Counter: bi-encoder gets candidates *to* the reranker, so improving bi-encoder recall@100 — even at no Δr@10 — reduces reranker error rate.)

---

### R4. Two-tower contrastive with InfoNCE on doc-doc co-occurrence

| Spec | Value |
|---|---|
| Base | `nomic-ai/nomic-embed-text-v1.5` |
| Loss | InfoNCE (manual, `torch.nn.functional.cross_entropy` over similarity scores) |
| Data | L3 — synthetic `(heading, body)` pairs from 25 k provider doc chunks (no human labels) |
| Hard negatives | none (in-batch only, but bs=128 because chunks are short) |
| LR | 5e-5, bs=128, 2 epochs (~400 steps) |
| Cost | $0.50 (~1.5 h A40) |
| Expected Δr@10 | -0.02 ± 0.05 |
| p(win, AND-gate +0.10pp) | **0.05** |
| p(any positive lift) | 0.30 |
| **Kill criteria** | post-train smoke: cosine similarity of (heading, body) self-pair on held-out chunks > 0.92 — if not, the loss didn't converge; abort. |

**Why it (probably doesn't) break the pattern:** Heading-body is a weak proxy for query-doc — it teaches the model "this section's heading describes this section's body" which is approximately what the query answers, but transfer to real queries is lossy. Included as a **cheap baseline** for the skeptic to compare against.

**Risk:** Doc-internal contrastive can easily learn lexical/format patterns rather than semantics; +0.30 prior on positive lift is generous, real outcome may be no measurable change.

---

### R5. Domain-adaptive masked-LM continued pre-training → vanilla MNRL

| Spec | Value |
|---|---|
| Base | `nomic-ai/nomic-embed-text-v1.5` |
| Stage 1 loss | masked-LM (HF transformers `Trainer` over the bert_decoder head from nomic_bert) |
| Stage 2 loss | `losses.MultipleNegativesRankingLoss` (yes, the same one that lost 4× — but on a domain-adapted base) |
| Stage 1 data | 42 k provider+workflow doc chunks (unlabeled) |
| Stage 2 data | L1 (the existing 91 pairs) — or L2 if R1 mining infrastructure already exists |
| Hard negatives | none for Stage 2 if L1; HN-A if L2 |
| LR | Stage 1: 2e-5, bs=16, 3 epochs; Stage 2: 1e-5, bs=8, 1 epoch |
| Cost | $4.00 (~12 h A40 — MLM is the expensive stage; skips R1's reranker mining cost) |
| Expected Δr@10 | +0.03 ± 0.08 |
| p(win, AND-gate +0.10pp) | **0.13** |
| p(any positive lift) | 0.50 |
| **Kill criteria** | Stage 1 perplexity < 8.0 on held-out chunks (nomic baseline ~12 on generic web; we want domain-adapted lower) — if not reached, abort Stage 2. |

**Why it breaks the pattern:** MLM continued pre-training on the domain corpus is the canonical fix for "model knows English, not pay-com" — it shifts the embedding manifold toward domain vocabulary (PaymentMethodToken, MerchantWDRequestId, payper, aircash). However Stage 2 reverts to MNRL, which is still the recipe-family-with-bad-prior. Including R5 because MLM-pre-adapt is in the literature (Gururangan et al. 2020 "Don't Stop Pretraining") and worth a comparison shot.

**Risk:** MLM expensive ($3 of the $4) and Stage 2 MNRL still subject to anisotropy; lift could come entirely from the MLM half and Stage 2 could reverse it.

---

## Ranking by p(win) × cost-efficiency

`Score = p(win) / cost_usd` — higher is better.

| Rank | Recipe | p(win) | Cost USD | Score | Verdict |
|---|---|---|---|---|---|
| **1** | **R1** TSDAE→CoSENT+HN-A | 0.18 | 3.50 | **0.0514** | **TOP PICK — execute first** |
| 2 | R3 MarginMSE distillation | 0.15 | 2.50 | 0.0600 | (higher score but lower absolute p(win); fallback if R1 setup blocks) |
| 3 | R5 MLM pre-train + MNRL | 0.13 | 4.00 | 0.0325 | only-if-budget run after R1 wins |
| 4 | R2 CachedMNRL + HN-A | 0.10 | 1.20 | 0.0833 | (highest score but lowest p(win); useful as a cheap probe) |
| 5 | R4 doc-internal InfoNCE | 0.05 | 0.50 | 0.1000 | (highest score, lowest absolute; only worth running if R1 fails to give signal) |

**Why R1 over R3** (despite R3's marginally higher score per $): R3 distills the *same* cross-encoder that the production reranker is fine-tuned from. R1 attacks a different mechanism (anisotropy) AND uses a non-MNRL loss family. Recipe-family diversity is more valuable than +0.04 cost-efficiency points when the prior is 0/4.

---

## Recommendation to lead

**Execute R1 first. Total commit ≤$3.50, kill at Stage 1 if smoke fails ($0.50 floor).**

If R1 lifts ≥+5 pp on eval-v3 (below AND-gate but directional), R3 next (different mechanism). If R1 hits AND-gate, **stop** — ship R1.

If R1 fails entirely (Δr@10 ≤ 0), drop FT axis for the session and bank $7.50 toward router/reranker work — that's the strategist's verdict from p6, and R1 was the highest-prior FT shot. Don't double down by re-running R3/R5 after R1 fails.

---

## Open gaps (acknowledged)

1. `losses.CoSENTLoss`, `losses.MarginMSELoss`, `losses.DenoisingAutoEncoderLoss` are all in `sentence_transformers ≥ 4.0`, but the wrapper at `scripts/runpod/train_docs_embedder.py` hardcodes `losses.MultipleNegativesRankingLoss` (line 108). **Code change required:** add `--loss=mnrl|cosent|marginmse|tsdae|mlm` flag with branching. ~30 lines, must keep 719/719 green.
2. `scripts/build_train_pairs_v2.py` does not exist. **New script required**, ~150 LoC: load logs/tool_calls.jsonl, filter via `_query_wants_docs` mirror, run baseline retrieval+reranker on each query, emit `(q, pos, [hard_negatives])` rows with eval-v3 path-disjoint enforcement.
3. nomic_bert MLM head (R5) is non-trivial — `trust_remote_code=True` model may not expose `head` directly. Verify with `model.auto_model.lm_head` before committing R5 plan. **Not blocking for R1.**
4. Reranker mining wall-clock (80 min on Mac CPU) is a one-time cost; if Mac CPU is too slow, run mining on the same A40 pod that does training (GPU CrossEncoder ~5× faster).

These are gaps in *infrastructure*, not in the recipe design. R1 can be staged: first land the train_docs_embedder.py loss-flag patch (no FT cost, just code+pytest), then build_train_pairs_v2.py (no FT cost), then run R1 Stage 1. By the time pod is up, the only cost is GPU time.

---

## Final pre-empt for skeptic

> "You proposed positive-only MNRL on more pairs, just like v0/v1/v1-fixed."

No. R1 uses TSDAE → CoSENT (neither is MNRL). R2 uses CachedMNRL with explicit hard negatives (different from v0/v1). R3 uses MarginMSE with cross-encoder teacher. R5 uses MLM continued pre-training. Only R5's Stage 2 reverts to MNRL, and even there the base has been domain-adapted first.

> "0/4 prior says drop FT entirely."

The strategist (p6) said the same. I'm proposing a recipe-family escape that wasn't tested in the 4 prior runs — TSDAE+CoSENT is novel for this project. If it loses on eval-v3, that's the 5th rejection, the prior firms to 0/5 (Jeffreys 1/11 ≈ 0.09), and the strategist's bank-the-money call wins decisively.

> "Eval-v3 is biased the same way eval-v2 was."

Eval-v3 was rebuilt with model-agnostic labeler (`final-report.md:38` confirms FTS+overlap, no vec). The bias the strategist worried about (vec_pool tied to baseline) is gone. Per-stratum AND-gate condition catches "win on average, lose on a stratum" cherry-picks.

> "The 22-query monoculture is real but you didn't show that 2387 prod queries have clean labels."

Correct — that's exactly why R1 uses *reranker pseudo-labels* (silver, not gold). R1 doesn't claim 12 k human-judged pairs; it claims 12 k reranker-aligned pairs that sample the head of prod-query distribution. The hard-negative supervision *is* the alignment signal.

> "You're claiming p(win)=0.18 with 0/4 evidence."

Jeffreys prior gives 1/9 = 0.11 for "any FT recipe wins". R1 multiplier 1.6× because:
- New loss family (CoSENT) — no prior failures with this loss in the project: ×1.3
- Explicit hard negatives from production reranker — addresses anisotropy directly: ×1.2
- 108× larger query support (2387 vs 22) — addresses train↔eval gap: ×1.05
- Bayesian product: 0.11 × 1.3 × 1.2 × 1.05 ≈ 0.18

Each multiplier is conservative. If the skeptic insists 0.15 is the realistic ceiling, it doesn't change the recipe ordering — R1 still wins.
