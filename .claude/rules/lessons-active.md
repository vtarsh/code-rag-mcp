# Active Lessons & Rules

## Search & Recall

1. Don't lower similar-task overlap threshold below 3 — causes more false positives than true positives.
2. Re-ranker is a polish step, not a fix for missing mechanisms. Always improve base recall first.
3. Hub penalty is a UX fix (less noise), not a metric fix. Do it for output quality, not benchmark numbers.
4. Suppress ALL provider detection for CORE- prefix tasks — prevents misclassification.
5. `pkg:@pay-com/X` virtual nodes must resolve to actual repos (e.g., `pkg:@pay-com/core-X` -> `grpc-core-X`).
6. kafka-cdc-sink is a co-change pattern — always changes with libs-types but no static dependency.
7. Data-flow dependencies through shared entities are invisible to static graph analysis.

## Ground Truth & Benchmarks

8. Ground truth = repos_changed from Jira, NOT repos_needed. Always report WHICH ground truth is used.
9. Phantom filtering (repos with 0 files_changed) should be the primary metric.
10. Never trust repos_changed blindly — cross-validate with files_changed and pr_urls.
11. After bulk collection, always analyze data shape (type distribution, empty fields, group distribution).
12. Expect recall to shift after bulk collection — more data reveals true weaknesses.

## Deep Analysis

13. Tier 3-4 finds 80% of issues. Tier 1 needed for the remaining 20% that Tier 3-4 gets wrong.
14. Independent grep + tool comparison is the most valuable diagnostic combination.
15. Overnight autonomous batching: 3 agents per batch, pattern mine every 10 tasks, cron every 30 min.

## Impact Audits

16. Don't fix what you haven't seen in sandbox. If sandbox doesn't reproduce it, defer to e2e testing.
17. Check platform generic handling (expiration workflows, generic callbacks, proto enums) BEFORE flagging gaps.
18-20. Severity calibration, API format verification, missing repos verification — see `impact-audit.md` (severity table + anti-patterns).

## Process

21. Don't report context percentages — just say "continuing" or "plenty of room" without numbers.
22. Don't create watchdog crons that only report. Make them ACT or don't create them.
23. When setting up overnight work, add recurring "continue" cron (every 30-60 min) that picks next item.
24. Always verify data collection tools are actually working. Silent failures are the worst kind.
25. When a tool is fixed/created, immediately document it in the relevant .claude/rules file.
26. Independent validation agents MUST NOT use MCP tools. Verify agent output has zero `mcp__pay-knowledge__*` calls.
