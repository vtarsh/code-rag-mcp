# Round 2 — Data Engineer cross-critique

## Systems-Thinker (SY)

### Position summarized
SY argues every win since the FTS5 sanitize fix has been ±3pp ranking-layer noise because the *real* losses are silent signal-loss sites upstream (pool truncation, RRF dilution, bench-prod drift). Their three moves are diagnostic, not corrective: SY1 audits `expand_query` bench-prod parity, SY2 isolates per-leg recall@K=200, SY3 instruments the pipeline to localise the lossy stage. They explicitly veto another reranker swap and any metric refactor before diagnostics land.

### Strongest point
**SY1 (bench-prod parity audit).** It's the cheapest experiment in the entire debate (one bench under three flag configs, < 1 hour of compute, zero risk to anything in the ranking stack), and it has a non-noise-bandwidth payoff: the W2 cross-evidence already says glossary expansion costs −9.71pp on jira, and prod runs that glossary unconditionally while bench bypasses it. If SY1 confirms the inversion, ripping `expand_query` from `service.py:68` is a >5pp prod lift with zero ranker change. The point is also strong in a meta sense: it certifies the validity of every subsequent A/B — without bench≡prod, every "+Xpp on jira" claim is ambiguous about whether the user actually feels it.

### Weakest point
**SY3 (5-stage instrumentation).** The premise — "we don't know which stage loses GT" — has a partial answer already from prior session2 work: 57.9% of GT is missing from the chunks table (upstream of FTS150/vec50/rerank). That bounds the maximum information SY3 can recover to the residual ~44pp of recall budget *after* the chunk-pool drift. It's worth doing for permanent diagnostics, but as a +6.5pp-to-goal driver it is not load-bearing — by SY's own framing, you can't recover signal that never entered the pipeline. The instrumentation has lasting value as a regression guard, but it doesn't change the strategic ranking.

---

## Researcher (RE)

### Position summarized
RE measured eval-shape directly and found three under-exploited signals: (1) 85.4% of jira queries concentrate ≥50% of GT in one repo while the pool competes 80+ repos, (2) 24.4% of GT paths contain camelCase tokens that the `porter unicode61` FTS5 tokenizer never splits, (3) PR-title queries are short English while indexed code is identifier-shaped. Their three moves attack all three: RE1 soft-boost-prefilter on predicted top-3 repos, RE2 Doc2Query offline synthetic-query column, RE3 code-aware tokenizer + path-as-document column. They reframe the user's grep+synonym intuition as Doc2Query/SPLADE rather than hand-curated YAML.

### Strongest point
**RE1 (repo prefilter, soft).** It exploits the largest measurable structural signal in the eval (85.4% single-repo concentration) and its failure mode (top-3 prefilter misses correct repo) is mitigated by soft-boost rather than hard-filter, so the worst case is no worse than baseline on the 14.6% multi-repo queries. The tractability is excellent: zero training cost in V1 (BM25 over per-repo README+code_facts summaries), 6-10h dev, $0 RunPod. Lift estimate +6 to +12pp is the only Round-1 candidate with a credible path to closing the +6.5pp gap to 60% on its own. The soft-boost-not-hard-filter design choice is the kind of robustness move that distinguishes "RE actually thought about edge cases" from "RE pattern-matched a paper".

### Weakest point
**RE2 (Doc2Query offline).** The cost-benefit is worse than RE1+RE3 combined: $5-15 RunPod, 16h dev, full re-index, +3-7pp expected. More importantly, the failure mode (hallucinated synthetic queries inflating FTS5 match counts) compounds with the FTS5 5-extension allowlist gap I identified — Doc2Query generates synthetic queries for chunks that exist in the index, but does NOTHING for the 57.9% of GT pairs that aren't in the index at all. So RE2 lift is upper-bounded by the indexable subset. RE2 also re-introduces the auto-generated-content brittleness that has bitten this project before (P1b judge bias, training-data leakage). Defer to RE2 only after eval is rebuilt and DE2-style coverage gaps are closed.

---

## Specific compositions you asked about

### SY1 vs DE1 — block or compose?
**Compose, do SY1 first.** SY1 is a prerequisite for DE1, not a substitute. DE1 (rebuild eval against indexed HEAD + drop GT noise) only matters if the eval-bench output we're optimizing on actually corresponds to what production serves. SY1 closes that loop in <1h; if it shows bench is materially above prod (likely, given W2 cross-evidence), then DE1's "honest eval" must include the bench-prod parity fix or DE1 will optimize for an artifact of the bench script rather than the production pipeline. They are sequential: SY1 (1h) → DE1 (4h). SY1's cost is small enough that running it before DE1 is the obvious move, and the result either validates the current bench (in which case DE1 proceeds unmodified) or invalidates it (in which case DE1 absorbs the parity fix as part of the rebuild).

### RE1 vs index rebuild — layered or substitute?
**Layered. RE1 sits ON TOP of clean eval, NOT in place of index work.** RE1's expected +6-12pp lift is computed under the assumption the ranker fights for the right repo within a fixed-size pool that already contains the GT files. But 57.9% of GT is missing from `chunks` entirely; soft-prefilter of `backoffice-web` doesn't help if `src/Components/ContactList/ContactList.tsx` (cited by BO-1041) doesn't exist in the current shallow-cloned HEAD. RE1's real lift on the *current* eval will be biased — it will look strong on the indexable subset and weak on the unhittable subset, with the per-stratum split obscured by the noise. Sequencing should be: (1) DE1 rebuild eval to indexed-only + denoised so RE1's measurement is honest; (2) deep-clone the top-8 repos (one-shot data fix, addresses the shallow-clone GT drift) so RE1 has correct ground truth in the corpus; (3) implement RE1 soft-boost. Without (1)+(2), RE1's bench number will under-report its true value and we may reject a ship-able fix.

### RE2 vs DE2 — obsolescence or complement?
**Complement, but order matters: DE2 first, RE2 later (or never).** RE2 (Doc2Query) generates synthetic queries for chunks that ARE in the index — it cannot index a `.sql` migration file or a `.graphql` schema that the extractor never copied. DE2 (relax `extract_artifacts.py:60-90` allowlist for `.sql/.graphql/.json/.yml/.cql`) is the prerequisite that puts those file types into the corpus where Doc2Query could later expand them. After DE2, the choice between glossary-expansion (W2-style) and Doc2Query becomes meaningful — but Doc2Query's marginal value ALSO depends on whether DE3 (pivot to prod query distribution) shows that prod queries are short-and-code-shaped, in which case Doc2Query's "expand chunks with English questions" is solving a problem prod doesn't have. So the conditional is: DE2 first (deterministic), then re-measure on prod-eval (DE3); if the residual gap is "English PR-title queries can't reach code-identifier chunks", THEN RE2 makes sense. If the gap is elsewhere, RE2 is wasted compute.

---

## Updated DE ranked list

| # | move | status | reason |
|---|---|---|---|
| 1 | **SY1 bench-prod parity audit** | ADOPT (insert before DE1) | <1h cost, gates honest measurement of every subsequent move including DE1. Cheap-and-load-bearing. |
| 2 | **DE1 eval rebuild (indexed + denoised)** | KEEP | After SY1, this is the foundation of every other move. Without it, RE1/RE2/RE3 magnitudes are biased and the ranker tweaks people will keep proposing will keep ±3pp-ing. |
| 3 | **RE1 soft repo prefilter** | ADOPT (after DE1) | Largest measurable structural signal (85.4% single-repo concentration). Highest ceiling among ranker-stage moves. Soft-boost mitigates the failure mode. **My DE2 was rank 2; RE1 supersedes it as the next +pp move because RE1 is cheaper and addresses pool-competition that index relax does NOT solve.** |
| 4 | **DE2 extractor relax (extension+dir allowlist → broader)** | KEEP (rank-down) | Still needed: 0%-indexed extensions (.sql/.graphql/.json/.yml) cannot be ranked into top-10 if not present. But headroom is bounded by what GT references the missing extensions actually retrieve — RE1 likely captures most of the same signal cheaper for the indexable subset. Run DE2 only if RE1+SY1 leave a clear residual on `.sql/.graphql/.cql` strata. |
| 5 | **RE3 code-aware FTS5 tokenizer + path-as-document** | ADOPT | Cheap (4-6h, $0), additive to RE1 (different stage), and the 24.4% camelCase-in-path measurement is a clean unexploited signal. The path-as-document column is the highest-leverage sub-piece — even without splitting, indexing `repo_name + file_path` at high weight directly addresses the "FTS5 matches content but not paths" miss class. |
| 6 | **DE3 prod-eval pivot (`tool_calls.jsonl`)** | KEEP (parallel track) | Distribution mismatch is real (prod 39.6% ≤3 tok / 30.2% code-shape vs jira 71.9% mid-length / 23.5% code-shape). Independent of every other move; can run parallel with SY1+DE1. After RE1+DE1 land, prod-eval becomes the gating metric, not jira-eval. |
| 7 | **SY2 stage-isolation per-leg recall@200** | KEEP (low priority) | Cross-evidence from two-tower v13 already bounds inter-leg dominance to ≤3pp. Run only if RE1+RE3+DE1 land and there's a residual gap that needs decomposition. |
| 8 | **SY3 5-stage instrumentation** | KEEP (permanent infra) | Build it as a side project for permanent regression-guard value, but do NOT block any of the above on it. Diagnostics are not a fix. |
| 9 | **RE2 Doc2Query offline** | DROP for now | Conditional on DE2+DE3 landing first. Re-evaluate only if RE1+RE3 leave a gap shaped like "English PR-title can't reach code-identifier chunks". Until then, the $5-15 + 16h is better spent on RE1+DE1+SY1 closure. |
| — | Glossary growth (W2-style) / equivalence-class index (W4) | DROP permanently | Both 2015-era IR. RE's framing is correct: synonym discovery should move to the index (Doc2Query / SPLADE), not the YAML. |
| — | Reranker model swap or new FT cycle | VETO (unchanged from Round 1) | 17 rejections; ±3pp ceiling; biased eval encodes into weights. |
| — | Boost/penalty/threshold tuning | VETO (unchanged from Round 1) | 6 sessions of ±3pp noise; rotation in noise space. |

## Sequencing recommendation

Phase 1 (this week, <2 days total):
1. SY1 bench-prod audit (1h)
2. DE1 eval rebuild (4h, depends on #1)
3. DE3 prod-eval scaffold (parallel with #1+#2)

Phase 2 (next 1-2 weeks):
4. Deep-clone top-8 repos to fix shallow-clone GT drift (1-2h compute, addresses 953 missing src files in `backoffice-web` from PR-historical refactors)
5. RE1 soft repo prefilter (6-10h)
6. RE3 path-as-document + camelCase split (4-6h, can ship parallel with #5)
7. Re-bench on the rebuilt clean eval — measure RE1+RE3 combined, expect +6-15pp

Phase 3 (only if Phase 2 leaves residual gap):
8. DE2 extractor relax for .sql/.graphql/.cql (8h dev + 3h compute)
9. RE2 Doc2Query (16h dev + $5-15 RunPod)

Stop conditions: if Phase 2 hits ≥60% on rebuilt eval AND prod-eval shows +Xpp, ship and freeze. If Phase 2 stalls at 55-58%, run SY2/SY3 diagnostics before any further model investment.

## What I changed from Round 1

- **Adopted SY1 as rank 1**, ahead of my own DE1. SY's argument that bench-prod parity gates every subsequent measurement is correct; DE1 without SY1 first is optimizing on a possibly-biased target. The cost is low enough that the inversion is obvious.
- **Adopted RE1 as the primary +pp move**, ranking it ahead of my DE2 (extractor relax). RE1 attacks pool-competition which DE2 does NOT solve, and RE1 is cheaper. DE2 stays in the queue but only as a residual fix after RE1.
- **Demoted RE2** from RE's rank 2 to my rank 9 — the index-coverage gap (DE2) and the prod-distribution mismatch (DE3) need to resolve before Doc2Query is worth the spend.
- **Kept the two VETO categories** (model swap, knob-tweaking) unchanged — neither SY nor RE proposed them, so this is a coherent multi-perspective rejection.
