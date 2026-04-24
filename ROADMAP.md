# P5 Reranker — Roadmap

**Status (2026-04-24 15:45 EEST — mid-day context switch):** Full rebuild running за 3h15m at ~87% (stuck on long-chunks tail). Decision: kill + apply batch-long-chunks speedup + restart from checkpoint. New session in parallel chat handles Priority 1b + RunPod Priority 0. Two-tower v13 + memguard fix + weekly launchd schedule all landed earlier today. Reranker = `reranker_ft_gte_v8` (unchanged). 668/668 pytest green. Remote `vtarsh/code-rag-mcp` HEAD = `c0b5107 → 71bc507` (doc-intent A/B prep landed by parallel session). Remote `vtarsh/pay-knowledge-profile` HEAD = `5df59e43`.

## 2026-04-24 15:45 EEST: build-speedup discovery + decision to apply Priority 1b mid-rebuild

**Context:** 2nd full rebuild of the day (started 12:28 EEST, triggered by user watch to validate memguard fix). At 15:43 / 87% real progress (74k / 86k chunks), rate dropped to ~1 chunk/sec. Remaining 12k chunks ETA ~3h more. First 60-70k chunks (short/batched) flew by at ~30 emb/sec in first hour; last 15k long chunks (file_type service/workflow/library with 2000-8000 char content) dragged the tail.

**Root cause:** `scripts/build_vectors.py::embed_and_write_streaming` and `src/index/builders/docs_vector_indexer.py::_embed_and_write_streaming` long-chunks path processes `len(content) > mcfg.short_limit` (1500 chars for coderank) **one chunk at a time** via `_encode(model, [text], mcfg)`. Python overhead + tokenize + MPS CPU↔GPU transfer dominate over forward pass. Batching 4 long chunks with padding eliminates this overhead per chunk.

**Fix plan (Priority 1b — full doc in NEXT_SESSION_PROMPT.md):**
- #1 `batch long chunks (LONG_BATCH=4 with padding)` — 5-8× speedup, low risk.
- #2 `short_limit 1500 → 2500` — 2× speedup, **HIGH risk** (breaks vector coherence across old+new chunks in same table).
- #3 `fp16 (model.half())` — 1.5× speedup, low risk.
- #4 `torch.compile` — 1.3× speedup, medium risk (arch-sensitive).
- #5 chunker tuning, #6 long_limit reduction — deferred.

**Decision (2026-04-24):** apply **only #1** mid-run. Path:
1. Kill current `full_update.sh` — checkpoint at 74512 + 75k vectors in LanceDB preserved.
2. Edit build_vectors.py + docs_vector_indexer.py long-chunks loop (batched with LONG_BATCH=4).
3. Smoke test `/tmp/smoke_batch_long.py`: 20 pending long chunks via old vs new code, assert cos≥0.999 per pair, RSS stable across 5 iterations.
4. pytest 668+ green.
5. Commit + push via MCP (Read+strip+md5-verify discipline).
6. Relaunch `full_update.sh --full` → resume from checkpoint → remaining 12k in ~25 min (vs 3h without fix) + step 5c docs tower ~25 min + step 6-7 ~5 min. **Total ~60 min to full finish.**
7. If smoke/pytest fail → `git checkout` the 2 files → rollback → original build continues eventually.

**Delegated to parallel session** via copy-paste prompt (full 7-stage plan with expected timings, commit message, rollback procedure).

**#2 short_limit change intentionally NOT applied** — would break semantic coherence between the 74k chunks already embedded with short_limit=1500 and the remaining 12k that would be embedded with short_limit=2500. Different text prefix = different vectors in the same LanceDB table = inconsistent retrieval ranking. Would require full `--force` rebuild from scratch (another 45-60 min). Defer #2 + #3 + #4 to separate session after validating #1.

## 2026-04-24 afternoon: docs-tower model A/B harness landed (parallel session)

Commits `cf5da9a + 71bc507` by parallel session landed 5 files for A/B testing of alternative docs embedders:
- `src/models.py` — registered 3 candidate models: `docs-gte-large`, `docs-arctic-l-v2`, `docs-bge-m3-dense` (each writes to separate `vectors.lance.docs.<key>/`).
- `src/index/builders/docs_vector_indexer.py::build_docs_vectors(..., model_key)` — extended.
- `scripts/build_doc_intent_eval.py` — eval-set generator. Already executed: `profiles/pay-com/doc_intent_eval_v1.jsonl` = 50 rows (20 gold + 30 prod-sampled).
- `scripts/benchmark_doc_indexing_ab.py` — sequential A/B build harness, sys-avail ≥5 GB hard guard.
- `scripts/benchmark_doc_intent.py` — file-level Recall@10 harness.

**Refinement 2026-04-24 evening:** add `nomic-ai/nomic-embed-text-v2-moe` as **#0 candidate** (drop-in 768d compat with v1.5, MoE 305M/75M active, +3-5% MTEB per nomic blog). Run order: nomic-v2-moe → gte-large → arctic-l-v2 → bge-m3 dense. Stop-early if v2-moe lift ≥3pp (free upgrade, zero arch change).

**Second-opinion research** (ChatGPT-level review 2026-04-24) confirmed: CodeRankEmbed + `reranker_ft_gte_v8` stay prod; only docs tower in A/B scope. Flagged hallucination: `Qwen3-Embedding-0.6B-Code` doesn't exist (other LLM confused naming).

## 2026-04-24 evening: RunPod capability plan landed

User registered RunPod + added $15 credits. Plan in NEXT_SESSION_PROMPT.md Priority 0. First use-case: **fine-tune pay-com-specific docs model** from `nomic-embed-text-v1.5` base on 103 doc-intent positive pairs. Budget ~$5-10, ~4-6 h on A100 Secure Cloud.

Safety rails (refined 2026-04-24 after user feedback):
- Secure Cloud only for pay-com data, never Community.
- Pre-upload `grep -rn "secret\|password\|token\|api_key\|Bearer \|MerchantSecret\|PrivateKey\|X-Api-Key" <dataset>/` must return empty.
- API key in `~/.runpod/credentials` (chmod 600), never in Claude context, never in git, never in MCP.
- Bulletproof teardown: atexit + signal handlers + RunPod pod auto-terminate (1h first run) + spending limit $5/day.
- First run = 10% subset (10 of 103 pairs), 100 steps, 1h cap, $1 spend — catches pipeline bugs cheap.
- Evaluation: holdout test set (not training pairs), accept fine-tuned only if Recall@10 ≥ baseline + 10pp AND train-test gap < 20pp.

Delegated to parallel session via separate copy-paste prompt (Stage A skeleton → B pre-flight → C 10% run → D full run).

## 2026-04-24 late: memguard fix — full-build no longer leaks RAM

**Problem discovered during user-watched `make build` test at 10:50 EEST.**
`scripts/build_vectors.py::embed_adaptive` and `docs_vector_indexer::_embed_adaptive`
accumulated every embedding into an in-memory `all_data` list until end of loop,
never called `gc.collect` / `torch.mps.empty_cache` between batches, and never
paused the resident MCP daemon before model load. On 16 GB Mac the code-tower
build climbed past 14 GB combined-with-daemon and Jetsam SIGKILL'd it — the
same failure pattern commit `74c0732` (2026-04-18) fixed in
`embed_missing_vectors.py`. That fix was never propagated to the full-build
scripts.

**Fix (5 files, commit `4361429` locally; pushed as `cf5a852..e429776` on main):**
- `src/index/builders/_memguard.py` (NEW, 172 lines) — shared module:
  `get_limits()` reads `CODE_RAG_EMBED_RSS_{SOFT,HARD}_GB` + `CODE_RAG_EMBED_SYS_AVAIL_{SOFT,HARD}_GB`
  (defaults 8 / 10 / 2 / 0.8 GB); `pause_daemon()` POSTs `/admin/shutdown`
  so launchd restarts a fresh low-RSS daemon; `free_memory()` is
  `gc.collect` + `torch.mps.empty_cache`; `memory_pressure()` classifies
  ok/soft/hard; `check_and_maybe_exit()` compact+sleep on soft, `sys.exit(0)`
  on hard (caller's checkpoint lets the next run resume).
- `src/index/builders/docs_vector_indexer.py` (REFACTOR) — streaming writes
  via `_open_or_create_writer + writer_fn` callback. Each batch flows directly
  into LanceDB (no `all_data` accumulation), then `free_memory` + watchdog.
  Checkpoint format simplified to `{done_rowids: [...]}` (LanceDB is the
  source of truth for vectors). `force=True` clears stale checkpoint at
  start. `pause_daemon=True` default.
- `scripts/build_vectors.py` (REFACTOR) — same pattern for the code tower.
  `embed_simple()` kept for back-compat (used by `embed_missing_vectors.py`);
  new `embed_and_write_streaming()` drives both full and incremental paths
  from `main()`. `--no-pause-daemon` flag for debug.
- `tests/test_memguard.py` (NEW, 16 tests) — covers `get_limits` env
  overrides, `pause_daemon` error paths, `memory_pressure` classification,
  `check_and_maybe_exit` state machine, `free_memory`.
- `tests/test_docs_vector_indexer.py` (UPDATE) — rewrote checkpoint-resume
  test for streaming semantics (chunks_embedded = THIS run, vectors_stored =
  LanceDB total). Added `test_force_clears_stale_checkpoint`.

**Lesson uncovered during the push:** Bash `cat` in agent sandboxes **silently
truncates files >~480 lines** — collapses 3+ newlines to 2, strips standalone
single-line comments, and replaces content past ~480 lines with
`// ... N more lines (total: N)`. First round of MCP pushes landed corrupted
versions of all 5 files; caught via per-file md5 verify (`feedback_mcp_push_full_content.md`
discipline). Re-pushed via `Read` tool + manual line-prefix strip →
byte-exact. Memorized as `feedback_bash_cat_truncates.md`.

## 2026-04-24 mid-day: weekly full rebuild scheduled + incremental cron fix

**Before today:** only daily 03:00 launchd incremental. No weekly full rebuild
— `make build` had to be kicked off manually.

**Today:**
- **New launchd plist** `~/Library/LaunchAgents/com.code-rag-mcp.weekly-rebuild.plist`:
  calls `caffeinate -i bash full_update.sh --full` on **Saturday 04:00 EEST**
  (Weekday=6, Hour=4). User's free window is 02:00-09:00 Kyiv; daily 03:00
  incremental finishes by ~03:20 so 04:00 leaves 40 min buffer. Worst-case
  4h full rebuild finishes by 08:00. `LOCK_DIR` in `full_update.sh`
  protects against overlap if the full rebuild runs long.
  **First fire: 2026-04-25 04:00 EEST.**
- **`scripts/full_update.sh`** fixed (commit `8fec3bf`): step `[5b/7]`
  (doc-vector sync via `embed_missing_vectors.py`) was hitting `Permission
  denied` on `run_with_timeout.sh` (tracked at 100644, no exec bit — git
  mode-bit drift). Now invoked as `bash "$SCRIPTS_DIR/run_with_timeout.sh"`
  → exec-bit-agnostic. Saved in `reference_launchd_schedules.md`.

## 2026-04-24 morning: pay-com housekeeping continued

**Starting state 2026-04-24 morning:** 675 unstaged entries in
`profiles/pay-com` git repo (separate from code-rag-mcp). Four local commits
ready (b219fbc, d540e7a, c66f018, 560f9df). Remote HEAD `0acb83bd` (parts 1-4
of c66f018 already pushed).

**Push agents (2 rounds):**
- Round 1 (`a9929fc0a1d9437cb`): pushed parts 5-16 of c66f018 (12 commits),
  covering references/*, docs/notes/_moc/*, docs/gotchas/*, all 20
  test-credentials/*. HEAD `bc69475`.
- Round 2 (`addf4f7b49e72a7c5`): pushed ROADMAP to code-rag-mcp (commit
  `29a4343`), then parts 17-19 of c66f018 (provider_types + recipes +
  scripts, 13/14 modified files) + parts 20-43 (23/23 deletions) + the
  `.gitignore` from commit 560f9df. HEAD `5df59e43`.

**Pay-com outstanding:**
- 1 file: `recipes/new_apm_provider.yaml` (modified, not pushed — agent hit
  token budget before reaching it)
- 334 Nuvei `.md` pages (NEW, not pushed) — local commit `560f9df` sits on
  disk. ~67 batches @ 5 files/batch via MCP = ~$3-5 in API tokens. Defer
  to a dedicated push session.

## 2026-04-24 early-morning: two-tower v13 deployed

**v13 = two-tower architecture**, not a 13th FT iteration. After v1-v12a single-tower FT all marginal/negative, the pivot was to give docs their own embedding tower so vector retrieval finds them on merit instead of relying on FTS + reranker bailouts.

## 2026-04-24 early-morning: two-tower v13 deployed

**v13 = two-tower architecture**, not a 13th FT iteration. After v1-v12a single-tower FT all marginal/negative, the pivot was to give docs their own embedding tower so vector retrieval finds them on merit instead of relying on FTS + reranker bailouts.

**Deployed pieces (12 commits on main, fe5d1e6..4398ccf; see `e429776` for full history):**
- `src/models.py` — new `"docs"` model key (nomic-embed-text-v1.5, 768d). `EmbeddingModel.document_prefix` field.
- `src/embedding_provider.py` — per-key singletons; `get_embedding_provider("docs")` lazy-loads the docs tower.
- `src/container.py` — per-key LanceDB cache; `get_vector_search("docs")` opens `vectors.lance.docs`.
- `src/search/vector.py` — `vector_search(..., model_key)` pass-through.
- `src/search/hybrid.py` — `hybrid_search` routes vector leg by `_query_wants_docs` + code-signal detection. Pure doc-intent → docs tower; pure code-intent → code tower; mixed → both, dedupe by rowid (keep-first) before RRF. `docs_index: bool|None` override for eval/debug.
- `src/search/service.py` — `search_tool` threads `docs_index` through cache key.
- `src/index/builders/docs_vector_indexer.py` — `build_docs_vectors(...)` reads chunks WHERE file_type IN {doc,docs,gotchas,reference,provider_doc,task,flow_annotation,dictionary,domain_registry} and writes embeddings. Adaptive batching, 5000-row checkpoints, IVF-PQ index.
- `scripts/build_docs_vectors.py` — CLI wrapper. `--force`, `--repos=`, `--no-reindex`.
- `scripts/full_update.sh` — step `[5c/7]` runs docs vectors sequentially after code vectors.
- `daemon.py` /health — exposes `embedding_providers_loaded: list`.
- 36 new tests (`test_two_tower_foundation.py` 19, `test_two_tower_routing.py` 8, `test_docs_vector_indexer.py` 9).

**Build stats (45759 doc chunks, MPS):**
- Embedding: ~25 min @ 18-23 emb/s (started high, decayed as long-chunk batches dominated)
- LanceDB write: 0.7s
- IVF-PQ index (64 partitions, 48 sub_vectors): 19.4s
- On-disk size: 153 MB

**Memory (M-series 16 GB):** code tower ~230 MB + docs tower ~550 MB + reranker ~285 MB ≈ 1.1 GB resident. Safe for production.

**Benchmark verdict (two-tower vs v8 baseline):**
| Bench | v8 | two-tower v13 | Δ |
|---|---:|---:|---:|
| queries (4 conceptual) | 0.9333 | 0.933 | flat |
| realworld (6 real) | 0.843 | 0.843 | flat |
| flows (2 flows) | — | 0.833 | new |

The benchmark queries are mostly code-intent so they still hit the code tower, which is unchanged. The win is on doc-intent queries that previously fell off the rerank pool because CodeRankEmbed embedded docs to near-random vectors. Smoke-tested live: `provider response mapping reference` returns `docs/references/provider-response-mapping.md` top-1 with no FTS fallback needed.

**Open follow-up:** add a doc-intent recall harness to the benchmark suite (current 4-conceptual + 6-realworld + 2-flow set has zero pure doc-intent queries, so the bench can't quantify the doc-tower lift). Suggested seed: 20-30 queries from the regen candidates that hit reference docs.

---

## 2026-04-23 late: v12a REJECTED — two-tower pivot decision

**V12a FT — REJECT.** Measured vs v8 baseline (full eval):
- Train Jira r@10 (859): 0.633 → 0.613 (**Δ −0.020**, primary gate fail)
- Train Jira r@25: 0.706 → 0.691 (−0.015)
- Test holdout r@10 (50): 0.548 → 0.518 (**Δ −0.030**)
- Test holdout r@25: 0.643 → 0.624 (−0.019)

Stable regression on both splits. v8 stays prod. v12a archived at `profiles/pay-com/reranker_ft_gte_v12a/`, snapshot at `profiles/pay-com/finetune_history/gte_v12a.json`.

**Root cause (why 12 FT iterations plateau'd):**
Single-tower architecture — CodeRankEmbed embeds code AND `.md` files into the same vector space, but was trained ONLY on GitHub code. Doc files get near-random embeddings → vector stage recall for doc queries is already broken before reranker sees a candidate. Reranker can only reorder what vector+FTS returned; it cannot conjure the right doc into top-100.

All reranker FT iterations (v1..v12a) hit the same ceiling. **12 FT iterations = sunk-cost spiral.**

**Decision: pivot to two-tower.**
- Code index: keep CodeRankEmbed, `db/vectors.lance.coderank/` (existing, unchanged)
- Docs index: new `db/vectors.lance.docs/` table with text-specialized model (`BAAI/bge-m3` OR `nomic-embed-text-v1.5`)
- Query router: extend `_query_wants_docs()` — code-intent → code index; doc-intent → docs index; mixed → both + RRF merge
- Cost: +5-8 GB disk, +2-3 GB daemon RAM, ~2 days engineering + overnight reindex

**Salvaged from v12 work (reusable for v13):**
- `v12_candidates_regen_labeled_FINAL.jsonl` (118+/79−) — doc-positive examples for future docs-reranker fine-tune
- `holdout_jira_50.jsonl` + `holdout_runtime_20.jsonl` — valid holdouts, reuse
- `scripts/eval_jidm.py` IM-NDCG — tower-agnostic gate
- `scripts/sanity_v2_gate.py` — v13 validation flow

**Pitfalls to not repeat:**
1. Don't stack reranker signals when architecture is the bottleneck
2. Dual-judge autonomous labeling has 5-10% FP — don't treat as ground truth for FT
3. Stratified holdouts still small (50+20) — Δ ±0.02 may be within noise envelope; v13 should target 100+ holdout tickets

Next session priorities in `NEXT_SESSION_PROMPT.md`.

## 2026-04-23 evening: Doc search diagnostics — 6-agent synthesis

User report: search requires 3-4 reformulations to surface relevant docs. Ran 6 readonly diagnostic agents in parallel: coverage heatmap, chunk quality, reformulation patterns, gotchas inventory, link rot, Obsidian-style redesign proposal.

### Critical findings

| # | Area | Finding | Impact | Fix Status |
|---|---|---|---|---|
| 1 | Chunking | `chunk_markdown()` does NOT enforce MAX_CHUNK. 1509 doc chunks >4k chars (exceed CrossEncoder window). Worst: Plaid `llms-full.txt` = 1 chunk, 5.5 MB. | +3-6pp R@10 if fixed (provider docs become reachable) | In-flight (agent `a1b24ec514a571526`) |
| 2 | Chunking | 550 exact-duplicate chunks (138 provider_doc + 22 reference clusters). Boilerplate like `### Responses\n\nOK` appears 66-69×. | +2pp R@10 (reduces noise floor) | In-flight |
| 3 | Chunking | 28% of doc chunks <200 chars. MIN_CHUNK=50 includes `[Repo: X]` prefix, so effective body threshold is ~15 chars. | +1-2pp R@10 (drop ~5000 orphan headings) | In-flight |
| 4 | Coverage | 21 provider code repos have ZERO external docs scraped (credentials, configurations, features, aptpay, paynt, sandbox = highest value). | Blind spots in retrieval | Open — needs scrape-docs run per `feedback_scrape_docs_prefer_sitemap` |
| 5 | Coverage | Only 4 of 67 provider code repos have `docs/GOTCHAS.md`. 63 providers silent. | `gotchas` file_type has strong rerank weight but only 75 chunks total | Open — human/agent authoring task |
| 6 | Reformulation | 82% of reformulation chains end with same `result_len` — user reformulates in vain. 56% of transitions are provider-swap (user manually fanning-out across nuvei/payper/volt). | High-user-pain, 1700+ wasted search calls in logs | Open — new tool: cross-provider fan-out for `provider × topic` queries |
| 7 | Link rot | Scraped provider docs have 9,807 broken internal links (99.6%) — scraper emits HTML nav residue (`/devdocs/*`, `#__docusaurus_skipToContent_fallback`) without host-rewrite. | Noise in FTS; some "answers" never resolve | Open — fix in `.claude/skills/scrape-docs` pipeline |
| 8 | Link rot | Plaid `provider-plaid-docs_llms-full.txt.md` has 8,394 outbound links = 62% of all externals; also 5.5MB single chunk. | Skews graph metrics + unusable in retrieval | Split or archive to `.archive/` |
| 9 | Gotchas | Directory `profiles/pay-com/docs/gotchas/` is CLEAN: 0 duplicate blocks, 0 stale versions, layered (canonical in `global-conventions.md` + per-provider specialization). | Not a problem source; false positive fear | Only cosmetic: add `_index.md`, linkify `CLAUDE.md:12` |
| 10 | Structure | Dominant filesystem axis is per-provider (87%) but retrieval needs per-topic. 64 hand-authored docs have 0 inbound markdown links (orphans). | Discoverability for users and reranker | Open — Obsidian-style redesign proposal (11h migration plan, 3 phases) |

### Three-prong fix plan

**Phase A — Chunker fixes (in-flight agent, landing this session):**
- Enforce MAX_CHUNK via paragraph subsplit + overlap (fix #1)
- Dedup by content_hash before insert (fix #2)
- Orphan heading filter: strip prefix before MIN_CHUNK check (fix #3)
- After code lands: **requires full reindex** (~overnight, 20 GB RAM per CLAUDE.md).

**Phase B — Coverage + link hygiene (next session, ~6h human + agents):**
- Re-scrape 21 missing providers via scrape-docs skill
- Split Plaid mega-dump into sitemap-aligned per-section files
- Fix scrape-docs post-processor to rewrite `/path/` → `https://host/path/` (or drop internal links)
- Author `docs/GOTCHAS.md` for top-20 providers by code-chunk count

**Phase C — Structural (when v12a settled, ~15h):**
- Cross-provider fan-out tool — new `analyze/cross_provider_pattern.py` (addresses 56% of reformulation chains)
- Doc-intent reranker routing in `hybrid.py:85-97` (addresses docs-strip regression even without explicit `docs` keyword in query)
- Obsidian-style migration: `notes/` tree + 7 MOCs + 12 tags + `[[WikiLink]]` syntax. Per proposal.

### Notes / caveats

- All 6 diagnostic agents are readonly audits (no modifications made in this sweep).
- Proposals and recipes are documented in agent output logs — not lost.
- Chunker fix does NOT trigger reindex automatically; that's a separate operator action.

### Relevant files

- Chunker: `src/index/builders/docs_chunks.py`, `src/index/builders/_common.py`
- Scraper: `.claude/skills/scrape-docs/` skill
- Search: `src/search/hybrid.py` (penalties, reranker, doc-intent detection)
- Data: `db/knowledge.db`, `profiles/pay-com/docs/{gotchas,references,providers}`
- Logs: `logs/tool_calls.jsonl` (reformulation source data)

## 2026-04-22 13-agent audit: synthesis, fixes landed, open items

Ran 13 parallel agents — 5 blind auditors (src, scripts, tests, index pipeline, daemon+MCP), 5 context-aware critics (P1c, v8 FT data, MCP tool design, benchmarks, recall-lever history), 3 methodology auditors (eval metrics, churn metrics, benchmark realism).

### Synthesis

**Real bugs found in code (18 P0/P1, most not FT-related):**

| # | File:line | Severity | Summary |
|---|---|---|---|
| 1 | `src/search/hybrid.py:304-335` | P0 | RRF `scores` dict is keyed by raw rowid — but FTS5 chunks and LanceDB vector rows live in independent rowid spaces, so `rowid=42` from both sources SILENTLY MERGES into one record with the wrong `repo/file_path/snippet`. Real recall bug. Needs (repo,file,chunk) composite key. **Not fixed yet — bigger refactor.** |
| 2 | `src/index/builders/orchestrator.py:399-401` | P1 | FTS5 `optimize` misuse: `INSERT INTO chunks(chunks, rank, content, ...) VALUES('optimize', ...)` is not the documented `INSERT INTO chunks(chunks) VALUES('optimize')` form — SQLite treats it as a plain row insert and creates a garbage row with `chunks='optimize'` every build. **Not fixed yet.** |
| 3 | `src/index/builders/repo_indexer.py:128-140` | P0 | `last_insert_rowid()` used right after a `chunks` (FTS5 virtual) insert can resolve to a rowid in the wrong table; `code_facts_fts` gets drift rows. **Not fixed yet — bigger refactor.** |
| 4 | `src/search/env_vars.py:23` | P1 | Docstring claims "avoids matching URL/API/TLS" but regex `\b[A-Z][A-Z0-9_]{2,}\b` matches them; every web/config query triggers env_var repo boost on unrelated repos. **FIXED** — added post-match filter: skip 3-char acronyms without `_`/digit. |
| 5 | `src/search/fts.py:63` | P1 | AND/OR/NOT/NEAR stripping used ` {op} ` substring — leading/trailing operators slipped through, FTS5 raised, swallowed by `except OperationalError`, silent 0-result. **FIXED** — word-boundary regex. |
| 6 | `src/search/vector.py:30-31` | P1 | `if err and table is None` masks the real reason when `get_vector_search` returns `(None, None, warning)` — caller sees generic "Vector search unavailable" instead of the actual path-missing message. **Not fixed yet.** |
| 7 | `src/container.py:43-45` | P2 | `_wal_set` race: check-then-act without `_lock`. Harmless in practice (journal mode is DB-file-level) but footgun. **Not fixed yet.** |
| 8 | `src/index/builders/repo_indexer.py:141-142` | P2 | `except Exception: pass` on per-file code_facts extraction — one malformed JS silently skips the repo's facts. **Not fixed yet.** |
| 9 | `daemon.py:160-186` | P0 | `/admin/unload` races with in-flight requests — new requests in the 500ms pre-exit window re-trigger model load then get killed. **Not fixed yet.** |
| 10 | `daemon.py:272-279` | P1 | JSONL tool-call log writes without lock/fsync/rotation — can interleave above PIPE_BUF (~4KB) producing corrupt lines; unbounded growth. **Not fixed yet.** |

**Dead / deprecated code deleted (7 files):**
- `scripts/churn_llm_judge.py` (deprecated Haiku stub — git history preserves it)
- `scripts/_opus_judge_results.py` (one-off Opus verdict dump, already materialized into `judge_opus_v8_vs_base.jsonl`)
- `scripts/update_all.sh`, `scripts/update.sh` (orphan duplicates of `full_update.sh`)
- `scripts/eval.py` (orphan; name-collides with eval_harness.py / eval_finetune.py / eval_verdict.py)
- `scripts/install_launchd.sh` (orphan; `setup_wizard.py::install_launchd()` supersedes it)

### Methodology gaps (not code bugs — document, design fix later)

| Area | Critical gap | Evidence |
|---|---|---|
| Eval gate | **GT is repo-level, not file-level** — user intent is file-level but gate rewards "repo made top-10" even when the target file is absent | `eval_finetune.py:128,330-334,475` |
| Eval gate | **"improved" = ±5pp r@10** — binary on 46% of tickets where `n_gt_repos=1`. `net_improved` is dominated by those; multi-repo tickets (where real retrieval quality matters) are downweighted | `eval_verdict.py:119-121` |
| Eval gate | **Test set is 4 tickets / 101 rows** (PI-54=79, CORE-2644=15, HS-257=7, PI-48=0); PI alone owns 78% | `finetune_data_v8/manifest.json` + train.jsonl sample |
| Eval gate | **Eval pool ≠ production pool by default** — `USE_HYBRID_RETRIEVAL=0` uses FTS5-only pool without code_facts / env_vars / content-type boosts | `eval_parallel.sh:39` |
| Eval gate | **Train/test distribution mismatch** — 87% diff positives in test (mean 226 char) vs mix in train; runtime returns ~1000-char chunks | sample of train.jsonl + test.jsonl |
| Eval gate | **Queries use Jira `description` (~478 char mean)** — runtime queries are 30-80 chars | `prepare_finetune_data.py::build_query_text` |
| Churn metrics | **`pct_high_churn_at_10=70.75%` is direction-less** — counts any reshuffle, can't distinguish improvement from regression | `churn_replay.py:227-228` |
| Churn metrics | **MiniLM judge is prose-biased, not neutral** — MS MARCO pretraining pushes it to prefer the doc list even on engineering queries (confirmed by 20/23 Opus-b → MiniLM-a flips) | `churn_reranker_judge.py:87` |
| Benchmarks | **Zero doc-intent queries in gold** — v8's doc-strip regression can never be caught by `benchmark_queries.py` or `benchmark_realworld.py` | `benchmarks.yaml` — every gold query expects a code repo |
| Benchmarks | **Structural overfit** — `GROUND-TRUTH.md` explicitly lists the LOO tickets = benchmarks.yaml; any change that reorders those wins by construction | ROADMAP.md:456 + `finetune_data_v8/` LOO files |
| Benchmarks | **Provider coverage 3/17** — gold tests Trustly/EPX/Worldpay only; EPX/Worldpay are effectively dead in real traffic (1/0 mentions of 400); 38% of real traffic uncovered | real_queries/sampled.jsonl vs benchmarks.yaml |
| MCP surface | **`trace_impact` used 3 of 2412 calls (0.1%)** — DELETE; `visualize_graph`, `context_builder` similar; `trace_flow`+`trace_chain` should MERGE | logs/tool_calls.jsonl analysis |

### Recall lever status (what stays, what to revisit)

| Lever | Status | Note |
|---|---|---|
| v8 FT reranker | **KEEP** | +8.3pp runtime, +5.09pp Jira w/ fallback; prod deployed 2026-04-21 |
| Conditional `--fts-fallback-enrich` | **KEEP** | Retrieval unlock on 77/909 tickets |
| Hybrid FTS+vector RRF | **KEEP** | Production pipeline |
| `reranker_override` kwarg (P0a) | **KEEP** | Enables eval parity |
| suggestions.py `node_type` fix (P0b) | **KEEP** | 629 zero-result queries recovered |
| P1c `_DOC_QUERY_RE` extension | **KEEP** | 3/9 doc recoveries measured; no regression in 410 tests |
| P1c `_CI_PATH_RE` + `CI_PENALTY=0.5` | **KEEP** | 3/6 CI files pushed out on #2; real code reaches top-10 |
| P4.2 `RERANK_POOL_SIZE` (200) | **KEEP** | +10pp baseline gain historical |
| P0c `code_facts` wiring | **REVISIT** | A/B disproved as cause of -10pp hybrid regression but positive contribution also unproven; measure in clean A/B |
| P0c `env_vars` boost | **REVISIT** | Same — partial effect visible in churn (80% top-1 flip on uppercase-id queries) but no isolated gain |
| Content-type boosts (gotchas/ref/dict) | **REVISIT** | Shipped pre-v8, never independently A/B'd after FT; may amplify docs v8 is trained to demote |
| P4.1 doc/test/guide penalties | **REVISIT** | Dominant loss mode source; partially mitigated by P1c |

### Action plan (short-term)

1. Land audit fixes (landed): `env_vars` acronym filter, `fts.py` word-boundary operators, 7 dead files deleted. 410/410 tests green.
2. Next-session priority: the 3 P0 index/daemon bugs (rowid collision, chunk_meta gap, /admin/unload race) — these affect correctness and aren't fixable via penalty tuning.
3. Before v12 FT: human-label `v12_candidates.jsonl` (pending), add runtime paired-preference bench, separate GT to file-level, fix test-set size.
4. MCP surface cleanup is a separable sprint — merge `trace_flow` into `trace_chain`, delete `trace_impact`/`visualize_graph`/`context_builder` from MCP (keep daemon-only), redesign `analyze_task` result shape.

## 2026-04-22 P1b.2/P1b.3 dual judge: judges disagree on docs↔code pairs

### Why two judges

Original `scripts/churn_llm_judge.py` (commit `238be6f4`) was a **Haiku-based LLM judge — incompatible with project policy** (no external LLM APIs; cf. `scripts/autoresearch_loop.py`: _"No LLM. Safe to run in daytime"_). Replaced by a **local reranker-as-judge** (`scripts/churn_reranker_judge.py` using `cross-encoder/ms-marco-MiniLM-L-6-v2`) that fits the local-only policy and is reproducible without any API key.

### P1b.3 Reranker-judge verdict — prose-biased

Neutral local scorer: `cross-encoder/ms-marco-MiniLM-L-6-v2` (different architecture + different pretraining than the gte-modernbert lineage used for base/v8; not fine-tuned on our Jira distribution). For each diff pair the judge fetches one `chunks.content` per `(repo_name, file_path, chunk_type)`, builds `(query, f"{repo} {file} {snippet}")` pairs for all 20 items, scores through the neutral CrossEncoder, and declares the winner by `sign(mean(v8) - mean(base))` with a `|margin| < 0.03` tie band.

```bash
python3.12 scripts/churn_reranker_judge.py
# -> profiles/pay-com/churn_replay/judge_reranker_v8_vs_base.jsonl (50 entries)
# -> profiles/pay-com/churn_replay/judge_reranker_summary.json
# run time: ~35s on MPS, 0 USD
```

| Metric | Value |
|---|---:|
| n | 50 |
| v8_wins (b) | 8 (16%) |
| base_wins (a) | 40 (80%) |
| ties | 2 (4%) |
| net_direction | **-0.64** |

### Judge disagreement pattern

Exact agreement between Opus and MiniLM: **19/50 (38%)**. The disagreement is systematic, not random. **The 20 Opus-b / MiniLM-a disagreements** are exactly the pairs where base returned docs and v8 returned code (`stripe-client.js`, `map-interac.js`, `apm-create.js`, etc.). Opus (trained on code + prose, coding-task instruction-tuned) reads "add stripe cash-app integration" as an engineering task and rewards the code list. MiniLM (MS MARCO pretraining on web passages, prose-heavy) scores the doc snippets higher on natural-language overlap with the query tokens.

**Neither judge is neutral on the axis this fine-tune targets.** Each judge's training distribution pre-commits the verdict.

### Why this matters for direction signal

- The **churn** part of P1b still holds: v8 reshuffles 77% of top-1s on real queries → FT transfer is real.
- The **direction** part (better or worse) needs a source outside both judges' training distributions. On this project, that source is ground-truth evaluation:
  - `gte_v8_fallback.json` — Jira eval, 909 tickets, v8 r@10 = 0.7622 vs base 0.7112 → **+5.09pp (gold label)**
  - `profiles/pay-com/bench/realworld` — runtime benchmark → **+2.1pp (manual labels)**
- These gold-label signals were the basis for the **2026-04-21 v8 prod deploy**, and they already answered the direction question positively.

### What stands regardless of judge disagreement

**P1c is not judge-dependent.** The 11 doc-intent query failures (query tokens `checklist`/`framework`/`severity`/`rules`/`sandbox`/`matrix`/`documentation`/`overview`) and the 1 `ci/deploy.yml` false positive are objective mismatches between user intent and returned results — you can read the pairs in `diff_pairs.jsonl` and see the intent mismatch without any scorer. P1c fixes them at the penalty layer without needing to pick a judge.

### P1c validation outcomes (9 affected pairs, MPS re-run)

| Pair | Query | Outcome | Notes |
|---|---|---|---|
| #4 | impact audit severity verification | ✅ DOC_RECOVERED | overlap 0.40 vs stored; impact-audit-rules.md surfaced |
| #10 | integration checklist webhooks lifecycle | ✅ DOC_RECOVERED | overlap 0.70; provider-integration-checklist.md back |
| #19 | investigation framework | ✅ DOC_RECOVERED | overlap 0.80; investigation-framework.md back |
| #42 | integration checklist APM_TYPES connections | ~ ALREADY_PRESENT | overlap 0.30; doc was already findable post-shuffle |
| #2 | ach provider service integration repo | ~ CI_PARTIAL | 3/6 CI files pushed out; `grpc-banks-crb::methods/ach-payment.js` now reaches top-10 |
| #5 | APM integration recipe checklist boilerplate | ✗ UNCHANGED | query triggers regex → penalty off, but v8 reranker still strips the doc — **v12 FT material** |
| #13 | openfinance APM integration documentation | ✗ UNCHANGED | same reason as #5 |
| #38 | payper sandbox testing magic values | ✗ SHUFFLED_NO_TARGET | reranker shuffled, sandbox doc still not in top-10 |
| #46 | provider code rules cross-service webhook auth | ✗ SHUFFLED_NO_TARGET | ambiguous query; no clear target doc |

**Summary:** 3 clean recoveries, 1 partial (CI), 1 doc already present, 4 unchanged/shuffled. **33% recovery rate is lower than the 47% forecast** because `_DOC_QUERY_RE` only neutralizes the penalty — it does NOT force v8 to rank docs higher. On 4 pairs (#5, #13, #38, #46) the v8 reranker alone (without any penalty) still ranks code above the doc the user asked for. This is **reranker-level behavior** that cannot be fixed by penalty heuristics — it is the explicit training target for v12 FT.

### Next levers

| # | Action | Cost | Expected | Status |
|---|---|---|---|---|
| **P1c code** | Extend `_DOC_QUERY_RE` + add `_CI_PATH_RE` + `CI_PENALTY=0.50` | 1h | Mechanically covers 9/19 base-wins | **LANDED** |
| **P1c validation** | `scripts/churn_p1c_validate.py` — measured outcomes above | 3-5 min MPS | — | **DONE** |
| P2 (v12 FT) | Retrain with doc-intent positives, CI-yml negatives | 2-3d | +3-5pp r@10 above v8 if P1c lands first | **awaiting gold-label set** |
| P3 | v8+base ensemble (score averaging) — untried | 4-6h | +2-3pp possible | parked |

### P2-prep: v12 gold-label candidates

**Candidates materialized 2026-04-22:** `scripts/v12_candidates.py` runs current v8 + P1c hybrid pipeline on 22 real MCP queries. Output: `profiles/pay-com/v12_candidates.jsonl` (230 rows). Each row has empty `label` + `note` fields for a reviewer to fill with `+` / `-` / `?`.

**Risk checks before v12 training** (per `feedback_pretrain_sample_check.md`):
- [ ] Compare 5 train / 5 test positive rows by hand to catch distribution mismatch.
- [ ] Ensure `query_uses_description` is NOT applied to real-query rows.
- [ ] Anti-leakage check: no overlap between labeled real queries and test_tickets.
- [ ] Keep prior 61250 v8 train rows + add labeled real-query rows; do NOT drop v8 data.

## 2026-04-22 P1b churn replay: v8 vs base on 400 real MCP queries

**Signal: v8 makes substantive changes on real queries** — the Jira-trained FT IS reaching the runtime distribution.

Setup:
- Queries: `profiles/pay-com/real_queries/sampled.jsonl` (400 real MCP search calls).
- Pipeline: `scripts/churn_replay.py` — run each query through `src.search.hybrid.hybrid_search` twice via `reranker_override` (base + v8), diff top-10 by `(repo, file_path)`.

### Aggregate metrics

| Metric | Value | Interpretation |
|---|---:|---|
| `mean_overlap@1` | 0.23 | Top-1 matches on 23% of queries; differs on 77% |
| `mean_overlap@10` | 0.35 | Only ~3.5 of 10 results shared on average |
| `pct_top1_changed` | 77.0% | How often v8 picks a different #1 |
| `pct_high_churn_at_10` | 70.75% | Queries with overlap@10 < 0.5 |

### What this tells us

1. **FT transfer is real.** 77% top-1 change is far too high to be noise.
2. **Direction is mixed qualitatively** (see dual judge §).
3. **Next step = LLM-as-judge on top-50 diff pairs** (DONE 2026-04-22 — see dual judge §).

## 2026-04-22 investigation: hybrid regression on 103 lost tickets

Running `scripts/ab_lost_tickets.py` with three A/B gates on the "lost" subset:

| Variant | r@10 | Conclusion |
|---|---|---|
| Control | 0.0000 | reproduces snapshot |
| `CODE_RAG_DISABLE_PENALTIES=1` | 0.0049 | **penalties NOT the cause** |
| `CODE_RAG_DISABLE_CODE_FACTS=1` | 0.0049 | **code_facts/env_vars NOT the cause** |
| `AB_ENRICHED_ALWAYS=1` on BO-798 | 1.0000 | enriched query recovers GT |

Enriched-always rescued 5 tickets but lost 10 other tickets — net regression. NOT a drop-in cure.

### Decisions

- `fts_fallback_enrich` in `eval_one_model_hybrid` reverted (commit `67f45a2` — A/B disproved it).
- Investigation env gates (`CODE_RAG_DISABLE_{PENALTIES,CODE_FACTS}`) committed for future A/B work.
- v12 FT NOT started — first need a stable measurement.

## 2026-04-21 late-evening: P0a+P0b+P0c landed

Three commits / six commit-sha pushed via MCP:

| Commit | Scope | Behaviour change |
|---|---|---|
| `595a06b` | P0b — `src/search/suggestions.py` | `WHERE node_type='repo'` → drop WHERE. 629 zero-result queries affected. |
| `9dfaac5` | P0c — `src/search/{code_facts.py,env_vars.py}`, `src/search/hybrid.py` | `_apply_code_facts` + `_apply_env_vars` wiring. 20 new tests. |
| `51c9181` | P0a — `src/search/hybrid.py` | `rerank()` + `hybrid_search()` accept `reranker_override` kwarg. |
| `5b71220` | P0a — `scripts/eval_finetune.py` | `_CrossEncoderAdapter`, `eval_one_model_hybrid()`, `--use-hybrid-retrieval` flag. |

---

## 🎯 2026-04-21 evening: Conditional enriched FTS fallback — CONFIRMED +5.09pp v8 Δr@10 full-eval

**Full eval completed** (`gte_v8_fallback.json`, 909 tickets). Verdict: **PROMOTE** (Δr@10=+0.051, ΔHit@5=+0.079, net=+100).

| Metric | Old (no fallback) | New (with fallback) | Δ absolute |
|---|---:|---:|---:|
| baseline r@10 | 0.6527 | **0.7112** | **+5.85pp** |
| v8 r@10 | ~0.6955 | **0.7622** | **+6.67pp** |
| v8 Hit@5 | ~0.8425 | **0.9131** | **+7.06pp** |

**v8 advantage over baseline is PRESERVED and slightly amplified with fallback enabled.**

### Post-breakthrough re-audit (5 agents)

| # | Finding | Severity |
|---|---|---|
| 1 | **Eval pipeline ≠ production retrieval.** 11 prior FT iterations tuned to wrong candidate pool. | P0 BUG |
| 2 | **Free lunch — unused tables.** `code_facts_fts` + `env_vars` existed in knowledge.db but never read. | P0 |
| 3 | **Mini bug.** `src/search/suggestions.py:72` — `WHERE node_type = 'repo'` but actual column is `type`. | bug |
| 4 | **Null_rank headroom.** 42 tickets have GT outside top-200 → +4.62pp locked headroom. | P1 |
| 5 | **Runtime transfer signal = top-K churn replay.** Real queries structurally close to fallback bucket. | P1 |
| 6 | **v12 FT verdict — mixed.** Defer until P0a retrieval parity lands. | decision |

---

## ✅ 2026-04-21 morning: v8 DEPLOYED to production

User decision: deploy v8, purge rest. Only v8 gave real runtime improvement (+8.3pp queries, +2.1pp realworld vs baseline); v10/v6.2/v9/v11 tied baseline on benchmarks.

- `profiles/pay-com/config.json::reranker_model` → absolute path to `reranker_ft_gte_v8`.
- Daemon restarted via `/admin/unload`. v8 loaded successfully.
- Purged v9, v10, v11 models (~6GB), `finetune_data_v9/`, all v9/v10/v11 eval snapshots.

---

## Critical pitfalls — do NOT repeat (граблі)

1. **No `--dedupe-same-file`** — v5 catastrophe (−16.67pp).
2. **MANDATORY sample check** — 5 train + 5 test positive rows. 10 min gate prevents 6h train waste.
3. **No `--max-rows-per-ticket` below 300** — v6.1 killed CORE at cap=120.
4. **No wholesale `--skip-empty-desc-multi-file`** — drops 13 CORE monster-PRs.
5. **Don't combine 5 new flags at once** — v5 lesson. Isolate each change.
6. **Both MPS env vars** — `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.8` AND `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.4`.
7. **`--history-out` no shard suffix** — `eval_finetune.py` appends `.shardNofN.json`.
8. **Env vars for DB paths** — `CODE_RAG_HOME=/Users/vaceslavtarsevskij/.code-rag-mcp`, `ACTIVE_PROFILE=pay-com`.
9. **Checkpoint resume requires same batch size** — HF Trainer bug.
10. **Eval metric is repo-level, not file-level** — r@10 is over ~70 repos, not 909 files.
11. **v7 lesson: don't iterate FE clusters sequentially** — single repo dominates.
12. **Run critics BEFORE implementation** — v7 hypothesis was wrong.
13. **Eval pipeline ≠ production.** Always verify eval candidate pool matches serving pool.
14. **Pre-commit pytest flakes under MPS contention.** Verify manually — do NOT `--no-verify`.
15. **MCP push_files requires FULL file content** per commit. Never assume origin == local HEAD.
16. **Blanket enriched query mode LAMEs FTS candidates** (−15pp on PI). Only CONDITIONAL fallback safe.
17. **The "rerank ceiling" claim after 11 iterations was WRONG.** 8.5% of tickets had 0 FTS candidates.
18. **Do NOT run eval_parallel.sh with EVAL_QUERY_MODE=enriched + USE_HYBRID_RETRIEVAL=1 + 3 shards.** MPS deadlock.
19. **Never use `tee` with long-running Python progress output.** Use `python3.12 -u` with direct `> file`.
20. **13-agent audit 2026-04-22** — 4 P0 bugs still open (rowid collision, /admin/unload race, code_facts_fts drift, FTS optimize misuse). Eval methodology has 6 systemic gaps (GT repo-level, binary improved, 4-ticket test, eval≠prod pool, query uses description, benchmarks overfit).

---

## Production state

- Reranker: `reranker_ft_gte_v8` (listwise LambdaLoss, 285MB bf16) — absolute path in `profiles/pay-com/config.json`.
- Hybrid retrieval: FTS5 (150) + CodeRankEmbed dense (50) → RRF → rerank top-200 → top-K.
- **Canonical baseline**: r@10 = 0.7112 / Hit@5 = 0.8339 (with `--fts-fallback-enrich`).
- **v8 eval**: r@10 = 0.7622 / Hit@5 = 0.9131 (with `--fts-fallback-enrich`).
- Base model for FT: `Alibaba-NLP/gte-reranker-modernbert-base` (149M params).

---

## Context for new session

- 16GB M-series Mac. MPS acceleration. One-epoch FT = ~75-100 min on 60k rows.
- Daemon on :8742 manages ML models in production. Unload before training.
- User is the only dev; commits via `mcp__github__*` tools (gh deny-listed).
- Test suite (410 tests as of 2026-04-22) must pass: `python3.12 -m pytest tests/ -q`.
- 4 P0 bugs queued for next session: hybrid.py rowid collision, daemon.py /admin/unload race, repo_indexer.py code_facts_fts drift, orchestrator.py FTS5 optimize misuse.
- `v12_candidates.jsonl` (230 rows) awaits human labeling before v12 FT can start.
