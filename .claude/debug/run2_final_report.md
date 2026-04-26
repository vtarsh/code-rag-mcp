# Run 1 + Run 2 + Jira-grounded eval — фінальний звіт (2026-04-27)

## Що ми зробили
3 RunPod cycles (Run 1 → Run 2 → Run 3), 6+ тренувань, jira-grounded eval n=908 побудовано, повний bootstrap CI, planning + architecture debates.

## Найкращий результат за день — РОУТИНГ замість заміни
Один реранкер на все = trade-off. Розділення інтенту дає реальний приріст:

| Шлях | Embedding | Реранкер | Чому |
|---|---|---|---|
| **Code-intent query** | CodeRankEmbed (прод) | **l12 FT** (Tarshevskiy/pay-com-rerank-l12-ft-run1) | +3.31pp top-10 vs прод L6 на n=908 (POSITIVE ✅) |
| **Docs-intent query** | nomic (прод) | **none** (skip rerank) АБО mxbai FT (=identical) | реранкер L6 ШКОДИТЬ docs (-4.5pp top-10) |

**Зважений combined (47% docs / 53% code за прод стат):**
- Поточний прод (L6 на обох): top-5=31.96%, top-10=39.91%
- З роутингом (best): top-5=32.13%, top-10=43.13%
- **Δ: top-5 +0.17pp, top-10 +3.22pp**

## Все нажите за день — bench результати

### Реранкери на коді (n=908 jira ground-truth, bootstrap CI)
| Модель | top-5 | top-10 | vs прод L6 (CI) |
|---|---|---|---|
| **l12 FT** | **24.12%** | **33.81%** | **POSITIVE +3.31pp** ✅ |
| прод L6 | 23.35% | 30.51% | baseline |
| mxbai FT (= no_rerank) | 20.81% | 26.43% | NEGATIVE -4.08pp ❌ |
| no rerank (raw coderank) | 20.81% | 26.43% | NEGATIVE -4.08pp |

### Реранкери на доках (n=192, bootstrap NOISE на цьому n)
| Модель | top-5 | top-10 |
|---|---|---|
| прод nomic БЕЗ реранкера | 41.15% | **53.65%** |
| mxbai FT (= no rerank) | 41.15% | 53.65% |
| прод L6 | 41.67% | 50.52% |
| l12 FT | 35.94% | 44.79% (-9pp **гірше**) |

### Docs tower кандидати (заміна nomic)
| Модель | Стан | top-5 | top-10 |
|---|---|---|---|
| **прод nomic** (baseline) | — | 45.6% (n=90) | 57.8% |
| docs-gte FT | DONE | 44.4% | 54.4% (-3pp) |
| docs-nomic FT | TRAIN ✅ + HF push ✅ + build TIMEOUT (2h) | — | — |
| docs-mxbai-large FT | CRASHED Bug 6o (gradient explosion → NaN) | — | — |

## Bug log (Bug 6 series)
| Bug | Опис | Стан |
|---|---|---|
| 6a-6n | scp/tar/HF token/loss compat — 11 інфра-багів | ✅ всі fix landed |
| 6o | mxbai-large + ST.fit() → NaN (gradient explosion, loss-agnostic) | ❌ root unfixed, defer |
| 6p | ST.save() для nomic-bert додає `encoder.encoder.X` префікс → reload broken | ✅ ROOT FIX landed (`27afc27`): bypass save_pretrained, direct safetensors save. Verified cosine 1.000000. |
| 6q | gte CUDA index oob на encode | ✅ fix existed (`_cap_max_seq_length` для `docs-gte-*`) |

## Tuning знахідки
- **CODE_RAG_RERANK_POOL_SIZE** (default 200): для **l12 не впливає** (pool 50/100/200/300 = ідентичні числа на code n=80 + docs n=192). Параметр був задуманий для L6 — там +10pp claim (per code comment, не валідовано сьогодні). Для l12 — мертвий важіль.
- **DOC_PENALTY/TEST_PENALTY/GUIDE_PENALTY** (0.15/0.20/0.25): тюнінг під L6 score scale. Не міряли вплив на l12 — todo Run 4.
- **Stratum-gated rerank-skip (P10 A2)**: ON для частини docs strata (nuvei/aircash/trustly/webhook/refund). Тюнено під L6 weakness pattern. Для нової рerank pipeline (l12 на коді, none на доках) це може бути зайвим — todo переоцінити.

## Cost / Budget burn
- ~$10-12 на RunPod за весь день (Run 1 + Run 2 + Run 3 + кілька retry)
- Banked $0 (на старті ~$15)
- ROI: 1 ship-able win (+3.22pp top-10 через роутинг)

## Що працює, що ні

### ✅ Працює
- **Bootstrap CI tool** (`scripts/bootstrap_eval_ci.py`) — від тепер кожен бенч-висновок проходить через CI перевірку
- **Pod_watcher** (`scripts/runpod/pod_watcher.py`) — proactive monitoring + ALERT
- **Local mini-pipeline smoke** (`scripts/local_smoke_candidates.py`) — ловить Bug 6o NaN за 40s до pod
- **Fast-fail у build_docs_vectors** — abort >50% NaN за 30 сек замість 39 хв
- **Jira eval n=908** — справжній ground truth, не handcrafted

### ❌ Не працює / варто уникати
- **mxbai-large + MNRL FT** — gradient explosion гарантовано (Bug 6o)
- **mxbai-rerank як reranker на коді** — pass-through (видає той самий ranking як raw retrieval)
- **bge-reranker-v2-m3** — дорого + не б'є меншого mxbai
- **Спам OOM на Mac при паралельних бенчах** — 16GB не тримає 2 процеси з coderank index одночасно
- **Mxbai FT на доках "перемогою"** — раніше казав +1pp vs L6, бутстрап показав NOISE (CI перетинає 0)
- **n=80 / n=90 evals для будь-якого висновку** — CI ширші за дельти. Завжди йти на n=200+ або jira_n=908

## Де ми зараз
- Production stack без змін (nomic + L6 + P10 A2 stratum gate)
- 3 нові FT'd моделі на HF Hub (l12, mxbai, bge — всі live)
- l12 — єдиний confirmed POSITIVE proven кандидат (на коді)

## Що далі (наступна сесія)
1. **Впровадити роутинг у `src/search/hybrid.py`** — `if is_doc_intent: skip_rerank() else: use l12 instead of L6`. ~30 LOC. Реальний +3.22pp top-10.
2. **Тюнінг DOC_PENALTY/TEST_PENALTY під l12** — sweep на n=908.
3. **Bug 6p docs-nomic pod retry з більшим time_limit** — щоб build_docs_vectors встиг завершитись (потрібно ~3-4h на rtx4090; або більше CPU cores на h100).
4. **Bug 6o (mxbai-large NaN)** — defer глибший ST debug, або просто dropping.
5. **Docs n=200+ eval вже є** (`doc_intent_eval_v3_n200.jsonl`), але **code n=908 jira-ground** — використовувати для всіх code-related decisions.

## Грабли (щоб не наступити знов)
1. **n=80 не дискримінує moderate FT changes** — будь-які "+5pp" на n=80 ймовірно noise. Завжди bootstrap CI.
2. **Pre-flight low_memory check у benchmark_doc_intent.py** має фолс-позитіви — використовуй `--no-pre-flight` коли мало RAM але є disk.
3. **Background bash з 3 послідовними командами** — output buffer глюкує, тільки останнє bench виводиться. Файли пишуться нормально, перевіряй ls -la не tail логу.
4. **SSH command timeout = 7200s default у oneshot scripts** — не вистачає для LanceDB optimize >2h. Раз провалить — pod auto-stops по time_limit, model на HF Hub лишається, вектори губляться.
5. **mxbai-rerank як reranker = pass-through на коді** — не зберігає ranking. Не плутати з mxbai-embed-large (інша модель, інший Bug 6o).
6. **Memory baseline числа застарівають** — завжди свіжо бенч прод перед comparison ("0.25 docs" виявилось 0.2365 на свіжому evali).
