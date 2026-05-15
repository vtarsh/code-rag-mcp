# Round 2 — Systems-Thinker (cross-critique of DE + RE)

## Data-Engineer

**DE's position summarized in my words (≤3 sentences):** Every move you make on the ranker is a sandcastle until the eval and index are clean: 57.9% of GT files aren't in the index, 25.4% are mechanical PR noise (`package-lock.json` etc.), and the jira-titles distribution doesn't match real prod queries anyway. The three ordered moves are DE1 rebuild eval against actually-indexed HEAD (drop unhittable + noise), DE2 relax the extractor to cover `.sql/.graphql/.json/.yml/.cql` so the R@10 ceiling lifts, DE3 pivot the primary eval to clustered prod queries from `tool_calls.jsonl`. Forced pick = DE1 first because it costs 4 hours and exposes whether the 53.5% plateau is a real ceiling or a measurement artifact.

**Strongest point:** DE1's "57.9% of GT not in index" is a hard ceiling on what hit@10 can ever reach — that's a Shannon-bound argument, not a tuning argument. Same shape as my SY thesis: the bottleneck is a *signal-loss site*, not a score function. DE quantified the upstream loss; my SY3 (stage-instrumented bench) couldn't have done that because the chunk-not-in-index loss happens BEFORE my instrumentation point.

**Weakest point:** DE3 (pivot to prod queries as primary eval) is correct in principle but introduces a label-quality risk that DE itself flags ("read-after-search heuristic, weak signal"). Doing DE3 BEFORE DE1+DE2 close the gap means optimizing against an even fuzzier signal — proxy-labels from session windows + Opus judges have known biases (memory `feedback_code_rag_judge_bias.md`: judges aren't neutral on code RAG). DE3 should be the *companion* eval, not the primary, until we have a calibration method that isn't itself biased.

---

## Researcher

**RE's position summarized in my words (≤3 sentences):** The codebase has been A/B-stressed on the same family of techniques (rerank-FT, two-tower, glossary, boost/penalty/RRF) and the unexploited signal is repo-level concentration: 85.4% of jira queries put ≥50% of GT in one repo, but the 150-candidate pool competes 80+ repos. Three ordered moves are RE1 soft repo prefilter (boost top-3 prefilter repos ×1.4 in fusion, +6-12pp expected, $0), RE2 Doc2Query (offline RunPod chunk→synthetic-query expansion, $5-15, +3-7pp), RE3 code-aware FTS5 tokenizer that splits camelCase/snake_case/dotted identifiers (+2-5pp). Bottom line: bundle RE1+RE3 as the next ship, queue RE2 after.

**Strongest point:** The 85.4%-concentration finding is the highest-leverage UNEXPLOITED signal in the corpus — it's both empirically verified (deterministic data property, repro `repro_h6.py` confirmed 775/908=85.4%) AND structurally orthogonal to everything we've tried (rerank/two-tower/glossary all operate on chunks; nothing operates on repo-level priors). RE1's soft-boost framing also defends against the obvious failure mode (multi-repo tickets) which is the right engineering instinct.

**Weakest point:** RE2 (Doc2Query) is positioned as cheap because "RunPod stage-A/B infra exists", but memory `project_training_cycle_failure_2026_04_26.md` shows that infra burned $4.50 with 5 infra bugs and 0 valid candidates two days ago. Re-using it for ~76k-chunk batch generation isn't a cheap drop-in; it's a re-engagement with a known-flaky pipeline. RE2 estimate ($5-15, ~6h on A40) is optimistic and the failure-mode mitigation (Gospodinov-2023 filter) adds another moving part. RE3 is also softer than RE estimates: `sanitize_fts_query` already splits dotted tokens (`fts.py:136`), so the camelCase win is partially shadowed by what's already there.

---

## Updated ranked list

Round-1 SY1 was bench-prod parity. Round-1 reproduced it (H5 confirmed-magnitude on n=50: −6.0pp hit@10 from live `expand_query`). That ship-time fix banks ~6pp of real-traffic recall for free, but does NOT close the bench's 53.5%→60% gap because the bench already excludes `expand_query`. Round 1 SY2 (stage-isolation A/B) and SY3 (stage-instrumented bench) are now SUBSUMED into a stronger plan that DE provided: if 57.9% of GT is missing from the index, my "which leg dominates" question is downstream of the wrong question. The lossiest stage is index-build, full stop.

**Updated ranking:**

1. **DE1 (eval rebuild against actually-indexed HEAD).** Promoted to rank 1 over my SY1. Reason: SY1 audits prod-vs-bench drift; DE1 audits bench-vs-truth drift. Both are diagnostic-before-fix, but DE1 is a STRICTLY larger drift (58pp upstream loss vs my 6pp prod-vs-bench gap), and SY1's strip-the-call fix has *already shipped* on the hypothesis side (H5 confirmed-then-excluded with repro). DE1 gives us the honest baseline against which RE1+RE3 are measured — without it, we cannot tell whether RE1's predicted +6-12pp is signal or eval-noise.

2. **RE1 (soft repo prefilter).** Promoted to rank 2 over my SY2. Reason: SY2 (stage-isolation A/B) was my answer to "where does GT die in the pipeline"; RE1 directly addresses the dominant signal-loss site (wrong-repo competition for pool slots) AND it's structurally orthogonal to everything we've tried. The 85.4% concentration is empirically verified. RE1 is the right structural fix even if SY2 stage-isolation reveals FTS5-only @K=200 already has GT — see explicit answer below.

3. **DE2 (index relax for `.sql/.graphql/.json/.yml/.cql`) bundled with RE3 (code-aware FTS5 tokenizer + path-as-doc).** Both touch the index-build stage; both add coverage to the upstream candidate set; both ship together to amortize one re-index cycle. DE2 raises the recall ceiling (the numerator); RE3 makes existing chunks more findable (the matcher). Rank 3 because the lift is bounded by what DE1's clean baseline reveals — if DE1 shows the clean ceiling is already 65%, RE3+DE2 land us at goal cheaply; if DE1 reveals a 75% clean ceiling, we don't need DE2 at all.

**Demoted from my Round 1:**
- **SY1** → already executed (H5 repro confirmed −6.0pp on n=50, ship-time fix banked).
- **SY2** → demoted to OPTIONAL diagnostic. Run it AFTER DE1 produces a clean eval (otherwise we're A/B-ing leg dominance against a 58%-noise eval, same anti-pattern DE warns against).
- **SY3** → demoted to OPTIONAL. The stage-instrumented bench is good A/B infrastructure but not gating once DE quantifies the upstream chunk-loss; the dominant transition is now known to be "GT file not in `chunks` table".

**Demoted from DE+RE:**
- **DE3 (prod-eval pivot)** → bank as future-work, not next-cycle. Label quality not yet calibrated; doing it before DE1+DE2 close the index gap puts us on a fuzzier signal in addition to the existing fuzzy signal.
- **RE2 (Doc2Query)** → bank as future-work. RunPod re-engagement risk + Gospodinov-filter complexity > expected lift unless RE1+RE3+DE2 land under +6pp combined.

---

## Specific responses to team-lead's prompts

### "DE1 (eval rebuild): does it BLOCK your bench-parity audit, or does SY1 give honest diagnosis even on noisy eval?"

**DE1 does NOT block SY1, but it dominates SY1.** SY1 was the most cost-effective experiment in my Round 1 set (1 hour, three-mode bench), and it has now been executed: H5 repro on n=50 shows live `expand_query` is bleeding −6.0pp hit@10 from real prod traffic. That diagnosis is honest *because the diff is internal to the bench*: even on a 58%-noise eval, the relative comparison `(a)−(c)` is a pure delta on the same noise floor, so the delta is unbiased (the noise cancels). What SY1 *cannot* tell you is whether the absolute 53.5% is a real ceiling or eval drift — and that's exactly what DE1 answers. Conclusion: **SY1 was strictly cheaper and complete; DE1 is strictly more important and remains rank 1.** They aren't competitors, they're sequential.

### "DE3 (pivot to prod queries): is this a methodology shift you'd endorse?"

**Endorsed in principle, not as next-cycle work.** DE's argument is correct: optimizing for jira PR titles when prod queries look like `trustly verification webhook` is generalization-by-luck. The methodology shift is real and necessary. But two blockers stop me from endorsing it for THIS cycle:
1. **GT label quality**: read-after-search proxy is a weak signal — the user might Read a file because the search MISSED what they wanted, not because it found it. Without click data, the label is noisy in the same way jira PR-titles are noisy, just differently noisy.
2. **Calibration risk**: Opus + MiniLM dual-judge has known asymmetry (memory `feedback_code_rag_judge_bias.md`: code-RAG judges are never neutral). Using them as the calibration ground truth means the prod-eval inherits judge bias.

**My endorsement form**: bank DE3 as parallel-track work (ship as `prod_eval_n500` companion), but keep clean jira-DE1 as primary until we have a 50-query gold-labeled overlap calibrated against a non-judge ground truth (e.g., user click telemetry if/when we wire it).

### "RE1 (repo prefilter): if SY2 stage-isolation reveals FTS5-only @K=200 already has GT, does RE1 become redundant? Or is RE1 the right structural fix even when both legs work?"

**RE1 is the right structural fix EVEN IF FTS5-only @K=200 already has GT.** The reason: "GT in the K=200 pool" is necessary but not sufficient. The K=200 cut feeds the rerank pool, where penalties + boosts + cross-encoder collapse it to top-10. The reranker's task is to discriminate among 200 candidates that mostly look right tokenwise but are from 80+ different repos. Even with a perfect cross-encoder, repo-level priors are *information the cross-encoder doesn't see* (it only scores chunk content, not "is this the repo the user probably means"). RE1's soft repo-boost ×1.4 injects that prior at the fusion stage where it can actually shift the rerank pool composition. If FTS5@K=200 already has GT, RE1 raises the GT chunk's RRF rank within the pool, increasing its survival probability through the rerank cut to top-10.

A stronger framing: hit@10 = P(GT in pool) × P(GT survives rerank | in pool). FTS5-only @K=200 ⇒ first factor is ~1. RE1 attacks the second factor by making the rerank pool's repo distribution match the query's repo prior. That's an attack on the *ranking* problem, but it's a structural one (priors), not a tuning one (boost knobs). It doesn't compete with rerank-knob tweaks; it operates in a different dimension. **Verdict: RE1 stays rank 2 unconditionally.**

---

## What I'd VETO in Round 2

- **Any "skip DE1, go straight to RE1+RE3" proposal.** DE's eval-noise argument is strong enough that shipping RE1 (predicted +6-12pp) without DE1 would let us claim a win that's anywhere from +6pp signal to +6pp absorption-of-noise. The 4-hour DE1 cost is cheaper than the cost of a wrong verdict on a +12pp ship.

- **Any "DE3 first, before DE1" proposal.** Pivoting to a prod-eval before the index/bench is honest stacks two unknown noise sources and we'll lose the ability to attribute movement.

- **Any reranker fine-tune in this cycle.** This veto stays from Round 1, with DE's evidence reinforcing it: cross-domain transfer fails on docs (P9), payfin-v0/v1-fixed -10.8/-8.3pp, mxbai latency-breach. Adding a model investment on top of a noisy eval doubles the noise budget.
