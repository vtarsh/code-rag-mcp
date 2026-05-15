# Refutation pass — D devil's advocate

Source data: `bench_runs/jira_e2e_wide_off_session2.json` (n=908, hit@5=35.79%, hit@10=41.63%, R@10=7.05%, nDCG@10=13.84%).
DB: `db/knowledge.db` (29,682 unique (repo, file_path) pairs in `chunks`).

## Excluded (refuted)

- **H1 (GT cardinality cap drives R@10/hit@10 floor)**: REASON: Bigger-GT bucket has HIGHER hit@10, opposite of cap-driven prediction (small N≤10 → 30.7%, large N>10 → 50.2%). Even capping R@10 numerator at min(|GT|,10) only lifts the metric to 11.24% — still 5.6pp below structural ceiling 66.79%, so cap is not the binding constraint. KILLER EVIDENCE: `python3 -c "import json; d=json.load(open('bench_runs/jira_e2e_wide_off_session2.json')); print('cap-norm R@10:', sum(round(r['recall_at_10']*len(r['expected_paths']))/min(len(r['expected_paths']),10) for r in d['eval_per_query']) / len(d['eval_per_query']))"` → 0.1124.
- **H2 (vague PR-title info-floor)**: REASON: hit@10 across token-count buckets is essentially flat (38.8% / 41.5% / 44.5% / 28.6% on 1-3 / 4-7 / 8-12 / 13+ tokens). KILLER EVIDENCE: code review of `fts.py:108` (`sanitize_fts_query`) confirms `_STOP_WORDS` is NOT applied on the `hybrid_search` path — only `sanitize_fts_with_stop_words` (used in `tools/context.py`) drops stop words. H2's stated mechanism does not exist on the production path.
- **H4 (two-tower mis-routing blanks pool)**: REASON: replicated `_query_wants_docs` over 908 queries — 718 (79%) route to docs_only tower. Hit@10 by routing: docs_only=41.23%, code_only=44.23%, merged=38.24% — only **3.7pp delta**. Maximum lift if all queries forced to code tower = 0.737 × 3.7pp ≈ 2.7pp — cannot explain 58% miss floor. KILLER EVIDENCE: FTS5 leg over unified `chunks` table (hybrid.py:690) compensates for vector mis-routing.
- **H5 (FTS5 OR-bag tokenization differential)**: REASON: replicated `sanitize_fts_query` over all 908 queries. KILLER EVIDENCE: mean tokens kept misses=5.59 vs hits=5.53 (Δ +0.06 — statistical zero). 0/530 misses with ≤1 token; 1/378 hits with ≤1 token. Tokenization is not differential between hits and misses. H5 premise (stop-word strip leaves 0-1 informative tokens) was based on the wrong sanitizer function.
- **H7 (stratum gate skips reranker on code-intent)**: REASON: gate fires on 42/908 queries (4.6%). KILLER EVIDENCE: `python3 [replicate _detect_stratum + _should_skip_rerank]` → 42 queries with stratum∈{webhook,trustly,method,payout} AND doc-intent. Hit@10 on those 42 = 26.2% vs 41.8% baseline → max lift if gate disabled ≈ 42 × 0.156 ≈ 0.72pp. Too small to drive 58% floor.
- **H8 (RRF pool depth ≤200 caps GT)**: REASON: existing pool sweep `bench_runs/sweep_l12_pool{50,100,200,300}.json` shows IDENTICAL hit@10=0.6125, R@10=0.1869 across all 4 pool sizes. KILLER EVIDENCE: `python3 -c "import json; [print(n, json.load(open(f'bench_runs/sweep_l12_pool{n}.json'))['hit_at_10']) for n in [50,100,200,300]]"` → 0.6125 / 0.6125 / 0.6125 / 0.6125. Pool depth has zero measured effect.
- **H10 (code_facts cross-repo injection displaces GT)**: REASON: arithmetic — injected RRF = (KW_WEIGHT × 0.5)/(K+1) = 0.0164 max; FTS5 RRF = KW_WEIGHT/(K+1) = 0.0328. KILLER EVIDENCE: injected candidates ALWAYS rank ½× below FTS5 at the same rank — cannot displace top entries; only fills bottom of 200-cap pool. Backoffice-web GT with provider-* top-1 occurs in only 2/287 single-repo misses — H10's posited displacement pattern does not match data.
- **H11 (semantic gap CodeRankEmbed vs PR-title English)**: REASON: top-1 combined_score Δ misses-vs-hits = -0.049 (small magnitude despite t=4.22 significance). KILLER EVIDENCE: memory note `project_loop_2026_04_25.md` — 17 candidate-model A/Bs all REJECTED. Model swap doesn't fix the floor; even if H11's mechanism is real, the fix path is exhausted. No direct (query, GT-chunk) cosine measurement available, but the search space has been thoroughly explored without lift.

## Open (unrefuted)

- **H3 (doc files dominate top-1 — penalty bypassed for 82% of jira queries)**: unrefuted because of two unfalsified mechanism claims:
  1. Among MISSED queries, code-intent path (penalty applied, n=89) yields 4.5% doc top-1; doc-intent path (penalty skipped, n=441) yields **41.5% doc top-1** — 9× difference.
  2. Of 187 missed-with-doc-top-1 queries, only 23 (12%) have any docs/.md in their GT — i.e. 88% are SPURIOUS doc returns where the user wanted code.
  Specific unrefuted reason: no production A/B with `CODE_RAG_DOC_PENALTY=0.5` AND `apply_penalties=True` forced exists in `bench_runs/` to confirm the predicted ≥3pp lift. Mechanism is consistent with all observed evidence and survives every refutation attempt.
- **H6 (index/eval drift)**: PARTIALLY confirmed — UNREFUTED for ~10% of floor scope. KILLER EVIDENCE: 86 queries (9.5%) have ZERO indexed GT (mathematically unhittable; confirmed 0/86 hit@10). REFUTATION on dominance: indexable subset (n=822) hit@10=45.99% — only 4pp above global, and 100%-indexed bucket (n=103) STILL misses 65% of the time. So drift caps R@10 ceiling but only ~10% of the hit@10 floor. Open with bounded scope; 444/530 misses are ranking-driven not coverage-driven.
- **H9 (boost multipliers GOTCHAS=1.5 / REF=1.3 / DICT=1.4 promote curated-knowledge over production code)**: unrefuted because of three unfalsified mechanism claims:
  1. Boosts apply at hybrid.py:817-824 to RAW RRF score BEFORE normalization, BEFORE rerank.
  2. Penalties (DOC_PENALTY=0.15, etc.) subtract from NORMALIZED [0,1] post-rerank space (hybrid.py:550-552) — and 82.3% of jira queries SKIP penalty entirely (apply_penalties=False on doc-intent classification).
  3. Arithmetic check: a gotchas chunk at FTS5 rank 30 (RRF=0.022) gets 1.5× boost → 0.033, beating unboosted code chunk at rank 5 (RRF=0.030).
  Specific unrefuted reason: no `bench_runs/` A/B exists with `gotchas_boost=reference_boost=dictionary_boost=1.0` set to falsify the predicted lift. Mechanism reproduces the empirical doc-top-1 over-representation seen in H3.

## Cross-cutting falsification signals D recovered

1. **The 444 indexable-but-still-missed queries (444/530 = 84% of misses) are RANKING failures, not COVERAGE failures.** This kills H6 as the dominant cause and points to upstream signal-direction (H3+H9) issues.
2. **Gate / pool / model swap have all been measured at zero or sub-1pp lift.** Killing H7, H8, H11 as actionable.
3. **Code-intent vs doc-intent split is the ONLY single decision branch with a 9× variance in doc top-1 frequency.** This makes H3+H9 the residue after all other refutation attempts.
4. The 222/530 (42%) wrong-repo misses are a separate failure mode not fully addressed by H3+H9 alone — the cross-repo top-1 is not from code_facts injection (H10 refuted) so its origin is open. May be a sub-mode of H3 (doc-tagged provider-doc files outrank code) but warrants independent test.
