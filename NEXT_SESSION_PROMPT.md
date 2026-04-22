# Next-Session Prompt (2026-04-22 late evening)

Copy everything below into the first user message of a new Claude Code session.

---

## Що відбувається

Проект: **code-rag-mcp** — generic RAG система для пошуку по GitHub-orgs, з профілем `pay-com`. Python 3.12, FastMCP, SQLite FTS5, LanceDB, CrossEncoder reranker.

Git: **vtarsh/code-rag-mcp** (push тільки через `mcp__github__*`; `gh` CLI і `git push` заборонені).

**Production:** `reranker_ft_gte_v8` (listwise LambdaLoss, 285 MB bf16). Deployed 2026-04-21. Jira r@10 +5.09pp vs baseline, runtime +8.3pp/+2.1pp.

**Тести:** 420/420 зелені (останній full run).

## Хронологія останніх 2 днів

1. **2026-04-21 morning** — v8 deployed, v9/v10/v11 purged. Lesson: rerank ceiling на Jira labels.
2. **2026-04-21 late-evening** — P0a (hybrid.py `reranker_override`), P0b (suggestions.py type column fix), P0c (code_facts_fts + env_vars wired в hybrid pipeline).
3. **2026-04-21 evening** — conditional `--fts-fallback-enrich` → +5.09pp Jira r@10 full-eval.
4. **2026-04-22 early** — P1b churn replay на 400 real MCP queries: v8 reshuffles 77% top-1; FT transfer до runtime confirmed.
5. **2026-04-22 midday — P1b.2/P1b.3 dual judge pass:**
   - Opus-as-judge (ручний): v8 +8pp net над base
   - Local MiniLM-as-judge (`scripts/churn_reranker_judge.py`): v8 −64pp, зміщений до прози
   - Висновок: жоден off-the-shelf суддя не нейтральний на docs↔code axis; canonical direction = ground-truth eval
6. **2026-04-22 midday — P1c landed:**
   - `_DOC_QUERY_RE` розширено: `checklist/framework/matrix/severity/sandbox/overview/reference/rules`
   - `_CI_PATH_RE` + `CI_PENALTY=0.50` для `ci/deploy.ya?ml` + `k8s/.github/workflows/*`
   - Результат валідації на 9 failing pairs: 3 docs recovered, 1 CI partial, 4 unchanged (reranker-level, потрібен v12 FT)
7. **2026-04-22 afternoon — 13-agent audit** (5 blind + 5 contextual critics + 3 methodology). 18 P0/P1 знахідок. 2 виправлено в сесії, 6 dead scripts deleted, design proposals написані.
8. **2026-04-22 evening — 6 паралельних fix agents:**
   - ✅ hybrid.py rowid collision (P0) — composite `fts:/vec:` keys замість raw rowid
   - ✅ repo_indexer.py code_facts_fts drift (P0) — `cur.lastrowid` explicit
   - ✅ orchestrator.py FTS5 optimize (P1) — canonical 2-column form
   - ✅ vector.py error propagation (P1)
   - ✅ container.py WAL pragma lock (P2)
   - ❌ daemon.py /admin/unload race — agent changes did not persist in worktree
   - ✅ design proposals (bench redesign + eval file-level GT)

**Всі зміни на remote** через MCP push: `78a12325`, `85aa05a7`, `e0fcbf2a`, `36d39525`, `d1ae747c`, `bcbf2cde`, `ae809dfd`.

## Що ЗАЛИШИЛОСЬ

### P0 — критично

1. **`daemon.py:160-186` `/admin/unload` race з in-flight requests.** Нові запити у 500ms вікні між nulling providers і `os._exit(0)` re-trigger model load і потім killed. Fix: module-level `_draining` flag, 503 на нові запити після flip, drain 300ms, тоді exit.
2. **`daemon.py:272-279` JSONL log без lock/fsync/rotation.** Writes > PIPE_BUF (~4KB) можуть interleave. Unbounded growth. Fix: `fcntl.LOCK_EX` + `fsync` + size-based rotation at 50MB → `.1` → `.2` → `.3`.

### P1 — високий пріоритет

3. **Methodology gaps:**
   - GT repo-level не file-level (`scripts/eval_finetune.py:128`)
   - `net_improved` ±5pp binary на 46% тикетів (`eval_verdict.py:119`)
   - Test set 4 тикети, PI 78% (`finetune_data_v8/manifest.json`)
   - Eval pool ≠ prod pool за замовчуванням (`eval_parallel.sh:39`)
   - Train/test distribution mismatch (87% diff positives в test)
   - Queries use Jira description (~478 char mean), runtime queries 30-80 char
   - Детальний план: `docs/eval_file_level_gt_proposal.md` (gitignored локально)

4. **Benchmark overfit:**
   - Zero doc-intent queries у `profiles/pay-com/benchmarks.yaml`
   - `GROUND-TRUTH.md` = `benchmarks.yaml` = LOO test tickets (structural overfit)
   - Детальний план: `profiles/pay-com/bench/BENCH_REDESIGN_PROPOSAL.md` (gitignored)

### P2 — human-only чи великі проекти

5. **Label `profiles/pay-com/v12_candidates.jsonl`** (230 rows, `+/-/?` per row). Необхідно перед стартом v12 FT.
6. **Implement bench redesign** — 200-query stratified bench (~6-7 год).
7. **Implement eval gate redesign** — file-level GT + stratified `net_improved` (~12 год).
8. **v12 FT** — blocked на #5-#7.

## Куди рухаємось (конкретний план для цієї сесії)

### Пріоритет 1: Multi-agent MCP surface audit

Запусти **5 паралельних агентів** з `isolation: "worktree"` і `model: "opus"`. Всі в одному повідомленні для parallelism.

**Agent A — user-workflow pattern mining:**
```
Analyze /Users/vaceslavtarsevskij/.code-rag-mcp/logs/tool_calls.jsonl.
Group by session (session_id field). For each session identify:
(a) what task the user was pursuing,
(b) which tools were used, in what order,
(c) where tool composition was awkward (3+ chained where 1 would do),
(d) where the user had to re-parse one tool's output to feed another.
Report top 5 pain points in current MCP surface + concrete redesign per each.
Under 500 words.
```

**Agent B — parameter ergonomics:**
```
Read mcp_server.py + all src/tools/*.py. For each of the 11 MCP tools,
score: required-arg count (>3 = complex); conditional-required-arg
(if mode=X then Y required); return shape (markdown blob vs structured JSON);
error handling verbosity. Report each tool with a score + concrete redesign
suggestion. Max 400 words.
```

**Agent C — dead paths:**
```
Cross-reference mcp_server.py tool definitions against logs/tool_calls.jsonl
usage (2412+ calls). For each tool with <1% usage: why is it rare (real
lack of demand, discoverability bug, poor name)? Propose delete / merge /
rename / leave-as-specialist. Max 300 words.
```

**Agent D — response-size bloat:**
```
Sample 30 tool-call responses from logs/tool_calls_full.jsonl. Measure
per-response: total chars, estimated token cost, fraction of text typical
caller would NOT use. Identify top 3 bloat tools. Propose response-shape
changes (--brief flag, structured JSON, split endpoint). Max 300 words.
```

**Agent E — missing tool hypothesis:**
```
From logs/tool_calls.jsonl, find query patterns where user:
(a) chained 3+ tools, (b) re-parsed one tool's output to feed another,
(c) worked around a missing primitive. Propose 3-5 NEW tools that would
eliminate these patterns. For each: name, args, return shape, who benefits.
Max 400 words.
```

Після повернення — синтезую, перевіряю via `diff`, land changes.

### Пріоритет 2: Daemon P0 fix (multi-approach)

**3 агента з різними РЕАЛІЗАЦІЯМИ одного завдання:**

```
Fix P0 /admin/unload race + P1 JSONL log concurrency in daemon.py.

Agent 1: drain-flag + contextmanager approach — minimal diff.
Agent 2: lifecycle state machine (LOADING/READY/DRAINING/EXITING).
Agent 3: full refactor — separate "model unload" HTTP endpoint from process shutdown.

For each: write tests in tests/test_daemon.py covering drain behaviour,
log concurrency (8 threads × 10 records), and size rotation. Run full
pytest suite. Do NOT commit.
```

Порівняти 3 рішення, обрати найкраще, land.

### Пріоритет 3: v12 FT data design audit

**3 критичних агенти:**

```
Agent A: "worst case scenarios" — read prepare_finetune_data.py + v8 manifest +
feedback_pretrain_sample_check.md. List top 5 ways v12 FT could REGRESS
vs v8. For each: concrete mitigation.

Agent B: "missing training signals" — look at feedback memories + ROADMAP
loss-mode taxonomy. What patterns should v12 train on that v8 didn't?
CI-yml negatives, doc-intent positives, short-query augmentation, etc.
Concrete recipe.

Agent C: "neutral calibration without API" — design a judge-independent
direction metric using only (query, file_list) → (repo, file, chunk_type)
classifiers. Reference feedback_code_rag_judge_bias.md.
```

## Мульти-агентний аудит — lessons learned

- Завжди `model: "opus"` — hook блокує defaults
- `isolation: "worktree"` для коду-змін; без isolation для readonly аудитів
- Пусти всі агенти в ОДНОМУ message для parallelism
- Prompt кожного self-contained (агент без пам'яті)
- **Agent's file edits MAY NOT PERSIST у worktree** — всі агентські звіти треба validate проти реального стану файлів через `diff worktree/file main/file` перед merge
- Після повернення — синтезую, НЕ довіряю звітам agentів без перевірки

## Критичні файли для контексту (READ FIRST)

1. `ROADMAP.md` — source of truth, секції 2026-04-22 найсвіжіші
2. `.claude/rules/conventions.md` — generic rules
3. `CLAUDE.md` + `.claude/CLAUDE.md` — project instructions
4. Memory files у `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/` — автозавантажуються через `MEMORY.md` index
5. `profiles/pay-com/RECALL-TRACKER.md` — history of levers + verdicts
6. `docs/eval_file_level_gt_proposal.md` + `profiles/pay-com/bench/BENCH_REDESIGN_PROPOSAL.md` — open design work (gitignored, локальні копії)

## Команди

```bash
# Tests
cd ~/.code-rag-mcp && /usr/local/bin/python3.12 -m pytest tests/ -q

# Daemon restart (after P0 daemon.py fixes)
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
  nohup /usr/local/bin/python3.12 daemon.py > /tmp/daemon.log 2>&1 &disown
sleep 4 && curl -s http://localhost:8742/health

# MCP usage analysis
/usr/local/bin/python3.12 scripts/analyze_calls.py --sessions
```

## Правила проекту

- **Push: MCP only** — `mcp__github__push_files` або `mcp__github__create_or_update_file` з `owner: vtarsh`. `gh` CLI заборонений. `git push` — no credentials.
- **No external LLM APIs** — жодних Haiku/GPT/Gemini у pipelines. Тільки local CrossEncoder (ms-marco-MiniLM-L-6-v2 neutral; gte-reranker-modernbert-base baseline; reranker_ft_gte_v8 prod).
- **No commits without explicit ask** — CLAUDE.md rule. Чекай "коміть" або подібне перед `git commit`.
- **420/420 tests must stay green** — run before every commit.
- **Memory files auto-load** — `MEMORY.md` в `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/` — нова сесія побачить все що треба.

## Перші кроки нової сесії

1. Прочитай `ROADMAP.md` (остання секція `2026-04-22 13-agent audit`) + `project_audit_2026_04_22_findings.md` + `project_next_session_plan.md` з memory.
2. Запусти 5 агентів MCP surface audit паралельно (Пріоритет 1).
3. Очікуй ~5 min, проаналізуй звіти (критично перевіряй через `diff`!), синтезуй топ-3 улучшення.
4. Запусти 3 агента daemon P0 fix (Пріоритет 2).
5. Merge best solution, land. Push через MCP.
6. Якщо є час — v12 FT data design audit (3 агенти, Пріоритет 3).

**Success metric:** 5+ поліпшень у MCP surface + daemon P0 fixed + v12 data plan ready to implement.
