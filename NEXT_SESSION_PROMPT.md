# Next-Session Prompt — 2026-04-24 late

Copy this into the first user message of a new Claude Code session. Next session starts with zero conversation memory; everything important is captured below.

---

## TL;DR — Що в production прямо зараз

- **Reranker:** `reranker_ft_gte_v8` (unchanged since 2026-04-21).
- **Vector retrieval:** two-tower (v13). Code → CodeRankEmbed (768d, `db/vectors.lance.coderank/`). Docs → nomic-embed-text-v1.5 (768d, `db/vectors.lance.docs/`, 153 MB).
- **Router:** `src/search/hybrid.py::hybrid_search` auto-routes по `_query_wants_docs` + code-signal detection; `docs_index: bool | None` override.
- **Remote HEADs:**
  - `vtarsh/code-rag-mcp` = `e429776`
  - `vtarsh/pay-knowledge-profile` = `5df59e43`
- **Pytest:** 668/668 green.
- **Benchmarks (vs v8 baseline):** queries 0.933 (=v8), realworld 0.843 (=v8), flows 0.833 (new). Без регресії. Але поточна бенчмарка немає жодного pure doc-intent query → lift'у docs-tower не виміряно.

---

## Що зроблено 2026-04-24 (цілий день)

### 1. Two-tower v13 (ранок)
Зафіксовано остаточно. 12 commits на main, details в ROADMAP.md "2026-04-24 early-morning: two-tower v13 deployed".

### 2. Pay-com housekeeping (ранок)
Перед тим — 675 unstaged entries в `profiles/pay-com`. 4 локальні коміти. Два push-агенти довели до ~50 commits на `vtarsh/pay-knowledge-profile`:
- `b219fbc` — `.gitignore` розширено (2.3 GB бінарників gitignored)
- `d540e7a` → 41 commits — session scratch + `data/` deletions
- `c66f018` → 16 commits (parts 1-16 landed by agent round 1)
- `c66f018` → parts 17-43 (provider_types/recipes/scripts + 23 deletions, landed round 2)
- `560f9df` → `.gitignore` for scraper metadata pushed solo

**Pay-com outstanding:**
- 1 file: `recipes/new_apm_provider.yaml` — modified, not pushed (agent hit token budget)
- 334 Nuvei `.md` pages — NEW, not pushed. Local commit `560f9df` has them. ~67 batches @ 5/batch via MCP = ~$3-5 API. Defer to separate session.

### 3. Launchd weekly full rebuild (mid-day)
**Перед сьогодні:** тільки `com.code-rag-mcp.update.plist` — daily 03:00 **incremental**. Ніколи не запускався full rebuild сам.

**Сьогодні додано:**
- `~/Library/LaunchAgents/com.code-rag-mcp.weekly-rebuild.plist` — Saturday 04:00 EEST (Weekday=6, Hour=4), `caffeinate -i bash full_update.sh --full`. Перший fire: **2026-04-25 04:00 EEST**.
- Слот підібрано під юзера: free window 02:00-09:00 Київ. Daily 03:00 incremental finishes ~03:20 → 40 min buffer. 4h full rebuild worst-case finishes до 08:00.
- Fix (commit `8fec3bf` на main): `full_update.sh` step `[5b/7]` fallback до `bash "$SCRIPTS_DIR/run_with_timeout.sh"` — раніше падало `Permission denied` бо file tracked 100644 без exec bit.

Memorized в `reference_launchd_schedules.md`.

### 4. Memguard fix — full-build no longer leaks RAM (кінець дня)

**Проблема:** коли юзер запустив `make build` для візуального тесту RAM, пам'ять росла монотонно без release. `ps` показував PID 27209 `build_vectors.py --force --model=coderank` з 82 MB RSS на старті, model loading планувався ~3-4 GB.

**Root cause (знайдено в 74c0732 — commit з 2026-04-18):**
- `scripts/build_vectors.py::embed_adaptive` і `docs_vector_indexer::_embed_adaptive` акумулювали кожен embedding в in-memory `all_data` list **до кінця loop'у**.
- Жодного `gc.collect` / `torch.mps.empty_cache` між батчами.
- MPS tensor buffers не звільнялись → PyTorch retains buffers for reuse.
- Жодного `pause_daemon()` — якщо MCP daemon живий, CodeRankEmbed завантажується двічі (+~1 GB combined).
- На 16 GB Mac ламало пам'ять через 14 GB, Jetsam SIGKILL'v.

Fix з 74c0732 застосовано був **тільки** в `embed_missing_vectors.py` (step 5b cron). Скрипти full-build залишились протіченими.

**Фікс (commit `4361429` локально, pushed як `cf5a852..e429776` на main):**
- `src/index/builders/_memguard.py` (NEW, 172 lines) — shared module.
- `src/index/builders/docs_vector_indexer.py` — streaming writes, writer_fn callback, watchdog в loop'і.
- `scripts/build_vectors.py` — те саме для code tower. `--no-pause-daemon` flag для debug.
- `tests/test_memguard.py` (NEW, 16 tests)
- `tests/test_docs_vector_indexer.py` — оновлено під нову streaming-семантику (chunks_embedded = THIS run, vectors_stored = LanceDB total).

**Thresholds (overridable env):**
- `CODE_RAG_EMBED_RSS_SOFT_GB=8` (default) / `HARD_GB=10`
- `CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB=2` / `HARD_GB=0.8`
- Soft → compact + sleep 30s. Hard → `sys.exit(0)` (next run resumes з checkpoint).

### 5. Discovered: Bash `cat` silently truncates >~480 lines (push-time лесон)

**Коли пушив memguard fix через MCP agent.** Agent спочатку використав `cat` для отримання content, push_files прийняв це як content. Post-push md5 verify показав all 5 files **corrupted** on remote:
- Collapsed 3+ newlines to 2
- Stripped standalone single-line comments (PEP8 section dividers)
- Replaced content past ~480 lines with `// ... N more lines (total: N)` placeholder

Re-pushed via `Read` tool + manual line-prefix strip + md5-verify → byte-exact.

Зафіксовано в `feedback_bash_cat_truncates.md`. Patch-pattern для MCP push великих файлів: `Read → strip \d+\t prefix → create_or_update_file → get_file_contents → md5 → re-push if mismatch`.

### 6. Plaid docs investigation (mid-day, deferred)

`profiles/pay-com/docs/providers/plaid/llms-full.txt.md` = 5.5 MB single file (Plaid's own "llms.txt" convention — "Complete version - for LLMs with sufficient context capacity"). Plus `llms.txt.md` 300 KB summary. 140 individual `docs_*.md` files (~3.7 MB total) **duplicate the content** of the aggregate dumps.

**Not fixed today.** Options for next session:
- A: delete both aggregate dumps + add scraper exclusion → individual files кажуть те саме, менше шуму в pool
- B: rely on chunker fix (MAX_CHUNK enforcement) to split → but produces ~1400 duplicate chunks
- C: залишити як є

---

## Active launchd schedules (актуальний стан)

```
~/Library/LaunchAgents/
├── com.code-rag-mcp.daemon.plist       — KeepAlive MCP daemon on :8742
├── com.code-rag-mcp.update.plist       — daily 03:00 incremental
├── com.code-rag-mcp.weekly-rebuild.plist — Sat 04:00 EEST full rebuild (NEW 2026-04-24)
├── com.code-rag-mcp.auto-collect.plist — hourly Jira/Linear pull
└── com.code-rag-mcp.pattern-checker.plist — every 4h CI pattern scan
```

**Critical:** plists are gitignored (`*.plist`); they live only on user's machine. Memorized в `reference_launchd_schedules.md`. Toggle via `launchctl unload/load`.

---

## Пріоритет 1: Validate memguard fix через real full rebuild

**Найкраще: Saturday 04:00 cron сам запустить.** Переконатись наступного ранку:
- `ls /Users/vaceslavtarsevskij/.code-rag-mcp/logs/update_20260425_*.log` — лог існує
- Bottom of log: `Update COMPLETE (full, 7 steps) / Finished: ...`
- Benchmarks passed: grep "Average" в логу
- `launchctl print gui/$(id -u)/com.code-rag-mcp.weekly-rebuild` — state=not running (no overrun)

**Або manual (тестовий запуск з visualization):**
```bash
# Clean slate + PATH must be right (brew python3.14 lacks pyyaml)
PATH="$PATH:/opt/homebrew/bin" \
  CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
  nohup bash /Users/vaceslavtarsevskij/.code-rag-mcp/scripts/full_update.sh --full \
  > /tmp/full_rebuild.log 2>&1 &
```

Expected RAM: soft threshold 8 GB → compact + sleep 30s; hard 10 GB → clean exit + resume next run. Model weights: code tower ~230 MB + docs tower ~550 MB + reranker 285 MB ≈ 1.1 GB steady-state.

---

## Пріоритет 2: Doc-intent recall harness (validate two-tower lift)

**Проблема:** поточний bench suite має 0 pure doc-intent queries, тому показав flat metric для two-tower. Неможливо сказати чи фікс реально дає приріст.

**Setup:**
- 20-30 queries з `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` де target — reference docs (file_type IN doc/docs/gotchas/reference/task).
- Новий скрипт: `scripts/benchmark_doc_intent.py`. Метрика: top-10 recall на doc-intent тільки.
- Цільова: ≥80% (vs ~40-50% на single-tower v8 per `project_v12a_rejected_two_tower_pivot.md`).
- Прогнати vs `docs_index=False` / `True` / `None` для A/B/C.

---

## Пріоритет 3: Фінальні pay-com pushes

Після Saturday rebuild дать агенту 67 batches Nuvei:
- `recipes/new_apm_provider.yaml` — solo push
- 334 `docs/providers/nuvei/*.md` — ~67 batches @ 5 files/batch

Pushing pattern з урахуванням `feedback_bash_cat_truncates.md`: **ніколи не cat для push; Read + strip + md5-verify.**

---

## Пріоритет 4: Plaid + re-scrape 21 missing providers

**Plaid (обсуджено сьогодні, рішення не прийнято):**
- Видалити `docs/providers/plaid/llms-full.txt.md` (5.5 MB) + `llms.txt.md` (300 KB) з pay-com
- Додати до scraper exclusion list щоб не вернулись
- 140 individual `docs_*.md` вже містять ту саму інформацію

**21 provider без scraped docs (per 2026-04-23 audit):**
`credentials, configurations, features, aptpay, paynt, sandbox, crb, ecentric, epx, flexfactor, hyp, mapping, nets, payfuture, paymend, payoutscom, payulatam, safecharge, sepa, storage, aptpay`.

Команда:
```bash
python3 profiles/pay-com/scripts/tavily-docs-crawler.py <DOCS_URL> <provider>
# Потім incremental update docs vectors:
python3.12 scripts/build_docs_vectors.py --repos=<p1>,<p2>,...
```

---

## Пріоритет 5 (опціонально): test-credentials hygiene

`profiles/pay-com/docs/references/test-credentials/*.md` містить **реальні sandbox credentials** (наприклад `nuvei-provider.md` → `nuveiMerchantSecretKey: 2ayeX7WuzUYLFwrvO52nSougtoahxfbl2WTCTCp4OgFnr9zsNcHAitNcsijgn1NY`, `ppp-test.nuvei.com` → sandbox). Pre-existing в історії задовго до цієї сесії. **Pay-com — PRIVATE repo** (`vtarsh/pay-knowledge-profile`), ризик контрольований, але якщо хочеш чисто:
- Перенести в Bitwarden/1Password/SOPS + шаблонізувати `.md` на плейсхолдери
- `git filter-repo` cleanup + force-push (одноразово)

---

## Критичні правила (в пам'яті перевір перед будь-якою дією)

- **Push via MCP only** — `mcp__github__push_files` / `create_or_update_file` / `delete_file`, owner=`vtarsh`. gh / `git push` заборонені (gh не встановлено локально взагалі).
- **Для push великих файлів (≥100 рядків) — Read+strip+md5-verify.** Cat в sandbox-у тихо обрізає >~480 lines. Пам'ять: `feedback_bash_cat_truncates.md`.
- **No external LLM APIs** — CodeRankEmbed + nomic-embed-text-v1.5 + MiniLM CrossEncoders локально.
- **NEVER >1 Python compute process одночасно** на 16 GB Mac. Один process, sequential. Юзер заморозив ноут 2026-04-23 коли 4× `build_docs_vectors` = 21 GB virt.
- **MCP push_files size cap:** ≤5 files/push OR ≤3 if any ≥500 lines. Silent truncation inside create_table.
- **V8 reranker stays prod.** Two-tower лише vector leg, не rerank.
- **Agent hallucination ~30% rate** — md5 compare pre/post edit. Read tool гарантує byte-exact content (з line numbers).

---

## Env + command reference

### Build/rebuild
```bash
# Incremental (only changed repos):
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make update
# Full rebuild (2-4h, peak ~10 GB RAM with memguard):
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make build
# Docs tower only (sequential, ~25 min on MPS):
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3.12 scripts/build_docs_vectors.py --force
# PATH gotcha: must include /opt/homebrew/bin for `gh` but APPENDED, not prepended:
#   PATH="$PATH:/opt/homebrew/bin"
# Else brew python3.14 becomes default and lacks pyyaml → step 3 crashes.
```

### Memguard tuning (if Saturday rebuild hits hard threshold too often)
```bash
# Soft at 9 GB, hard at 11 GB (less conservative):
CODE_RAG_EMBED_RSS_SOFT_GB=9 CODE_RAG_EMBED_RSS_HARD_GB=11 make build
```

### Daemon
```bash
# Restart:
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
# Reversible unload:
curl -X POST http://localhost:8742/admin/unload
# Shutdown + launchd respawn:
curl -X POST http://localhost:8742/admin/shutdown
# Health:
curl http://localhost:8742/health | jq
```

### Launchd
```bash
launchctl list | grep code-rag-mcp           # active plists
launchctl print gui/$(id -u)/com.code-rag-mcp.weekly-rebuild  # state
launchctl start com.code-rag-mcp.weekly-rebuild  # ad-hoc kickoff
launchctl unload ~/Library/LaunchAgents/com.code-rag-mcp.weekly-rebuild.plist  # disable
```

---

## Memory файли критичні для next session

- `project_two_tower_v13_landed.md` — what's deployed + how to use
- `project_v12a_rejected_two_tower_pivot.md` — root cause why we pivoted from single-tower FT
- `reference_launchd_schedules.md` — active plists + schedule rationale
- `feedback_bash_cat_truncates.md` — **NEW 2026-04-24**: MCP push big-file pattern (Read+strip+md5-verify)
- `feedback_agent_hallucination_detection.md` — md5 workflow
- `feedback_push_files_size_cap.md` — ≤5 files/push
- `feedback_push_via_mcp_not_gh.md` — MCP push only
- `feedback_no_external_llm_apis.md` — local-only

---

## Перші кроки нової сесії

1. Прочитай `ROADMAP.md` остання секція + memory files вище.
2. Перевір Saturday cron вже відпрацював:
   ```bash
   ls -lt ~/.code-rag-mcp/logs/update_*.log | head -3
   # Шукай update_20260425_040*.log — це weekly full
   ```
3. Якщо пройшов чисто (`Update COMPLETE (full, 7 steps)`) → Пріоритет 2 (doc-intent harness) як швидкий валідаційний win.
4. Якщо хардфейл → знайти в логу `hard memory pressure ... exiting cleanly at N/M` → resume manually з переглянутими thresholds.
5. Або (якщо ноут вільний раніше): ad-hoc `launchctl start com.code-rag-mcp.weekly-rebuild` — не чекати Saturday.

**Success metric для наступної сесії:** doc-intent recall harness показує ≥80% top-10 (vs ~40-50% baseline single-tower v8).
