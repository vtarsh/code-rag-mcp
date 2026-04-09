# MCP Development Pattern

## Core Rule
Measure first, build second, validate third.

## Capability Ladder (do NOT skip rungs)

1. **Gotcha** (1-5 lines, zero code) → indexed by search, found when relevant
2. **Process doc** (investigation stages, anti-patterns) → questions, not answers
3. **Convention** (co-change rules, domain patterns in conventions.yaml) → feeds existing tools
4. **Tool mode** (new mode on existing tool) → extends proven tool
5. **New tool** (only when demand proves Rung 4 insufficient)
6. **Pipeline** (pre-computation infrastructure) → ALMOST NEVER JUSTIFIED

Promotion: 3+ sessions need it → promote one rung.

## Pre-Flight Checklist

Before any MCP improvement:
1. What specific problem? (link to session log)
2. How many real sessions hit this? (> 3 = worth solving)
3. Can a gotcha solve it? (yes → write gotcha, stop)
4. What rung? (start lowest)
5. 3 test cases? (diverse, including negative)
6. Pre-computes answers or provides navigation? (answers → reject)
7. If code changes tomorrow, does this become wrong? (yes → reject)
8. 200-line budget? (more → split scope)
9. Benchmarks that must not regress?
10. When to check if proved value? (1 week)

## What to Pre-compute vs Not

**Pre-compute**: topology, navigation pointers, co-change patterns, verified behavioral facts, process guides, recipes
**Do NOT**: field mappings, type definitions, validation schemas, status enums, gap conclusions

Heuristic: if someone changes one file and this data becomes wrong → don't pre-compute.

## Nightly Refresh
- FTS5 index, dependency graph, vectors, benchmarks → auto
- Gotchas, recipes, process docs → manual (stable)
- Co-change rules → weekly re-mine

## Sync vs Async (ALWAYS check)
When analyzing any change: is this sync only, or does it have async path (webhook → Temporal)?
Same code (map-response, statuses-map) may be called from BOTH paths.

## Feedback Loop
Session → tool_calls.jsonl → analyze_session_quality.py (weekly) → identify struggling patterns → write gotcha → validate → deploy
