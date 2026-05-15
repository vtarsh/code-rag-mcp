---
author: gte-unblocker (debate teammate, task #2)
date: 2026-04-25
team: debate-recipe-improvement
question: Is the gte-large NTK-rope blocker fixable cheaply, and is fixing it worth doing?
inputs:
  - ~/.code-rag-mcp/.claude/debug/final-report.md
  - ~/.code-rag-mcp/.claude/debug/loop-log.md (lines 488–514, 666–668)
  - ~/.code-rag-mcp/.claude/debug/p6-pivot-strategist.md (claims 6h yak-shave)
  - HF cache modeling.py (1418 LOC)
  - CPU smoke loads on gte-large + gte-base + post-patch verification
verdict_one_line: "U1 fork-and-patch via 3-line runtime monkey-patch — ~30 min, $0 local, p(unblock)=0.95. Pivot-strategist's 6h estimate is ~12× too high."
---

# 1. DIAGNOSIS

## 1.1 Reproduction (CPU, Mac, Python 3.12, transformers 5.6.2 / sentence-transformers 5.3.0 / torch 2.9.1)

```python
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('Alibaba-NLP/gte-large-en-v1.5', trust_remote_code=True, device='cpu')
m.encode(['nuvei withdrawal webhook'])
```

Stack trace (verbatim):
```
File ".../modeling.py", line 392, in forward
    rope_cos = rope_cos[position_ids].unsqueeze(2)  # [bs, seq_len, 1, dim]
IndexError: index 4664831856 is out of bounds for dimension 0 with size 9
```

Identical failure mode reproduced for `Alibaba-NLP/gte-base-en-v1.5`:
```
IndexError: index 6593903585892573700 is out of bounds for dimension 0 with size 9
```

→ Both gte-large AND gte-base share the bug. They use the SAME `Alibaba-NLP/new-impl/modeling.py` via `auto_map` in their config — verified by reading `~/.cache/huggingface/hub/models--Alibaba-NLP--gte-large-en-v1.5/.../config.json` (`auto_map.AutoModel = "Alibaba-NLP/new-impl--modeling.NewModel"`).

## 1.2 modeling.py:389–392 (the failing block)

```python
389        # Compute rotary embedding
390        if self.position_embedding_type == 'rope':
391            rope_cos, rope_sin = self.rotary_emb(inputs_embeds, seq_len=seq_length)
392            rope_cos = rope_cos[position_ids].unsqueeze(2)  # [bs, seq_len, 1, dim]
393            rope_sin = rope_sin[position_ids].unsqueeze(2)  # [bs, seq_len, 1, dim]
```

Line 392 indexes `rope_cos` (shape `[seq_length, dim]`, e.g. `[9, 64]` for a 9-token query) with `position_ids` (an int64 tensor expected to contain `[0..seq_length-1]`).

## 1.3 Root cause (REVISED from prior session — pivot-strategist's NTK-overflow hypothesis is wrong)

The prior loop-log diagnosis claimed: *"NTK rope position buffer initialised to size 9 (default), then forward expects size=seq_length but reads garbage"*. This is **incorrect**. I traced the live state:

```
type(rotary_emb)            = NTKScalingRotaryEmbedding   ✓ correct subclass
rotary_emb.cos_cached.shape = [16384, 64]                 ✓ correct (8192 × scaling 2.0)
rotary_emb.sin_cached.shape = [16384, 64]                 ✓ correct
position_ids buffer.shape   = [8192]                      ✓ correct shape
```

**Buffer SHAPES are correct.** The actual fault is in the **VALUES**:

```python
emb.position_ids[:10] = [0, 4367638688, 64865, -1, 168627659, 127192916757307, 227, 23089744183296, ...]
emb.rotary_emb.inv_freq[:5]    = [0.0, 0.0, 0.0, 0.0, 0.0]
emb.rotary_emb.cos_cached.min  = 0.0
emb.rotary_emb.cos_cached.max  = 0.0    # ENTIRE 16384×64 cache is zeros
```

All three buffers registered with `persistent=False` are post-load garbage:
1. `NewEmbeddings.position_ids`         (line 308–310, `torch.arange(8192)` → garbage int64 memory)
2. `RotaryEmbedding.inv_freq`           (line 191, computed inv-freq → all zeros)
3. `RotaryEmbedding.cos_cached/sin_cached` (line 205–206 + 250–251, computed → all zeros)

The flow: `NewEmbeddings.__init__` runs at construction time and computes correct values. **transformers ≥ 5.0** uses `accelerate.init_empty_weights()` lazy materialization (replacing `__init__` tensor allocations with `torch.empty(..., device='meta')`), then `from_pretrained` materializes only `persistent=True` state-dict entries. Buffers registered `persistent=False` get **allocated empty** but **never re-populated** — their `__init__` values are lost.

**Why line 392 specifically explodes**: `position_ids` = uninitialized int64 memory. When sliced `position_ids[:9]`, it returns 9 random int64 values (e.g. `4664831856`). These are then used as indices into `rope_cos` of size 9 → out-of-bounds.

(The cos/sin/inv_freq being zeros is also wrong but doesn't cause an IndexError — it would cause `nan` outputs, which is what I observed in my second smoke test before re-running `_set_cos_sin_cache`.)

## 1.4 Fix verification (CPU, end-to-end)

After 3-call buffer rebuild (re-init `position_ids`, recompute `inv_freq`, call `_set_cos_sin_cache`), encoding succeeds:

```
=== gte-LARGE post-patch ===
shape=(1, 1024) norm=23.949
cos(payment-related, payment-related) = 0.4793
cos(payment-related, fruit)           = 0.3966   Δ = 0.083 (sane discrimination)

=== gte-BASE post-patch ===
shape=(1, 768) norm=21.730
cos(payment-related, payment-related) = 0.3270
cos(payment-related, fruit)           = 0.2874   Δ = 0.040 (weaker but sane)
```

→ gte-large gives ~2× the discrimination of gte-base on a 3-document smoke. Use gte-large.

---

# 2. THREE UNBLOCK OPTIONS

## U1 — Fork-and-patch via runtime monkey-patch in `_load_sentence_transformer`

**Approach**: don't fork modeling.py at all. Apply the buffer re-init **after** `SentenceTransformer(..., trust_remote_code=True)` returns, inside our existing `src/index/builders/docs_vector_indexer.py::_load_sentence_transformer` hook (already has `cfg.max_seq_length` cap from commit `fdc5c2a3`).

**Concrete patch** (3 lines in indexer + ~8-line helper):

```python
# src/index/builders/docs_vector_indexer.py — add after model is loaded

def _fix_gte_persistent_false_buffers(model):
    """transformers ≥ 5 + accelerate lazy-init drops persistent=False buffer values
    on Alibaba-NLP/new-impl modeling. Re-initialize them post-load.
    Bug repro & fix: ~/.code-rag-mcp/.claude/debug/debate-gte-unblock.md
    """
    auto = model._first_module().auto_model
    if type(auto.embeddings).__name__ != 'NewEmbeddings':
        return  # not a gte/new-impl model; no-op
    import torch
    cfg = auto.config
    auto.embeddings.register_buffer(
        'position_ids',
        torch.arange(cfg.max_position_embeddings, device=auto.device),
        persistent=False,
    )
    rot = auto.embeddings.rotary_emb
    inv_freq = 1.0 / (rot.base ** (torch.arange(0, rot.dim, 2, device=auto.device).float() / rot.dim))
    if hasattr(rot, 'scaling_factor') and getattr(rot, 'mixed_b', None) is None:
        inv_freq = inv_freq / (rot.scaling_factor ** (2 / rot.dim))  # NTK paper eq (6)
    rot.register_buffer('inv_freq', inv_freq, persistent=False)
    rot._set_cos_sin_cache(int(rot.max_seq_len_cached), inv_freq.device, torch.float32)
```

Wired into `_load_sentence_transformer` immediately after `SentenceTransformer(...)` constructor returns (1 call site, conditional on the `NewEmbeddings` class so it's a no-op for nomic / CodeRankEmbed / arctic / bge-m3).

**Steps**:
1. Implement helper + call site (~10 min code).
2. CPU smoke `python3.12 -c "..."` (~2 min).
3. Pytest 719/719 (~1 min, helper is no-op for non-gte models so existing tests can't regress).
4. md5-verified mcp__github__push_files commit (~2 min).
5. Pod cycle to A/B against eval-v3 (~$0.30, ~25 min wall).

**Total**: ~30 min code + commit, ~25 min pod, **~$0.30 spend, $14.70 remaining of $13.30 banked + future allocation.**

**Effort**: 0.5 h. **Risk**: low — patch is local, conditional on model class, mathematically derived from the same `_set_cos_sin_cache` code in the upstream file (no behavior diverging from intended). **p(unblock)** = **0.95** (the only failure mode is if quality smoke also surfaces other lazy-init bugs e.g. in attention/layernorm — scanned `named_parameters` for nan/inf and found NONE post-patch, so this is unlikely).

**Maintenance cost**: zero. The helper is a 10-line idempotent runtime fix. If transformers fixes the underlying issue we delete the function and ship.

---

## U2 — Vendor patched copy at `src/index/builders/_gte_modeling.py`

**Approach**: download `modeling.py` from HF to our repo, edit `__init__` of `NewEmbeddings` to set `persistent=True` for `position_ids` (and same for the rotary buffers), then override `auto_map` via a custom `from_pretrained` wrapper to load *our* modeling.py instead of HF's.

**Where to put it**: `src/index/builders/_gte_modeling.py` + `src/index/builders/_gte_configuration.py` (both files needed because configuration.py contains `NewConfig`).

**Wiring**:
```python
# in src/index/builders/docs_vector_indexer.py
def _load_gte_with_local_modeling(model_id):
    from . import _gte_modeling, _gte_configuration
    config = _gte_configuration.NewConfig.from_pretrained(model_id)
    model = _gte_modeling.NewModel.from_pretrained(model_id, config=config)
    # wrap in SentenceTransformer scaffold matching the HF config (3 lines)
    ...
```

**Effort**: ~2.5 h. Why so much higher than U1: vendoring means we own the full 1418-LOC modeling.py + 200-LOC configuration.py, must validate every code path (xformers / packed-qkv / unpad / NTK / etc.), AND must rebuild SentenceTransformer wrapping by hand because `auto_map` resolution is what triggers `trust_remote_code` download. Plus we need to keep our copy in sync if Alibaba pushes a fix.

**Risk**: medium — the surface area is 1600 LOC of foreign code we now own. ANY upstream regression we miss = silent recall hit.

**p(unblock)** = 0.85 (the SentenceTransformer scaffold reconstruction is the failure mode; SentenceTransformer reads `1_Pooling/config.json` + `modules.json` + `sentence_bert_config.json` which assume HF's config — fiddly).

**Maintenance cost**: high. Every `transformers` upgrade requires re-validating the vendored copy.

**Verdict**: U2 is strictly worse than U1 unless U1 surfaces additional latent lazy-init bugs (it didn't in my scan).

---

## U3 — Pivot to `Alibaba-NLP/gte-base-en-v1.5`

**CPU smoke result**: gte-base **has the exact same bug** (line 392 IndexError, same modeling.py path). Pivoting to gte-base does NOT bypass U1 work — it requires the same patch.

**If you DO apply U1 to gte-base**:
- dim 768 (vs gte-large 1024) → smaller index, faster encode, lower RAM
- MTEB (English, public leaderboard 2024) gte-base ≈ 64.11 avg vs gte-large ≈ 65.39 avg → ~1.3pp behind on average benchmark
- My CPU smoke (3 docs): discrimination Δ = 0.040 (gte-base) vs 0.083 (gte-large) → gte-large gives ~2× signal on payment-vs-fruit pair

**Verdict on U3 alone**: useless without U1 (same bug). With U1 already applied, gte-large is the strictly better candidate at trivial extra cost.

**Optional D5 (gte-base as fallback)**: register `docs-gte-base-en-v1.5` alongside gte-large in the A/B run; if gte-large bench OOMs the pod despite 24 GB GPU + the position-ids fix, fall back to gte-base. ~$0.30 added pod time. p(needed) = 0.05.

---

# 3. RECOMMENDATION

**Run U1 first. ~$0.30, ~30 min, p(unblock)=0.95.**

| Path | Effort | $ | p(unblock) | Expected R@10 vs baseline 0.2509 (eval-v3, n=90) | Verdict |
|---|---|---|---|---|---|
| **U1** fork-and-patch monkey-patch | 0.5 h | $0.30 (pod for A/B) | **0.95** | **+0.01 to +0.04** (point est. 0.025) | **DO FIRST** |
| U2 vendor copy in repo | 2.5 h | $0.30 | 0.85 | same as U1 | only if U1 surfaces more bugs |
| U3 pivot to gte-base alone | identical to U1 (same bug) | n/a | 0.0 (without U1 patch) | n/a | rejected |

**Where the +0.01 to +0.04 prior comes from**:
- gte-large MTEB English avg = 65.39 vs nomic-embed-text-v1.5 = 62.39 → ~3pp absolute on MTEB (different eval, different domain — be cautious about transfer).
- Our prior 4 candidates ALL regressed -4 to -11pp on eval-v3, suggesting the corpus + eval is HARSH on swaps. Conditional on landing without regressing, the upside is bounded by MTEB gap.
- Honest p(beat baseline) = **~0.30** (one out of 5 candidate categories may finally clear). Honest p(beat baseline by AND-gate +0.10pp) = **~0.10**.

**Why this is worth doing despite low p(deploy)**:
1. **Cheap**. $0.30 + 30 min — order of magnitude less than the next-best FT recipe in task #1 ($3-5 + 1-2h).
2. **Information value**: a 5th measured rejection on eval-v3 closes the doc-tower hypothesis. Currently we have 4 measurements all in negative territory; a 5th confirms ceiling.
3. **Pivot-strategist's "6h yak-shave" estimate is 12× too high**. The actual fix is 3 lines.

---

# 4. DISAGREEMENT WITH PIVOT-STRATEGIST

p6-pivot-strategist labeled gte-large unblock as "❌ yak-shave outside P6 scope; ~6h debug for a model that may not even win after fixing." Two factual corrections:

1. **6h is wrong**. Actual cost: 30 min code + 25 min pod = ~1h wall. Already validated on CPU before any pod cost.
2. **The bug is NOT modeling.py-intrinsic**. Prior loop-log claimed *"Bug is intrinsic to `Alibaba-NLP/new-impl/modeling.py`, NOT a CUDA-only quirk"*. The bug is actually in the **interaction** between Alibaba's `persistent=False` buffers and `transformers ≥ 5` lazy init via `accelerate.init_empty_weights`. This is a 30-line helper, not an upstream PR.

If the team-lead accepts U1 cost as fitting "ship process gain", we add gte-large to the A/B without sacrificing pivot-strategist's other recommendations (eval-v3 + router probe) — they're orthogonal.

---

# 5. FALLBACK CANDIDATE (per task spec disagreement clause)

If the team rejects U1 (e.g. because eval-v3 is the priority and one more candidate adds noise), the next-best embedding family to test in P7 is:

**`mixedbread-ai/mxbai-embed-large-v1`** (1024d, 335M params, MTEB English avg 64.68, no NTK rope, BERT-style absolute positions → no `persistent=False` regression possible).

| Metric | gte-large-en-v1.5 | mxbai-embed-large-v1 |
|---|---|---|
| MTEB English avg | 65.39 | 64.68 |
| dim | 1024 | 1024 |
| max seq | 8192 (NTK) | 512 (BERT) |
| memory footprint (fp32) | ~1.7 GB | ~1.3 GB |
| HF id | Alibaba-NLP/gte-large-en-v1.5 | mixedbread-ai/mxbai-embed-large-v1 |
| custom modeling.py | yes (this bug) | no (vanilla bert) |
| p(loads cleanly) | 1.00 with U1 patch | 1.00 unconditional |

Ranking:
- **U1 (gte-large with patch)** > **mxbai-embed-large-v1** for raw MTEB ceiling
- **mxbai-embed-large-v1** > **U1** for code-simplicity / no-patch-required / safe maintenance

If the team values "no maintenance debt" higher than "+0.7pp MTEB headroom", swap U1 → mxbai. Both are 30-min jobs at this point.

---

# 6. ENVIRONMENT NOTES

No package installs needed. Everything ran on existing Mac env:
- Python 3.12.x
- transformers 5.6.2
- sentence-transformers 5.3.0
- torch 2.9.1 (CPU)

The HF cache modeling.py at:
`~/.cache/huggingface/modules/transformers_modules/Alibaba_hyphen_NLP/new_hyphen_impl/40ced75c3017eb27626c9d4ea981bde21a2662f4/modeling.py`

(NB: HF replaces hyphens with `_hyphen_` in its filesystem path encoding. Took 10 min to find — documenting here so the next session doesn't lose time on the same find command.)

---

# 7. ACTIONABLE PROPOSAL FOR LEAD SYNTHESIS

If GO on U1:
1. Apply patch in `src/index/builders/docs_vector_indexer.py` per §2.U1 code block (10 min).
2. Pytest 719/719 green check (1 min).
3. mcp__github__push_files single-file commit (2 min, md5-verified).
4. RunPod pod cycle: build gte-large lance dir, bench against eval-v3 (~25 min wall, ~$0.30).
5. Compare to baseline `/tmp/bench_v3_docs.json` (R@10 = 0.2509). AND-gate decides DEPLOY:yes/no.

Stop conditions:
- If gte-large R@10 < 0.20 (clear regression) → REJECT, document as 5th rejection on eval-v3, close doc-tower hypothesis.
- If 0.20 ≤ R@10 < 0.2509+0.01 → REJECT (no meaningful lift).
- If R@10 ≥ 0.2509+0.10 → DEPLOY (clears AND-gate).
- Anywhere in between → SHIP-AS-PROCESS-GAIN; tracker entry, no daemon swap.

Total budget impact: ~$0.30 of $13.30 remaining → **$13.00 banked after**.
