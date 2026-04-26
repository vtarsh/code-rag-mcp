# Next-Session Prompt — 2026-04-27 (Run 1+2+3 + Jira eval + routing finding)

> **READ FIRST:** `.claude/debug/run2_final_report.md` — повний звіт, грабли, всі знайдені числа, бекмарки, що працює/що ні.

## TL;DR за день 2026-04-26 → 27

**Найбільша знахідка:** РОУТИНГ замість заміни. Один реранкер на все = trade-off. Розділення:
- `if is_doc_intent: skip reranker` (або mxbai FT — ідентично) → docs +4.5pp top-10 vs прод L6
- `else: use l12 FT instead of L6` → code +3.31pp top-10 vs прод L6 (POSITIVE on n=908 jira eval, bootstrap-confirmed)
- **Зважений combined: +3.22pp top-10 vs поточного прод (47% docs / 53% code).** Один з небагатьох ship-able win'ів.

**Що тренували сьогодні (FT'd на HF Hub Tarshevskiy/...):**
- `pay-com-rerank-l12-ft-run1` — **WINNER на коді** (n=908)
- `pay-com-rerank-mxbai-ft-run1` — на коді pass-through (=raw retrieval), на доках NOISE vs L6
- `pay-com-rerank-bge-h100-{bs2,bs8}-perftest` — програв всім, дорого
- `pay-com-docs-mxbai-ft-run1` — empty vectors (Bug 6o NaN)
- `pay-com-docs-gte-base-ft-run1` — built (109m), R@10 0.2093 (-3pp vs nomic)
- `pay-com-docs-nomic-ft-run1` — train+HF push OK (Bug 6p ROOT FIX landed `27afc27`), build_vectors timed out 2h, model на Hub але без локальних векторів

**Перший action для наступної сесії:**
1. Впровадити роутинг у `src/search/hybrid.py`: `is_doc_intent → skip rerank; else → l12 FT replaces L6`. ~30 LOC. Real +3.22pp top-10.
2. Re-eval після впровадження на jira_eval_n900 + doc_intent_eval_v3_n200.

**Що НЕ робити:**
- mxbai-large + ST.fit() — гарантовано NaN (Bug 6o, loss-agnostic)
- mxbai-rerank як рerankerа на коді — pass-through, без бенефіту
- bge-reranker-v2-m3 — 5x дорожче за mxbai, гірше за все
- Робити висновки з n=80/n=90 evals — bootstrap CI ширші за дельти
- pool size sweep для l12 — параметр для нього мертвий (тестував 50/100/200/300, всі ідентичні)

---

# Next-Session Prompt — 2026-04-24 late (15:45 EEST, mid-rebuild handoff) [LEGACY BELOW]

Copy this into the first user message of a new Claude Code session. Next session starts with zero conversation memory; everything important is captured below.

---

## TL;DR — Що в production прямо зараз (2026-04-24 15:45 EEST)

- **Reranker:** `reranker_ft_gte_v8` (unchanged since 2026-04-21).
- **Vector retrieval:** two-tower (v13). Code → CodeRankEmbed (768d, `db/vectors.lance.coderank/`). Docs → nomic-embed-text-v1.5 (768d, `db/vectors.lance.docs/`, 153 MB).
- **Router:** `src/search/hybrid.py::hybrid_search` auto-routes по `_query_wants_docs` + code-signal detection; `docs_index: bool | None` override.
- **Remote HEADs:**
  - `vtarsh/code-rag-mcp` = `71bc507` (parallel session landed A/B harness on top of `c0b5107`)
  - `vtarsh/pay-knowledge-profile` = `5df59e43`
- **Pytest:** 668/668 green.
- **Benchmarks (vs v8 baseline):** queries 0.933 (=v8), realworld 0.843 (=v8), flows 0.833 (new). Без регресії. Але поточна бенчмарка немає жодного pure doc-intent query → lift'у docs-tower не виміряно.

### LIVE STATE 2026-04-24 15:45

- **Rebuild running** (started 12:28 EEST, PID 453+456+473+2503). At 87% (lance=75874/86465). Stuck in long-chunks tail: ~1 chunk/sec on MPS. ~12k chunks left. ETA without fix: +3-3.5h to step 5 complete, +25min docs tower, +5 min benchmarks → fini ~19:00.
- **Decision made 15:43:** kill + apply Priority 1b fix (batch long chunks, LONG_BATCH=4) → restart from checkpoint → save ~2.5h. Parallel session executing now via 7-stage plan (kill → edit 2 files → smoke → pytest → commit+push via MCP → relaunch → sync).
- **Parallel session 2 (RunPod):** Priority 0 setup in progress. $15 credits added. First run = 10% subset fine-tune (~$1) → full run (~$4).
- **Monitor running** (Bash task id in chat — progress every 3 min with checkpoint + lance counts + RSS + sys-avail). Will fire "BUILD ENDED" with exit code when rebuild completes.

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

## Пріоритет 0 (optional capability): RunPod integration for fine-tuning + big-model bench

**User зареєстрований на https://console.runpod.io/user/settings, API key буде надано окремо. Ця секція — підготувати capability ДО першого запуску; user закине credits коли план готовий.**

**Що RunPod дає** чого нема на 16 GB Mac:
- Fine-tuning власного docs embedding model на pay-com корпусі (потребує >16 GB GPU, багатогодинний train)
- Тестування моделей >3 GB RAM (stella_en_1.5B_v5 MTEB top-5, linq-embed-mistral 7B, майбутні 10B+)
- Паралельні candidate builds замість sequential ~3 год local

**Що RunPod зараз НЕ потрібен:** поточний docs A/B shortlist (gte/arctic/bge-m3/nomic-v2-moe) ВСІ влізають у 16 GB sequentially. RunPod реально winning тільки для fine-tune / big models.

### First use-case: fine-tune pay-com-specific docs embedding model

**Budget: $5-10, ~4-6 год на A100 Secure Cloud ($0.89/hr).**

**Base model:** `nomic-ai/nomic-embed-text-v1.5` (drop-in у поточну архітектуру, 768d match з CodeRankEmbed для RRF) або `Alibaba-NLP/gte-large-en-v1.5` якщо initial A/B покаже перевагу.

**Training data:**
- Positives: 103 `(query, file_path)` pairs з `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` де `query_tag=="doc-intent" and label_final=="+"`
- Resolved doc content через `SELECT content FROM chunks WHERE file_path=? LIMIT 1` проти snapshot knowledge.db
- Negatives: mined від baseline nomic (top-K != expected_path) + random
- Split: 80/20 train/holdout — **новий holdout test set, не ті самі пари** (критично для uncontaminated eval)

**Method:** `MultipleNegativesRankingLoss` на sentence-transformers, 3-5 epochs. Standard recipe, 100k-step budget overkill для 100 пар — досить 1-2k steps.

### Security + data handling

**Perimeter:** pay-com knowledge.db + profile data = **PRIVATE** (vtarsh/pay-knowledge-profile). Cloud upload потребує Secure Cloud tier (data isolation) + pre-upload grep scan.

**Rule (refined 2026-04-24):** НЕ робити dummy-data dry-run — це overcautious і тратить час. Замість того:
- Від першого run — **RunPod Secure Cloud** ($0.89/hr) НІКОЛИ Community Cloud для pay-com data
- Підписати BAA/DPA з RunPod якщо є option
- Pre-upload scrub:
  ```bash
  grep -rn "secret\|password\|token\|api_key\|Bearer \|MerchantSecret\|PrivateKey\|X-Api-Key" dataset/
  # has to return EMPTY. Fix by redacting / removing files.
  ```
- Особливо скриньте `profiles/pay-com/docs/references/test-credentials/*.md` — там реальні sandbox креди. Виключити entirely з training dataset.
- Upload обрізаний artifact: `knowledge.db` subset → тільки doc chunks з file_path + anonymized content (rerun grep scrub після redaction).

**First run subset strategy** (catches bugs за $1, not $20):
- **10% of training pairs** (10 з 103) — достатньо щоб triger'и всі code paths (dataloader, tokenizer, loss, checkpoint save, HF Hub push)
- 1h time limit + $5 daily spending cap — якщо скрипт зациклиться, втрата обмежена
- 100 training steps замість 1-2k
- Це **не** "перевірка що в даних нема credentials" — то scrub робимо pre-upload. Це **перевірка що pipeline coding правильний** перед full $5 spend.

### API key handling

Claude Code **НЕ має ніколи бачити key в контексті відкритим текстом**. Pattern:

```bash
# Setup once (user)
mkdir -p ~/.runpod
echo "RUNPOD_API_KEY=rpa_..." > ~/.runpod/credentials
chmod 600 ~/.runpod/credentials

# Runtime (scripts)
source ~/.runpod/credentials
runpodctl pod create --api-key=$RUNPOD_API_KEY ...
```

Ніколи: `runpodctl --api-key=rpa_abc123...` в bash history / MCP push / git / claude transcript.

`.gitignore` additions:
```
.runpod/
*.runpod.key
runpod-api-*
/profiles/*/runpod_*/  # якщо скрипти пишуть intermediate в profile dir
```

Memory: `feedback_runpod_api_key_hygiene.md` — коли landed, для future sessions.

### Teardown discipline — КРИТИЧНО

Bulletproof shutdown у кожному training/bench script:

```python
import atexit, os, signal, subprocess

POD_ID = os.environ.get("RUNPOD_POD_ID")

def emergency_stop():
    if POD_ID:
        subprocess.run(["runpodctl", "stop", "pod", POD_ID], check=False)
        print(f"[teardown] Pod {POD_ID} stop requested", flush=True)

atexit.register(emergency_stop)
signal.signal(signal.SIGTERM, lambda *_: (emergency_stop(), sys.exit(1)))
signal.signal(signal.SIGINT, lambda *_: (emergency_stop(), sys.exit(130)))
```

**Плюс на самому RunPod pod settings обов'язково виставляй:**
- `Spending limit: $20` (daily hard cap)
- `Auto-terminate after: 1 hour` ← **для ПЕРШОГО run. Якщо скрипт buggy або зацикленний — втрачаєш $0.89 а не $20**
- `Idle timeout: 15 min` (auto-stop якщо GPU utilization <5%)

Після перевіреного workflow можна bump'ити до 6h/run, 12h/day, але first run — 1h + $5 daily.

### Cost guard

```python
MAX_DAILY_SPEND_USD = 20  # HARD limit
MAX_SINGLE_RUN_USD = 5     # first run
# Перед стартом: fetch RunPod account billing via API, abort if spent_today + estimated >= limit
```

### Evaluation methodology — CRITICAL

Без цього $10 витрачуться без answer'у "чи fine-tuned реально кращa":
- **Holdout test set:** 20-30 `(query, expected_path)` pairs окремі від 103 training pairs. **Не перетинається.**
  - Seed: `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` + 10-15 hand-crafted production-sampled queries (з `logs/tool_calls.jsonl` doc-intent subset — див. `project_docs_production_analysis_2026_04_24.md` top terms)
- **Metric:** file-level Recall@10 (per `project_docs_model_research_2026_04_24.md` methodology)
- **Baseline:** vanilla `nomic-v1.5` Recall@10 на тому self test set
- **Target:** fine-tuned Recall@10 на тому self test set
- **Significance:** accept if +≥10pp recall lift AND NOT overfitted (train-test gap <20pp)

### Required new files coли landing

- `scripts/runpod/pod_lifecycle.py` — create/stop/status via API. atexit guard. Cost check before start.
- `scripts/runpod/train_docs_embedder.py` — fine-tune harness (sentence-transformers MultipleNegativesRankingLoss). Writes model to HF Hub private repo for download.
- `scripts/runpod/bench_large_models.py` — load stella_en_1.5B / other оверgrown candidates, run benchmark_doc_intent methodology.
- `scripts/runpod/setup_env.sh` — pod bootstrap: install sentence-transformers, lancedb, huggingface-hub. Clone code-rag-mcp.
- `~/.runpod/credentials` + `.gitignore` entries.
- Memory: `feedback_runpod_api_key_hygiene.md`, `reference_runpod_cost_guard.md`.

### First run checklist

1. **User creates API key**, stores in `~/.runpod/credentials` (chmod 600). `.gitignore` entries landed.
2. **User sets RunPod account** spending limit = $5/day for first week, per-pod auto-terminate = 1h, idle timeout = 15 min. **Secure Cloud only** — not Community.
3. **Pre-upload secrets scrub** on training dataset: `grep -rn "secret\|password\|token\|api_key\|Bearer \|MerchantSecret\|PrivateKey\|X-Api-Key" <dataset>/` → must return empty. Exclude `profiles/pay-com/docs/references/test-credentials/*` entirely.
4. **Agent runs `scripts/runpod/pod_lifecycle.py --status`** — verifies API key works, account has credit, limits set correctly.
5. **Agent launches short fine-tune on 10% subset** (10 of 103 training pairs, 100 steps, 1h cap, ~$1). Goal: verify pipeline runs end-to-end (dataloader → tokenizer → loss → checkpoint save → HF Hub push → teardown).
6. **If clean** → full fine-tune: Secure Cloud A100, 103 pairs, 1-2k steps, ~4h, ~$4. Output: HF Hub private repo `vtarsh/pay-com-docs-embed-v1`.
7. **Local download + integrate:** `huggingface-cli download vtarsh/pay-com-docs-embed-v1`, add `"docs-payfin-v1"` key to `src/models.py`, run `python scripts/build_docs_vectors.py --force --model=docs-payfin-v1`.
8. **Eval on holdout** (20-30 new pairs not in training) → deploy if Recall@10 ≥ baseline + 10pp AND train-test gap < 20pp.

**Total estimated cost for full first cycle:** ~$5-10 (10% subset $1 + full run $4 + eval $0).

---

## Пріоритет 1b: Build-speedup for long-chunks path (5-10× faster rebuild)

**Проблема зафіксована 2026-04-24:** full rebuild на 16 GB Mac = **3-4h code tower alone**, bottleneck — long-chunks phase. У `build_vectors.py::embed_adaptive`: chunks <`short_limit` (1500 chars) йдуть batched (16-32/batch, ~20 emb/s), а chunks ≥1500 chars — **один-по-одному** (`~1 emb/sec` на MPS). Python overhead + tokenize + CPU→GPU transfer домінують, не forward pass.

Для corpus з ~13k long code chunks (файли з file_type service/workflow/library що розрізані chunker'ом 2000-8000 chars) це **~3.5 год** just standing there. Docs tower страждає менше бо avg 312 tok (most short-batched), але код має багато довгих.

**Fix sequence (сумарно speedup ~10-15×, ~1-2 год роботи):**

| # | Fix | Effort | Speedup | Risk | Where |
|---|---|---|---:|---|---|
| 1 | **Batch long chunks** (LONG_BATCH=4-8 замість 1-by-1) | 15 хв | 5-8× | low | `scripts/build_vectors.py::embed_adaptive` long_rows loop; same в `docs_vector_indexer.py::_embed_and_write_streaming`. Pad до max length у batch. |
| 2 | **`short_limit` 1500 → 2500** | 1 хв | 2× | low | `src/models.py` EMBEDDING_MODELS["coderank"]. Більшість chunks потрапляють у batched short path. Max 830 tok на 2500 chars — безпечно для max_seq 8192. |
| 3 | **fp16 (`model.half()`)** | 5 хв | 1.5-2× | low (0.5% recall loss per research) | Після `SentenceTransformer(...)` у `_load_sentence_transformer` / `_load_model` build scripts. Менше GPU memory, швидший compute на MPS. |
| 4 | **`torch.compile`** | 30 хв | 1.3× | medium (arch-sensitive, потрібен warmup) | Після model load: `model._first_module().auto_model = torch.compile(model._first_module().auto_model)`. Compile cost ~60s на старті. |
| 5 | **Chunker tuning** (`MAX_CHUNK_CODE` 2000 → 1500) | 1 год | systemic (менше long chunks в принципі) | medium (recall на granularity) | `src/index/builders/code_chunks.py`. Вимагає rebuild для застосування. |
| 6 | **`long_limit` 8000 → 4000** | 1 хв | 2× | HIGH (quality loss на хвостах документів) | Вимагає bench перед деплоєм. |

**Priority order:** #1 + #2 + #3 — найбезпечніші, разом ~5-7× speedup. #4 — опційно, test карефули. #5-6 — окремі дослідження.

**Validation plan:**
1. Apply #1+#2+#3 
2. Run `python scripts/build_docs_vectors.py --force` (45k chunks) — measure wall-clock. Baseline: 25 min. Target: <15 min.
3. Run `python scripts/build_vectors.py --force` (86k chunks) — measure. Baseline: 3-4h. Target: <1h.
4. Run pytest + benchmarks. Assert no recall regression (`benchmark_queries`, `benchmark_realworld`).
5. Якщо OK — push via MCP.

**Memory note:** fp16 дає 2× RAM reduction (CodeRankEmbed 230 MB → ~115 MB). Корисно для concurrent docs model A/B (Пріоритет 2) — headroom збільшується.

**Де записана детальна методологія:** не в окремому memory file (поки не запущено), але повна deconstruction вище в цьому prompt'і.

---

## Пріоритет 2: Doc-intent recall harness + docs-model A/B (validate two-tower lift)

**Проблема:** поточний bench suite має 0 pure doc-intent queries, тому показав flat metric для two-tower. Неможливо сказати чи фікс реально дає приріст І чи варто міняти docs-tower model на сильнішу.

**Setup:**
- 22 queries × 103 doc-intent positives з `profiles/pay-com/v12_candidates_regen_labeled_FINAL.jsonl` (`query_tag=="doc-intent" and label_final=="+"`). Top up to 30 hand-picked TOC entries.
- Новий скрипт: `scripts/benchmark_doc_intent.py` (skeleton в `/tmp/docs_model_research.md` §3 OR memory `project_docs_model_research_2026_04_24.md`).
- Метрика: file-level Recall@10 (а не rowid — chunk IDs змінюються при rebuild).
- **Bypass router+reranker**: `vector_search(..., docs_index=True)` raw top-K. Rerun з reranker on as secondary.
- Цільова: ≥80% (vs ~40-50% baseline single-tower v8).
- A/B/C router: `docs_index=False` / `True` / `None` (auto-route).

**Plus model A/B (research done 2026-04-24):**

Research agent виявив 3 кращі за `nomic-embed-text-v1.5` кандидати:
1. **`Alibaba-NLP/gte-large-en-v1.5`** — primary (1024d, ~1.0-1.3 GB RAM, MTEB 57.91, Apache-2.0, no prefix). LoCo long-doc 86.71 — реально використовує 8k context для Plaid/Nuvei.
2. **`Snowflake/snowflake-arctic-embed-l-v2.0`** — secondary (1024d, ~1.2 GB, MRL truncatable to 256d як fallback).
3. **`BAAI/bge-m3`** dense-only (1024d, MIT, strong long-doc).

Memory verdict: всі три fit на 16 GB Mac sequentially. **Run AFTER Saturday rebuild settles** (otherwise model load competes for RAM).

**Wall-clock per candidate:** ~35-50 min (download 5 + build 30-40 + bench 5). Full A/B 4 моделей (включаючи nomic baseline rerun) = ~3 год overnight.

**Risks (must address):** prefix bugs (smoke test cos>0.5 на known positives), gte-large RoPE NTK scaling factor=2 для ≥8k tokens, eval-set leakage (labels generated на nomic+coderank — dual-judge unlabeled top-10 per candidate).

**Full plan:** `project_docs_model_research_2026_04_24.md` memory + `/tmp/docs_model_research.md` (volatile — копію в memory вже зроблено).

**Production data analysis 2026-04-24** (`project_docs_production_analysis_2026_04_24.md`) уточнив рішення:
- **46.8% production search calls — doc-intent** (1339/2860). Doc-tower має масивний impact area.
- **Real prod queries SHORT**: avg 4.71 tok, p95 10, max 15. **Long-context (8k) gte-large advantage WASTED** на запитах. Але 1 chunk у corpus >8k → ctx все ж потрібен для documents.
- **Top 5 prod terms**: `payout` (192), `provider` (127), `nuvei` (120), `webhook` (102), `method` (69).
- **Corpus profile**: 48 892 doc chunks, 52% provider / 48% internal. avg=312 tok, p95=1015 tok, max=2710 tok. Тільки 1 chunk >8k. **98% English** → arctic-l-v2 / bge-m3 multilingual = wasted RAM.
- **Eval set BIASED**: 20 unique queries vs 1242 prod, 9/30 top-term overlap. Synthetic words (documentation, checklist, framework, audit) overweighted; misses payout/webhook/refund/apm/Aircash/Trustly/Interac. **MUST expand eval з 30-50 production-sampled queries** перед фінальним A/B.
- **Final pick refined**: `gte-large-en-v1.5` (English-only corpus → не платимо за multilingual), AFTER eval-set expansion.

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
