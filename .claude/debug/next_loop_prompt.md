# Prompt для нової автономної Karpathy-loop сесії

> **Скопіюй текст нижче як перший user message і запусти `/loop` без інтервалу.**

---

Karpathy-style автономний research loop через РЕАЛЬНИЙ `hybrid_search()` pipeline. Шукай реальні bench-improvements; кожна гіпотеза перевіряється end-to-end (НЕ через `benchmark_doc_intent.py` яке має `router_bypassed: True`). Stop conditions: 8h wallclock OR disk free <10GB OR 3 consecutive bench errors.

## ОБОВ'ЯЗКОВО ПРОЧИТАЙ ПЕРШИМ

1. `.claude/debug/overnight_log.md` — повний лог попередньої loop сесії (54 ticks). Особливо **Tick 55** — root-cause чому попередня recommendation INVALIDATED.
2. `.claude/debug/comfort_routing_proposal.md` — INVALIDATED proposal (top stamp). Зрозумій що НЕ робити.
3. `.claude/debug/run2_final_report.md` — Run 1+2+3 train cycle history.
4. `~/.code-rag-mcp/CLAUDE.md` — project-rules.
5. `~/.claude/CLAUDE.md` + `~/.claude-personal/CLAUDE.md` — global/personal rules.

## КРИТИЧНІ УРОКИ З ПОПЕРЕДНЬОЇ СЕСІЇ — НЕ ПОВТОРЮЙ

1. **`scripts/benchmark_doc_intent.py` НЕ ВІДОБРАЖАЄ production behavior.** Він має `router_bypassed: True` hardcoded і використовує pure vector→rerank. **НЕ роби висновки про routing/stratum-gate з його cached benches.** Минула сесія так зробила і отримала -6.21pp regression замість +1.45pp.
2. **Завжди використовуй `scripts/bench_routing_e2e.py`** (написаний попередньою сесією, проходить через real `hybrid_search()`) для будь-якої гіпотези щодо routing/rerank/stratum-gate. Він повільний (~5min для n=161, ~30min для n=908) — закладай це у cadence.
3. **Per-query simulation valid ONLY коли candidate pool identical between variants.** Зміна OFF-set ЗМІНЮЄ candidate pool у hybrid_search (skip → RRF top-10; run → reranker reorders). НЕ можна симулювати це з cached benches.
4. **Cached vector→rerank benches OK для embedding-tower compares (без routing change)** — там pool не залежить від routing. Але для будь-якого rerank/route experiment — e2e only.
5. **Bootstrap CI з лower bound > 0** — обов'язковий gate для будь-якого ACCEPT. NOISE = REJECT.
6. **NEVER push до remote main без e2e validation.** Local changes + revert OK; push без bench — заборонено.

## ПОТОЧНИЙ СТАН РЕПО (на 2026-04-27 11:55 EEST)

- **Local**: чисто, hybrid.py md5 `c2e1b2a7`, pytest 1023/1023.
- **Remote main HEAD**: `92f8c989` — Tick 0 push з попередньої сесії що завершив routing wiring (`intent = "docs" if _query_wants_docs(query) else "code"` у rerank()). Per cached simulation = no-op на jira; per e2e = UNMEASURED.
- **Open question for new session**: revert Tick 0 чи залишити? Потрібен e2e bench pre/post щоб знати.

## ХАРАКТЕРИСТИКА ПОТОЧНОГО PIPELINE (production)

```
src/search/hybrid.py::hybrid_search(query, ...):
  1. FTS5 keyword search (top 150)
  2. LanceDB vector search (top 50, two-tower routing via _query_wants_docs)
  3. RRF fusion (key=fts:rowid|vec:rowid)
  4. code_facts + env_vars boosts
  5. content_type boosts (gotchas, task, reference)
  6. Sort by RRF score, take top-200 candidates
  7. Stratum-gate: if doc_intent AND stratum in OFF → return top-10 RRF (skip rerank)
  8. else: rerank() — applies CrossEncoder, combines 70% rerank + 30% RRF, applies penalties
```

Поточний stratum gate (LOCAL hybrid.py): OFF = {webhook, trustly, method, payout}, KEEP = {nuvei, aircash, refund, interac, provider}. (Remote має narrower OFF = {webhook, trustly}.)

## АРТЕФАКТИ ВІД ПОПЕРЕДНЬОЇ СЕСІЇ

- `bench_runs/v2_with_comfort_routing.json` — n=161 e2e з comfort routing applied (REGRESS)
- `bench_runs/v2_baseline_e2e.json` — n=161 e2e baseline (apples-to-apples)
- `bench_runs/v2_calibrated_{L6,l12,NO_RERANK}.json` — n=161 cached vector→rerank (БУДЬ ОБЕРЕЖНИЙ — bypass-bench)
- `bench_runs/jira_n900_{prod_L6,l12,mxbai,no_rerank}.json` — cached vector→rerank на jira
- `scripts/bench_routing_e2e.py` — RIGHT bench tool — використовуй цей
- `scripts/bootstrap_eval_ci.py` — bootstrap CI tool

## TICK 0 — ОБОВ'ЯЗКОВО ВИКОНАЙ ПЕРШИМ

Calibrate baseline через ПРАВИЛЬНИЙ pipeline. Запусти e2e bench на v2 calibrated (n=161, ~5-10 min) з ПОТОЧНИМ кодом:

```bash
cd ~/.code-rag-mcp
CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com python3 scripts/bench_routing_e2e.py \
  --eval=profiles/pay-com/doc_intent_eval_v3_n200_v2.jsonl \
  --out=bench_runs/v2_e2e_baseline_session2.json \
  --label=session2_baseline
```

Очікування: hit@10 ≈ 0.6087 (matched попередньому baseline e2e). Якщо суттєво відрізняється — перевір, чи repo не змінився, чи daemon не активний (моделі дублюватимуться).

Запиши baseline у новий лог `.claude/debug/loop2_log.md` як Tick 0.

## RESEARCH BACKLOG (по черзі, найвища info-density зверху)

Кожна гіпотеза = окремий tick. Per-tick protocol:
- 1 hypothesis (1 line)
- 1 experiment (config tweak + e2e bench OR analysis)
- bootstrap CI або per-query diff vs Tick 0 baseline
- verdict: ACCEPT / REJECT / NOISE / NEED-N
- log to `.claude/debug/loop2_log.md`

### Backlog (першочергові — re-test попередньої сесії знахідок через e2e)

1. **TICK 0 push impact**: ревертни `92f8c989` локально (git stash на hybrid.py rerank() block), bench e2e v2, порівняй vs current. Це скаже чи Tick 0 hurts/helps prod (попередня сесія НЕ виміряла).

2. **OFF stratum WIDE vs NARROW e2e**: поточний LOCAL = wide ({webhook,trustly,method,payout}); remote = narrow ({webhook,trustly}); proposal = тільки {webhook}. Bench всі 3 варіанти e2e на v2_calibrated_n200_v2 + jira_n900. Знайди optimal.

3. **Comfort routing БЕЗ stratum зміни**: чи допомагає тільки routing change без OFF gate change? Apply only the rerank() routing line change, leave OFF set wide. E2E bench. Минула сесія НЕ ізолювала ці два ефекти.

4. **Jira e2e bench (n=908)**: ~30 min. Перевір чи routing зміна впливає на jira-style queries в production pipeline.

5. **Rerank pool size sweep e2e**: `RERANK_POOL_SIZE` 50/100/200/300 з реальним hybrid_search. Минула сесія тестувала через bypass-bench → ймовірно не валідно.

6. **Penalty sweeps e2e**: TEST_PENALTY/GUIDE_PENALTY/CI_PENALTY/DOC_PENALTY [0, 0.1, 0.2, 0.3] на jira+v2 e2e. Знайди optimal config.

7. **KEYWORD_WEIGHT sweep e2e**: [1.5, 2.0, 2.5, 3.0]. RRF fusion balance.

8. **RRF_K sweep e2e**: [30, 60, 90].

9. **Two-tower routing accuracy on real prod queries**: query distribution з `logs/tool_calls.jsonl` — чи routing decision matches eval labels.

10. **Per-query L6 vs l12 e2e win/loss analysis** (не cached!): identify queries where each helps. Build classifier from REAL data, not bypass-bench data.

11. **Stratum-specific reranker selection** (через e2e): може певні strata otrymať кращий результат від `Tarshevskiy/pay-com-rerank-mxbai-ft-run1` чи `bge-reranker`.

12. **Prod query stratum distribution**: 3262 prod queries з `tool_calls.jsonl`; компонуй eval-set з PRODUCTION samples, не synthetic v3 одного типу.

### Backlog (медіум — потребують більше ресурсу)

13. Train classifier на per-query L6/l12 outcomes (e2e measured) → LR / sentence-transformer.
14. Score-based ensemble (blend l12_score + L6_score) замість binary choice.
15. Score-scale calibration: L6 outputs 5-7, l12 outputs 0.5-1.0; penalty=0.15 вплив відносний — re-tune penalties для l12 specifically.
16. Eval-set expansion (>=300 v2-calibrated рядків) для tighter CI.

### Backlog (LOW — потребують RunPod $)

17. RunPod fine-tune l12 для docs-domain robustness (зараз code-trained).
18. Train new docs-tower embedding (попередні training cycles failed — див. `project_training_cycle_failure_2026_04_26.md`).

## HARD RULES (повторно)

- **NEVER push to main без e2e POSITIVE bootstrap CI на v2 + jira**
- **NEVER use cached vector→rerank benches для routing/stratum hypotheses** (lesson Tick 55)
- **NEVER spend RunPod $ in this loop**
- **NEVER train new models in this loop** (eval/research only)
- **Push only via mcp__github__** (not gh, not git push). Owner = `vtarsh`, repo = `code-rag-mcp`.
- **Single Python compute process at a time** (16GB Mac); kill prior bench before next
- **Daemon health check at start of each tick**: `curl -s http://localhost:8742/health` — restart if needed (це підвищує overall throughput, моделі завантажуються один раз)
- **Disk monitor**: stop if `df -h ~/` shows < 10GB free

## CADENCE для ScheduleWakeup

- Active research, e2e bench takes 5-10min: 360-540s wakeup
- Long bench (jira n=908): 1800s wakeup, monitor file existence
- Idle / waiting: 1500-1800s
- НЕ обирай 300s — worst-of-both для prompt cache TTL

## STATE BETWEEN TICKS

- Завжди читай tail `.claude/debug/loop2_log.md` (last 50 lines)
- Tracking: одна гіпотеза за tick, не повторюй
- Якщо backlog вичерпано → пропиши 5 нових з observed pattern; продовжуй

## OUTPUT TO USER (final message before stop)

- Tick 0 baseline numbers
- Number of ACCEPTED hypotheses with deltas
- Outstanding ABORT reasons
- Recommended next action (revert Tick 0? push X? do nothing?)
- ОБОВ'ЯЗКОВО: usage of e2e bench for ALL claims

## Якщо хочеться зупинити early

Якщо знайшов чітку POSITIVE win (e2e bootstrap CI lower bound > 0 на ОБОХ v2 + jira), напиши proposal у `.claude/debug/`, апдейтни NEXT_SESSION_PROMPT.md, send PushNotification. НЕ push автоматично — чекай user confirm.
