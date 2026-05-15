# Next-Session Prompt — P7 (post-P6 close)

> Copy this verbatim into the first user message of a new Claude Code session. The new session starts with zero conversation memory; everything important is captured below or linked.

---

## Status snapshot — P6 closed 2026-04-25

- **P6 outcome:** BASELINE WINS. Vanilla `docs (nomic-ai/nomic-embed-text-v1.5)` залишається в production. 4 challengers відхилені на чесному eval-v3 (n=90), 1 був заблокований на upstream HF modeling.py — тепер розблоковано через U1 patch.
- **P6 deliverables (process gains):**
  - eval-v3 model-agnostic labeler (`scripts/build_doc_intent_eval_v3.py`)
  - 5-condition AND-gate (`scripts/benchmark_doc_intent.py --compare`)
  - normalize_embeddings fix
  - max_seq_length cap + LONG_BATCH env
  - RunPod skeleton (cost_guard, pod_lifecycle, prepare_train_data)
  - eval-v3 jsonl у private repo
  - **NEW (P6 close):** U1 monkey-patch — `_fix_gte_persistent_false_buffers()` у `src/index/builders/docs_vector_indexer.py`. Розблоковує gte-* family для будь-якого майбутнього A/B (no-op для nomic / CodeRankEmbed / arctic / bge-m3 / mxbai). Pytest 720+/720+ green.
- **Honest eval-v3 baseline:** R@10=0.2509, nDCG@10=0.3813, Hit@5=0.3778, p95=20ms, n=90 (n=150 plan див. нижче).
- **RunPod spend:** $1.70 of $15. **$13.30 banked**. Effective cap для P7 = **$11** (hold $2 safety margin).
- **Pytest:** 720+/720+ green (719 + U1 no-op test).

Full TL;DR + iteration table + commits-landed: `~/.code-rag-mcp/.claude/debug/final-report.md`. P6 debate verdict: `.claude/debug/debate-verdict.md` (synthesizer's final form). Memory entry: `project_p6_debate_verdict_2026_04_25.md`.

---

## Goal of P7 — domain-adaptive doc-tower attempt on hardened eval

**One swing, properly designed.** Не повторюємо patterns 4 попередніх rejection-ів. Цільова метрика: **Recall@10 ≥ 0.3509** (baseline 0.2509 + AND-gate +10pp), AND-gate усі 5 умов проходять, no per-stratum drop > 15pp.

P7 has two phases. Phase 1 — eval hardening + scaffolding (no spend). Phase 2 — single FT iteration на найкращому recipe (cap $5).

### Phase 1: Eval hardening + train infrastructure (no spend, ~6–10h human time)

1.1. **Grow eval-v3 from n=90 to n=150 — ✅ MOSTLY DONE in P6.**
   - Worker shipped `profiles/pay-com/doc_intent_eval_v3_n150.jsonl` (143 rows; per `eval-grow-stats.json` 50 picked + 43 labeled + 7 dropped).
   - Train-disjointness ALREADY enforced (9 train-jaccard rejections + 20 train-dup rejections).
   - **Remaining:** re-bench baseline R@10 on n=143 (cheap, ~2 min Mac CPU): 
     ```bash
     python3.12 scripts/benchmark_doc_intent.py --eval=profiles/pay-com/doc_intent_eval_v3_n150.jsonl --model=docs --no-pre-flight --out=/tmp/bench_v3_n143_docs.json
     ```
   - Optional: pick + label 7 more rows to hit n=150 even (label rate 7/8 historically).
   - Paired SE on n=143 = ±7.1pp vs n=90 ±9.0pp — power at +10pp delta improves from ~30% to ~50%.

1.2. **CM4 query-disjointness enforcement у `prepare_train_data.py`.**
   - Skeptic знайшов критичний gap: 100/100 eval-v3 queries присутні в `tool_calls.jsonl`. Path-disjointness НЕ запобігає silver-positive transduction leak.
   - Add assert у train-pair builder: `(q_lower NOT IN eval_v3_queries)` перед записом JSONL.
   - Test: `tests/test_prepare_train_data.py` — failing test, що ловить leaking pair.

1.3. **Loss-flag plumbing у `train_docs_embedder.py`.**
   - Поточний код hardcode-ить `MultipleNegativesRankingLoss`. Додати CLI flag `--loss=mnrl|cosent|marginmse|tsdae`.
   - Кожен loss має різний data-format очікуваний; додати mappers.
   - Pytest cov на кожен loss.
   - **Why:** 3 з 4 попередніх rejection — на MNRL family. Recipe-family escape є необхідним для p(win) > 0.10.

1.4. **`build_train_pairs_v2.py` — новий скрипт, ~150 LoC.**
   - Input: `logs/tool_calls.jsonl` filtered via `_query_wants_docs`.
   - For each prod query: run baseline vector retrieval + reranker; emit `(q, pos_rank_1-3, hard_neg_rank_11-30)` rows.
   - Hard negatives через `reranker_ft_gte_v8` (production aligned).
   - Enforces query-disjointness vs eval-v3 expected_paths AND queries.
   - Idempotent + deterministic seed.
   - Realistic effort: 4–6h human time, 1–2h reranker mining (CPU або pod).

### Phase 2: Single FT iteration (cap $5, kill at Stage 1)

2.1. **Recipe selection** (re-run debate в P7 з оновленими eval/SE/disjointness facts):
   - **R1 TSDAE→CoSENT+HN-A** залишається топ-pick з рекомендацій recipe-architect (after honest p(win) recalc у skeptic = 0.07; з grown eval + query-disjointness fix p(win) ≈ 0.10–0.12).
   - **Альтернатива A:** domain-adaptive contrastive на full prod-query-log (per pivot-strategist §7 footnote). p(win) на eval-v3-n150 ≈ 0.30–0.40 за strategist оцінкою — але цифра НЕ перевірена; treat як upper bound.
   - **Альтернатива B:** вшити mxbai-embed-large-v1 base-swap (no FT) як 5-ту перевірку на свіжому eval-v3-n150. Cost $1; якщо програє — закриває "non-nomic base" гіпотезу.
   - Decision: вибрати **один** (не два) recipe в наступному debate. Рекомендація — base-swap-first (mxbai), бо infrastructure risk = 0 і це чистий сигнал per pivot-strategist Information-Value.

2.2. **Pod cycle:**
   - cost_guard cap $5.0
   - Stage 1 smoke (50-row probe) — kill if Δr@10 < -0.03.
   - Stage 2 full bench на eval-v3-n150 — AND-gate decides DEPLOY:yes/no.
   - HF Hub upload приватним repo з версійним tag.

2.3. **Decision:**
   - +10pp clear AND-gate (на eval-v3-n150) → freeze, deploy.
   - +5pp до +9pp лифт без clear gate → ship-as-process-gain (RECALL-TRACKER row), no daemon swap.
   - 0 to +5pp або negative → close P7 з 5-м rejection, prior firms to 1/11=0.09.

### Stop conditions for P7

- $11 effective cap reached → freeze best-so-far, write closure report.
- 1 iteration не покращує → close P7 з рекомендацією P8 (router term-whitelist per pivot-strategist c3, p(any positive lift) = 0.78).
- AND-gate clean win → freeze, deploy, NEXT_SESSION_PROMPT шукати наступну вісь.
- Eval-v3 grow blocks (e.g. labeler bug discovered) → revert до n=90, rerun debate з пониженою впевненістю.

---

## Open questions for P7 debate (re-run у new session)

1. mxbai single-base-swap iteration vs domain-adaptive contrastive — який ризик/винагорода кращі під n=150 SE?
2. Чи треба перебудовувати reranker_ft_gte_v8 під новий eval-v3 (поточний v8 trained на eval-v1 ground truth)?
3. Router term-whitelist (pivot-strategist c3) — чи паралельно P7, чи після P7?
4. Чи варто розширити eval-v3 до n=200 (ще +50 prod) — ±6pp SE — перед FT, чи це diminishing returns?

---

## How to start P7

```
запусти дебати recipe-improvement-v2
```

Або вручну: TaskCreate 4 tasks (recipe-architect, gte/mxbai-unblocker, skeptic, synthesizer) + spawn 3 opus teammates по template `~/.claude-personal/templates/debate-prompt.md`. Контекст для нового debate:

- eval-v3-n150 grown (path: `profiles/pay-com/doc_intent_eval_v3_n150.jsonl`)
- query-disjointness enforced
- U1 patch landed (gte-* family unblocked)
- Loss-flag plumbing landed
- 4 rejections + 1 base-swap rejection (якщо mxbai дано P6 closure) — оновити Jeffreys прийнятні.

---

## Critical operating rules (verify before any action)

- **Push via MCP only** — `mcp__github__push_files` / `create_or_update_file` / `delete_file`, owner=`vtarsh`. `gh` and `git push` forbidden.
- **For push of large files (≥100 lines) — Read+strip+md5-verify** (див. `feedback_bash_cat_truncates.md`).
- **No external LLM APIs** — local stack only (CodeRankEmbed + nomic-embed-text-v1.5 + MiniLM CrossEncoders).
- **MCP push_files size cap:** ≤5 files/push OR ≤3 if any ≥500 lines.
- **NEVER >1 Python compute process** на 16 GB Mac. Sequential only.
- **V8 reranker stays prod.** Two-tower піднімає лише vector leg.
- **Agent hallucination ~30% rate** — md5/wc/grep before treating "I modified file X" as truth.

---

## Active artifact paths (single source of truth)

| Artifact | Path |
|---|---|
| Final loop report (P5+P6) | `~/.code-rag-mcp/.claude/debug/final-report.md` |
| P6 debate verdict | `~/.code-rag-mcp/.claude/debug/debate-verdict.md` |
| P6 debate inputs | `debate-recipes.md`, `debate-gte-unblock.md`, `debate-skeptic.md`, `p6-pivot-strategist.md` |
| Iteration journal | `~/.code-rag-mcp/.claude/debug/loop-log.md` |
| Loop state JSON | `~/.code-rag-mcp/.claude/debug/loop-state.json` |
| Phase-6 root cause | `~/.code-rag-mcp/.claude/debug/p6-verdict.md` |
| Eval set v3 (n=90) | `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3.jsonl` |
| Eval set v3 (n=143, grown 2026-04-25) | `~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3_n150.jsonl` (file size: 93KB; 143 rows) |
| Eval grow stats | `~/.code-rag-mcp/.claude/debug/eval-grow-stats.json` (drop_stats + strata counts) |
| RECALL-TRACKER | `~/.code-rag-mcp/profiles/pay-com/RECALL-TRACKER.md` |
| Bench JSON baselines | `/tmp/bench_v3_docs*.json` (regenerate via `scripts/benchmark_doc_intent.py`) |
| RunPod tooling | `~/.code-rag-mcp/scripts/runpod/*` |
| HF Hub historical models | `Tarshevskiy/pay-com-docs-embed-{v0, v1, v1-fixed}` |
| Memory: P5 final | `~/.claude-personal/projects/-Users-vaceslavtarsevskij--code-rag-mcp/memory/project_loop_2026_04_25.md` |
| Memory: P6 close | `project_p6_debate_verdict_2026_04_25.md` (TBD) |

---

## Env + command reference

### Build/rebuild
```bash
# Incremental:
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make update
# Full rebuild (2-4h, peak ~10 GB RAM with memguard):
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com make build
# Docs tower only (~25 min on MPS):
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3.12 scripts/build_docs_vectors.py --force
```

### Daemon
```bash
kill -9 $(lsof -ti:8742); sleep 2
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 daemon.py &disown
curl -X POST http://localhost:8742/admin/unload    # reversible unload
curl -X POST http://localhost:8742/admin/shutdown  # shutdown + launchd respawn
curl http://localhost:8742/health | jq
```

### Launchd
```bash
launchctl list | grep code-rag-mcp
launchctl print gui/$(id -u)/com.code-rag-mcp.weekly-rebuild
launchctl start com.code-rag-mcp.weekly-rebuild
```

---

## What did NOT change in P6

- `src/models.py` `docs` entry — still `nomic-ai/nomic-embed-text-v1.5`
- Production daemon — no restart needed
- v1, v2 eval files — preserved as historicals
- LanceDB indices — `db/vectors.lance.docs/` unchanged
- Reranker — `reranker_ft_gte_v8` stays prod

---

## Memory files critical for P7 context

- `project_loop_2026_04_25.md` — P5 full record (18 iterations, 4 rejections)
- `project_p6_debate_verdict_2026_04_25.md` — **NEW**: P6 close (debate, U1 patch, eval-v3 grown plan)
- `project_two_tower_v13_landed.md` — what's deployed + how to use
- `project_v12a_rejected_two_tower_pivot.md` — root cause why pivoted from single-tower FT
- `reference_launchd_schedules.md` — active plists + schedule rationale
- `feedback_bash_cat_truncates.md` — MCP push big-file pattern (Read+strip+md5-verify)
- `feedback_agent_hallucination_detection.md` — md5 workflow
- `feedback_push_files_size_cap.md` — ≤5 files/push
- `feedback_push_via_mcp_not_gh.md` — MCP push only
- `feedback_no_external_llm_apis.md` — local-only stack

---

## Pre-flight before P7 actions

```bash
cd ~/.code-rag-mcp
python3.12 -m pytest tests/ -q                                  # 720+/720+ expected
python3.12 scripts/runpod/cost_guard.py --check 5.0             # OK ≤$11
test -f profiles/pay-com/doc_intent_eval_v3.jsonl               # exists
test -f profiles/pay-com/doc_intent_eval_v3_n150.jsonl || echo "P7 Phase 1.1 not run yet"
ls ~/.runpod/credentials                                         # has RUNPOD_API_KEY + HF_TOKEN
```
