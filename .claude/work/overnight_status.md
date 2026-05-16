# Overnight Status — 2026-05-17

## Completed
- EXP2 rerun 3: **hit@10 = 63.91%** (665/665 queries, 0 errors, 32m20s) — first stable full run
- G1 research agents completed:
  - Gotchas/dictionary structure analysis: highly structured, repo names in filenames + inline citations, bidirectional linking
  - Eval pollution analysis: gotchas/dictionary only 1.2% of top-10, not the problem. Real problem: provider_doc + docs = 32.8% of queries, -15.2pp hit rate

## In Progress
- Baseline eval (no EXP2): restarting with correct args + correct path
- CamelCase eval: daemon ready (PID 86436), eval pending after baseline

## Blocked / To Fix
- eval_jira_daemon.py silently drops errors on exception (counts as miss but not recorded) — needs fix
- Docs tower collapse: 83% queries misrouted before fix, still has 83K orphan vectors
- RunPod sync: 56GB fragile, needs archive workflow

## Memory Watch
- Current Python procs: ~6 daemons, RSS ~0.6GB each (VSZ high from mmap, normal)
- Will monitor and kill idle daemons between runs

## Next Combinations to Test
1. Baseline (no EXP2) — running now
2. EXP2 only (63.91% benchmark)
3. EXP2 + camelCase — pending
4. EXP2 + dictionary hints — pending
5. EXP2 + docs tower disable (route all to code) — pending (biggest projected lift: +20-30pp)

## 2026-05-17 03:45 Update
- Baseline eval: 50/665 done, 64% current hit rate
- Memory: 15% free, swap 6.9/8.2GB used — stable
- Daemon RSS: 709MB (normal for model)
- Next after baseline: camelCase eval, then combined combos

## Tiered Search Plan (for agents when user wakes)
### Phase 1: Context Mining (gotchas/dictionary → structured hints)
- Extract repo hints from gotcha filenames + inline `repo/path:line` citations
- Extract concept definitions from dictionary YAML (definition, scope, set_by, read_by)
- Build: query → [(repo_hint, confidence), (concept, definition), (file_hint, line)]

### Phase 2: Filtered Code Search (using Phase 1 context)
- Repo prefilter: boost repos from gotcha matches (weighted by confidence)
- Query expansion: add dictionary aliases/concepts to query tokens
- Reranker hints: prefix docs with [concept = definition] for matched concepts

### Phase 3: Return (code + metadata)
- Primary: code chunks
- Metadata: related gotcha/dictionary hints (not ranked as primary)

### Implementation Order
1. Prototype: gotcha/dictionary extraction module (src/search/knowledge_context.py)
2. Integration: wire into hybrid.py Phase 1/2/3
3. Eval: test on subset of queries that should benefit (provider-specific queries)
4. Bench: full 665-query eval
03:52 — Progress:
[50/665] current=32/50 = 64.00%
[100/665] current=60/100 = 60.00%
03:52 — Memory:
Daemon PID=82231 RSS=938MB
Daemon PID=87458 RSS=14MB
Memory: 1% free

