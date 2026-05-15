# Round 1 — Systems Thinker (SY)

## Frame

We are at hit@10 = 53.5% on jira_eval_n900 (n=908). The single biggest jump in this entire campaign was the FTS5 sanitize fix: +11.89pp from one-line input scrubbing. That win has a *system* shape: a silent-failure path swallowed errors and starved the rest of the pipeline. Every "ranking-layer tweak" since (W1, W2, model swaps ×17, pool size, stratum gate) has produced ≤ ±3pp because we are tuning around a pipeline whose bigger losses are already hidden somewhere upstream. The bottleneck is not a *score function*; it is a *signal-loss site*. We need to find the next FTS5-class loss before another reranker bake-off.

Pipeline survey (lines verified — `bench_routing_e2e.py:107` calls `hybrid_search` directly; `service.py:68` calls `expand_query` first; `fts.py:60-83` sanitize is post-expand; `hybrid.py:690` FTS pool=150, `hybrid.py:710` vector=50, `hybrid.py:832` rerank pool ≥200, `hybrid.py:846` stratum gate, `hybrid.py:861` rerank). Symptoms ≠ scores; symptoms = "queries that produce N=0 or N=tiny pools survive the pipeline". The W1/W2 NOISE result is consistent with this: when the pool is right, the reranker delta is small. When the pool is wrong, no rerank tweak can save it.

---

## SY1: Bench-prod parity audit + production glossary kill-switch

- **rank**: 1
- **bottleneck identified**: `expand_query` (production-only, bench-skipped). Production calls `service.search_tool` → `expand_query(query)` → `hybrid_search(expanded, ...)` (`service.py:68`). Bench calls `hybrid_search(q, limit=...)` directly (`bench_routing_e2e.py:107`). Glossary expansion is the single largest bench-prod drift in the system, AND we already have data showing a tighter version of it (W2) is catastrophic (−19.83pp v2, −9.71pp jira). Production is currently running this glossary every query while we measure on a bench that bypasses it. We literally do not know the production hit@10 — only the bench's idealized number.
- **justification (2 sentences)**: Every ranking-layer tweak has ±3pp bandwidth; bench-prod drift can be 10pp+ silently — that's where the leverage is. Until bench == prod we are optimizing an oracle, not the system the user actually queries.
- **experiment to validate**: Run `bench_routing_e2e.py` THREE times on jira_eval_n908: (a) baseline (current — bench skips expand), (b) `--with-expand` (call `expand_query(q)` before `hybrid_search`), (c) `--prod-via-service` (call `search_tool` and parse the formatted output). The deltas tell us three things at once: (1) is the bench artifically high vs prod? (2) does the current glossary help or hurt jira-class queries (we already know it hurts on v2)? (3) what is the *real* prod ceiling we are climbing? If (b) is materially below (a), strip `expand_query` from production immediately — that alone is potentially a >5pp lift on real traffic with zero risk to the ranking stack.
- **failure mode (1 concrete way it could fail to deliver)**: Glossary turns out to be net-zero on jira (mismatched eval distribution from W2's eval) and we still need ranking improvements for the +6.5pp goal. Mitigation: this experiment is < 1 hour; even a null result *certifies bench validity* and unblocks every subsequent A/B from "is this a bench artifact?" doubt.

---

## SY2: Stage-isolation A/B — measure which retrieval leg dominates

- **rank**: 2
- **bottleneck identified**: We have never measured the marginal contribution of FTS5 vs vector vs RRF-fusion on jira. We know the reranker pool sees ~200 candidates after RRF (`hybrid.py:832`); we do not know how many of the GT files reach that pool through which leg. The W1/W2 NOISE result is consistent with one leg already saturated and the other irrelevant — but if vector is dragging down the pool by injecting 50 wrong candidates that displace genuine FTS hits, the symptom looks like "RRF doesn't help" when the truth is "RRF is being poisoned".
- **justification (2 sentences)**: Until we know which leg has the GT recall and which leg is dilution, every weight/pool tuning is shadow-boxing. One bench run with three configs (FTS-only, vector-only, fusion) at K=200 gives us the recall ceiling per leg and a clean fusion-gain measurement.
- **experiment to validate**: Add three feature flags (no model changes): `CODE_RAG_FTS_ONLY=1` (skip vector_search call), `CODE_RAG_VECTOR_ONLY=1` (skip fts_search call), and `CODE_RAG_NO_RERANK=1` (return RRF-sorted top-K). Run `bench_routing_e2e.py` jira n=908 four times: fts-only+rerank, vector-only+rerank, fusion+rerank (current), fusion+norerank. The matrix shows: (1) which leg's pool@K=200 already contains GT (recall ceiling per leg), (2) how much rerank rescues vs how much it churns, (3) whether fusion helps over the better leg. **Predicted finding**: FTS5-only recall@200 on jira will be > current hit@10 of 0.535. If true, our bottleneck is "GT is in the pool but rerank loses it" — a totally different fix surface than "model swap".
- **failure mode (1 concrete way it could fail to deliver)**: Both legs equally weak (each <40% recall@200) — diagnosis would be "hybrid is genuinely the limit; need new retrieval signal" (e.g., AST symbol search, repo-structure expansion). Still actionable: it points at *adding* a leg, not tuning the existing two.

---

## SY3: Decouple recall stage from ranking stage with a hard contract

- **rank**: 3
- **bottleneck identified**: The current pipeline conflates *recall* (get the right files into the candidate set) and *ranking* (order them well). FTS pool=150, vector pool=50, RRF merge → ~200 candidates → rerank → top-10. But `hybrid_search` mixes content boosts, code_facts injection, env_var boost, and TASK_BOOST INTO the RRF score — the same number that decides who survives the cut to the rerank pool of ≤200. So a chunk with great content match but bad RRF rank can be cut before it ever sees the cross-encoder. Conversely, a TASK_BOOST=1.1 multiplier can promote a junk chunk past a real GT chunk that was rank 200. This is the "everything everywhere all at once" anti-pattern that sub-bugs hide inside.
- **justification (2 sentences)**: Two-stage retrieval (pure recall → pure precision) is the dominant industry pattern for a reason: failure modes are isolatable. Today, when a query misses, we cannot tell *which stage lost the GT* — was it FTS pool truncation at 150? RRF dilution? Boost mis-ordering before the rerank cut? Stratum gate skipping a query that needed rerank?
- **experiment to validate**: Build a single instrumented bench that, per query, logs `gt_in_fts150`, `gt_in_vec50`, `gt_in_rrf_top200`, `gt_in_rerank_input`, `gt_in_top10_post_rerank`. Aggregate over jira n=908. The drop-off curve tells us *exactly* where the system loses recall. Hypothesis: most losses concentrate in one transition (most likely "GT in rrf_top200 but lost after rerank+penalties+sort", which would point at penalty over-firing). Once we know that, the fix surface narrows from 5 stages to 1.
- **failure mode (1 concrete way it could fail to deliver)**: The losses are uniform across stages (10pp per transition) — diagnosis is "system needs end-to-end rebuild, not point fix". Still better than the current darkroom: at least we'd know we are not on the "one bug fix away from 60%" path. Mitigation: the instrumentation itself is permanent value — we keep it for every future A/B.

---

## What I'd VETO

- **Another reranker model swap or fine-tune.** Seventeen rejections, ±3pp ceiling, costs tens of dollars per run, and we now know from FTS5-fix data that the dominant losses are NOT at the rerank stage. Any proposal of the form "what if we tried <new CrossEncoder>" should be rejected until SY1+SY2+SY3 are completed and they identify the rerank stage as the lossiest. Until then, every dollar on a reranker is a dollar we don't spend on diagnostics.

- **Metric refactoring (MRR, R@K=24, "useful precision") before the diagnostic is done.** hit@10 is a perfectly good *change-detector* — its absolute value is debatable but its delta tracks fixes. Switching to a new metric mid-debugging resets all the baselines, lets us re-grade past rejected candidates as wins, and adds a new failure mode ("did the fix help, or did the metric just rephrase the same number?"). Once SY1-SY3 land, *then* a metric audit (with calibration on the same 50 hand-labeled queries) is appropriate. Not before.
