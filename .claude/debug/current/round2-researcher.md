# Round 2 — Researcher (cross-critique of DE + SY)

## DE — Data Engineer

DE's position summarized in MY words (≤3 sentences): GT pointing at deleted, regenerated, or lockfile paths means our hit@10 metric is 18% measuring retrieval and 82% measuring index/eval drift, so every ranker tweak has been gradient-descending into noise. The fix is sequenced: clean the eval (DE1) → relax the extractor to lift the recall ceiling (DE2) → migrate primary eval to real prod traffic (DE3). Until DE1 lands, no model/ranker/prefilter number can be trusted as a real win.

**Strongest point**: DE1's measurement that **57.9% of GT is not in the index AND 25.4% is mechanical noise** (`package-lock.json` 1141×, etc.). This is a hard upper bound on hit@10 that no ranking improvement can overcome — it makes DE1's "GT ⊆ index" invariant the single most leverage-positive move on the table. The number is grounded in a SQL grep, not speculation, and the failure mode (W1/W2 all ±3pp NOISE) is exactly what optimizing against a noisy oracle looks like.

**Weakest point**: DE3 (pivot to `tool_calls.jsonl` prod queries) is right in spirit but wrong in cost-benefit ordering — it requires GT labeling we don't have ($10 + 12h + Opus judging), and the proposal admits we'd need a "proxy-label heuristic" (read-after-search) of unknown quality. Until DE1 is run and we know whether the *current* eval is fixable, building a *new* eval on weaker labels is jumping a layer ahead. DE3 is correct as the long-term north star, not the next move.

---

## SY — Systems Thinker

SY's position summarized in MY words (≤3 sentences): The +11.89pp FTS5-sanitize win has a system shape — a single silent-failure site dwarfs every score-function tweak — and the next +6.5pp is structurally identical: a hidden signal-loss site, not a ranking knob. SY1 (audit bench-prod parity, kill production glossary if it hurts) measures the bench artifact directly; SY2 (per-leg ablation under feature flags) reveals which retrieval leg is dominant vs dilutive; SY3 (per-stage GT instrumentation) replaces darkroom debugging with an empirically isolated drop-off curve. All three are diagnostic-first, no model swaps until the diagnosis is in.

**Strongest point**: SY3 (per-stage GT instrumentation) is the **permanent capability**: once we log `gt_in_fts150 / vec50 / rrf_top200 / rerank_input / top10_post_rerank` per query on jira n=908, every future A/B has a clean drop-off curve to attribute against. This is a one-time tooling cost (≤1 day) that converts every subsequent debate from "what's the bottleneck?" to "where on the curve does this candidate move the needle?". Its value compounds across all later sessions.

**Weakest point**: SY1's framing of the glossary as "potentially a >5pp lift on real traffic with zero risk" understates the magnitude. W2 cross-evidence (−9.71pp jira from a stricter glossary) only bounds the sign; the live glossary could be net-positive on jira-style PR titles even if the stricter W2 variant was negative. Stripping `expand_query` from production without the (b) bench result risks regressing prod recall on doc-intent queries that the glossary specifically targets (auth/sca/3ds/apm). SY1 is right to measure first; calling it "zero risk" before measurement is overconfident.

---

## Specific attention items from team-lead

### A. DE1's 25.4% mechanical-noise claim — does it change RE1's repo-prefilter signal?

**Re-tested empirically just now.** Re-ran the GT-concentration measurement after stripping DE1's noise patterns (`package-lock.json`, `package.json`, `*generated*`, `consts.{js,ts}`, lockfiles, `.drone.yml`, etc. — 3950/22459 = 17.6% of GT pairs stripped, 160 queries dropped for <3 clean GT remaining):

| measurement | original (n=908) | clean (n=748) |
|---|---|---|
| ≥50% GT in single repo | 775/908 = 85.4% | 638/748 = **85.3%** |
| ≥80% GT in single repo | (not computed) | **386/748 = 51.6%** |

**The repo-prefilter signal SURVIVES DE1's attack — 85.3% vs 85.4% is well within sampling noise.** Mechanism: even when `package-lock.json` is over-represented in GT, it lives in the SAME repo as the actual code GT for that ticket — so concentration is preserved. RE1 still has +6 to +12pp expected lift on a clean eval; the lift estimate is unchanged by the noise correction.

**However — the team-lead's hypothesis was not silly.** A second-order effect: if `backoffice-web` (27% of all GT) has 1141 lockfile entries inflating its share, then a prefilter that "predicts backoffice-web" gets credited for retrieving the lockfile, not the real `.tsx`. On a CLEAN eval, RE1's measured lift will be HONEST; on the dirty eval, RE1 might score +1-2pp more than it deserves. Conclusion: **DE1 is a strict prerequisite for measuring RE1 honestly**, but it does not invalidate RE1's mechanism.

### B. Is SY3 (per-stage instrumentation) a prerequisite to RE1, or independent?

**Independent — but bundling them is strictly better.** RE1 is implementable WITHOUT SY3: predict top-3 repos via BM25-over-README, ×1.4 boost in fusion, A/B against baseline on a cleaned eval. The mechanism doesn't require knowing where the GT was being lost; it adds a new signal that lifts *whichever stage was losing it*.

**However**, if RE1 ships first and lands, say, +4pp, we have NO ATTRIBUTION CHAIN — did it improve FTS pool inclusion? Did it survive the rerank cut better? Without SY3 telemetry, we can't tell whether to invest in pushing RE1 further (e.g., learned classifier instead of BM25) or pivot to a different signal. **Recommendation**: do SY3 (≤1 day) before RE1; treat it as the bench harness's permanent fixture. The cost is small and the diagnostic value compounds.

### C. DE's VETO of "another model swap" — does RE2 (Doc2Query) pass DE's bar?

**Yes — RE2 explicitly is NOT a model swap, and DE's veto categories don't apply.** Mapping:

| DE's VETO categories | RE2 (Doc2Query) status |
|---|---|
| Boost/penalty/threshold tuning | No — RE2 changes the FTS5 indexed CONTENT, not weights/thresholds |
| Model swap or fine-tune of encoder/reranker | No — reranker and encoder UNCHANGED; offline LLM is used to generate synthetic queries that get indexed alongside the original chunk |
| New rerank model | No — same `ms-marco-MiniLM` / `reranker_ft_gte_v8` |

RE2 is **structurally a data-pipeline change** (augment chunks at index time with synthetic queries derived from the chunk content itself, filtered by Gospodinov-2023's retrieve-own-chunk filter). DE's framing — "data quality is upstream, ranker work without these is sandcastle" — actually FAVORS RE2: it bridges the short-query/long-doc lexical gap by enriching the indexed corpus, not by tuning a knob downstream.

**However, RE2 does fail one DE-derived bar I want to add explicitly: it must run AFTER DE1+DE2.** Doc2Query indexed against a corpus where 57.9% of GT files aren't even present can't recover those queries — synthetic queries can only help when a chunk exists to attach them to. So the ordering is:

```
DE1 (clean eval, 4h) → DE2 (extractor relax, 8h+3h) → SY3 (instrumentation, 1d)
  → RE1 (repo prefilter, 6-10h, $0)
  → RE3 (code-aware FTS5 tokenizer, 4-6h, $0)
  → RE2 (Doc2Query, 16h, $5-15)   ← only after DE2 lands so RE2 has a richer corpus to enrich
```

This passes DE's bar AND extracts ~+15pp of additive lift across the chain.

---

## Updated ranked list (post-Round-2)

Reordering RE1/RE2/RE3 by REAL leverage given DE+SY's diagnoses:

| # | move | type | est. lift on CLEAN jira hit@10 | dev / $ | depends on |
|---|---|---|---|---|---|
| 0 | **DE1 (eval clean)** — adopt | data | unblocks honest measurement (n=908 → ~n=748 clean) | 4h, $0 | — |
| 1 | **SY3 (per-stage instrumentation)** — adopt | diagnostics | 0pp metric, infinite future leverage | 1d, $0 | — |
| 2 | **DE2 (extractor relax)** — adopt | data | +5-10pp R@10 ceiling lift | 8h+3h, $0 | DE1 |
| 3 | **RE1 (repo prefilter)** — own | retrieval | +6-12pp hit@10 | 6-10h, $0 | DE1 (for honest measurement); SY3 (for attribution) |
| 4 | **RE3 (code-aware FTS5 tokenizer)** — own | retrieval | +2-5pp hit@10 | 4-6h, $0 | DE1 |
| 5 | **RE2 (Doc2Query, offline RunPod)** — own | data + index | +3-7pp hit@10 | 16h, $5-15 | DE1+DE2 |

**Strategic claim**: my Round-1 ranking (RE1 > RE2 > RE3) is **unchanged in priority among researcher moves**, but I now CONCEDE that DE1 + SY3 should ship FIRST. The combined ship plan is additive: DE1 → SY3 → DE2 → RE1 → RE3 → RE2 yields a cumulative expected lift of +16 to +34pp on hit@10 (jira-clean). Even at the lower bound, this clears 53.5% + 16 = 69.5% > 60% goal.

**Veto changes from Round 1**: I withdraw the Round-1-implicit "RE1 first" framing. SY's diagnostic-first lens is correct; without DE1+SY3, RE1 ships with a measurement that's part-noise. Order matters more than my Round-1 ranking suggested.

**On synonyms (final lens)**: Both DE and SY independently dismiss further glossary tuning. DE because the 25.4% lockfile-noise dominates whatever recall expand_query buys; SY because W2 evidence shows the live glossary is more likely net-negative than positive. RE2 (Doc2Query) remains the SOTA answer here — synonym discovery moves to the index, corpus-driven, not curator-driven — but only after DE1+DE2 give it a corpus that contains the GT.
