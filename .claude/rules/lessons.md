# Lessons & Self-Improvement

## Rule: Capture Repeated Instructions

If the user repeats the same instruction, preference, or correction within a session or across sessions — it means it's not documented well enough. Immediately:
1. Save the lesson here (this file) with date and context.
2. Update the relevant .claude/rules/ file so it doesn't happen again.
3. If it's a user preference, save to memory system too.

## Lesson Log

### 2026-03-22: Jira auto_collect was broken and nobody knew
- `auto_collect.py` had `maxResults=50` with no pagination AND silently failed on auth (JIRA_EMAIL missing from launchd)
- Result: only 105 tasks for months instead of 800+
- Fix: cursor-based pagination, JIRA_EMAIL fallback, proper error reporting
- **Rule**: Always verify data collection tools are actually working. Silent failures are the worst kind.

### 2026-03-22: User keeps saying "parallel agents" every session
- Saved to memory + workflow.md: distribute ALL independent work to parallel agents by default, don't ask.

### 2026-03-22: User keeps explaining the validation cycle
- The collect → validate independently → validate via RAG → compare → improve cycle was explained multiple times.
- Now documented in workflow.md.

### 2026-03-22: Bug tickets in Jira
- User notes: unclear if bug tickets (vs Stories/Tasks) are captured. Jira org may use different issue types or just regular Tasks for bugs.
- `DONE_STATUSES` filter may miss bug-specific workflows. Need to verify after collection what `issuetype` distribution looks like.
- **Rule**: After bulk collection, always analyze the data shape (type distribution, status distribution, empty fields).

### 2026-03-22: Document tools and their correct usage
- auto_collect.py with pagination was the fix, but it wasn't documented in .claude as THE way to collect tasks.
- **Rule**: When a tool is fixed/created, immediately document it in the relevant .claude/rules file.

### 2026-03-22: Independent validation agents need tool isolation
- User asked: how do we know "no-RAG" validation agents actually don't use RAG?
- Answer: prompt alone is not sufficient. Agent output shows tool calls but requires manual review.
- **Rule**: For validation cycle, independent agents MUST be launched without MCP tool access. Use `subagent_type` that has no MCP tools, or verify tool call list in agent output. If agent output contains any `mcp__pay-knowledge__*` call — result is invalid, discard and re-run.
- **TODO**: Investigate if `allowedTools` in agent spawn can restrict MCP access per-agent.

### 2026-03-23: Bulk collection results — data shape analysis
- **974 tasks** collected (was 105). BO: 583, CORE: 313, HS: 37, PI: 41.
- **Bug tickets exist**: 184 Bug, 694 Task, 82 Story, 10 Epic, 4 Sub-task.
- **63% have empty repos_changed** (615 tasks) — no GitHub PRs found. Useful for keyword patterns but not recall benchmarks.
- **Recall on expanded dataset**: TOTAL 86.4% (1082/1253). BO 95.1%, CORE 77.8%, HS 100%, PI 83.1%.
- **CORE regression**: 87% → 77.8% — more tasks exposed more misses. This is the priority for pattern mining.
- **BO inflated**: 95.1% likely because many BO tasks are simple (few repos) and similar-task boost works well with 583 tasks.
- **Rule**: After bulk collection, expect recall to shift — more data reveals true weaknesses. CORE is now the priority.

### 2026-03-23: Night audit — pattern mining on 974 tasks
- **PI** (83.6%): Misses concentrated in 3 tasks. `express-webhooks` is a blind spot (8 PI tasks). No generic fix available — these are batch/edge-case tasks.
- **BO** (94.8%): 32% of misses are bulk boilerplate noise. `kafka-cdc-sink` most actionable. Task_patterns table has zero BO patterns — needs rebuild.
- **CORE** (77.8%): Many 0% tasks have empty descriptions AND 0 initial findings. Adaptive threshold (lowering from ≥3 to ≥1 overlap) tested and REVERTED — caused BO false positives without helping 0-finding tasks.
- **Root cause for 0% CORE tasks**: Descriptions too vague (e.g., "Guard running two settlements at once"), no keywords match any domain. FTS can't find similar tasks either. Fix requires richer Jira descriptions or a completely different approach (e.g., developer→repo history prediction).
- **Rule**: Don't lower similar-task overlap threshold below 3 — it causes more false positives than true positives. The bottleneck is finding INITIAL repos, not propagation.

### 2026-03-23: Night audit — implemented 3 improvements, CORE 78%→86%
- **Bulk migration detector** (mechanism #14): keywords like "migrate"/"audit"/"upgrade" trigger enumeration of all service repos. Configured via conventions.yaml `bulk_keywords` + `service_repo_patterns`. +41 CORE repos recovered.
- **core-dispute domain** added to conventions.yaml: keywords [dispute, chargeback, representment, retrieval, arbitration] + seed repos.
- **vault keywords** added to core-payment domain: vault, tokenize, "card vault" + grpc-vault-* repo patterns.
- "collaboration" keyword initially added to dispute domain but removed — too broad, matched "Collaborations Section" in BO tasks.
- **TOTAL recall: 86.5% → 89.5%** on 361 benchmarkable tasks (974 total in DB).

### 2026-03-23: Overnight crons didn't chain — wasted 2 hours idle
- After 04:23 implementation cron completed, session sat idle until 06:37 morning summary.
- Should have: (a) added more crons dynamically after each step completes, or (b) planned a "continue work" recurring cron every 30-60 min that picks up the next roadmap item.
- **Rule**: When setting up overnight work, add a recurring "continue" cron (every 30-60 min) that checks what's done, picks up next item from a TODO list, and keeps working. One-shot crons are too rigid — if one finishes early, time is wasted.

### 2026-03-23: Ground truth quality — repos_changed ≠ repos_needed
- benchmark_recall.py measures recall against `repos_changed` from Jira (repos with PRs matching ticket ID). This is "what devs touched", not "what SHOULD be touched".
- 26% of PI repos_changed are phantoms (zero files changed). Ground truth is noisy.
- **Two metrics needed**: (1) Recall vs Jira — automated, fast, noisy. (2) Recall vs reality — deep validation via grep/code analysis, slow, accurate.
- **Rule**: Always report WHICH ground truth is being used. Don't claim "90% recall" without specifying "vs Jira repos_changed". Clean ground truth (filter phantoms) should be the primary metric.
- **TODO**: Add phantom filtering to benchmark_recall.py as an option (--filter-phantoms). ✅ DONE

### 2026-03-23: Phase 1 deep analysis — 5 PI tasks
- 35% of expected repos are phantoms (0 files_changed). Phantom filtering is essential for honest metrics.
- Independent grep beats tool on 2 tasks: PI-2 (node-libs-common) and PI-21 (workflow-collaboration-processing).
- **node-libs-common miss**: has BLIK in payment-method-types.ts + npm_dep edge from grpc-apm-ppro. Tool should scan npm_dep chain for task keywords.
- **workflow-collaboration-processing miss**: has 12+ chargebacks911 files but ZERO inbound graph edges. Invisible to cascade. Grep finds it trivially.
- **Rule**: Deep analysis agents find real issues that benchmark_recall.py alone doesn't surface. The combination of independent grep + tool comparison is the most valuable diagnostic.
- **Actionable**: (1) scan npm_dep chain for keywords, (2) add Temporal workflow dispatch edges to graph.

### 2026-03-23: PI deep analysis complete — 40/40 tasks
- **166 real repos, tool 95.8%, independent 98.8%** (phantom-filtered)
- 30% of PI ground truth is phantoms (repos with 0 files_changed)
- PI-3 has broken ground truth (both repos unrelated to task description)
- **7 tool misses**: npm_dep ×3, graph_gap ×1, cross-provider ×1, short abbreviation ×1, isolated repo ×1
- **Top fix**: npm_dep chain traversal would fix 43% of all misses (3/7)
- **#2 fix**: reverse cascade from webhook_handler edges (fixes PI-37 crb miss)
- grpc-apm-okto appears as phantom in 5+ tasks — systematic Jira artifact, not real involvement

### 2026-03-23: CORE big tasks expose cascade explosion problem
- CORE-2610 (23 repos), CORE-2581 (26), CORE-2595 (32) — tool returns 200-400+ repos each
- Recall is 100% but precision is 5-6% — useless in practice
- Independent grep achieves 48-68% precision vs tool's 5-14%
- **Rule**: Need a PRECISION metric alongside recall. Returning entire org is not helpful.
- **Rule**: For bulk migration tasks, "list all repos depending on package X" is the right approach, not domain cascade.
- 51% of expected repos in these 3 tasks are phantoms (version bumps only)

### 2026-03-23: CORE deep analysis complete — 95/95 tasks
- Tool phantom-filtered recall: 88.4% (372/421)
- Top miss patterns: co_change_only (17), graph_gap (10), description_missing (5), weak_fts (5)
- kafka-cdc-sink missed 5x — always changes with libs-types but no static dependency
- Workflow repos have 0 inbound edges — cascade can never find them from service repos
- grpc-core-paymentlinks/reconciliation: 16/17 tasks have ONLY package.json bumps — auto-bump repos
- CORE-2102 (0% recall) = banking subdomain with isolated graph, vague description
- **Actionable**: vault domain cluster, kafka-cdc mapper detection, package-only filtering
