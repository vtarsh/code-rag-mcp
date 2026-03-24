# Overnight Deep Analysis Prompt

Прочитай цей файл і виконуй інструкції автономно до ранку.

## Контекст
- Прочитай `~/.pay-knowledge/profiles/pay-com/NEXT-SESSION-PROMPT.md` для повного контексту
- Прочитай `~/.pay-knowledge/.claude/rules/workflow.md` для правил роботи
- Прочитай `~/.pay-knowledge/.claude/rules/deep-analysis-tiers.md` для класифікації задач
- Прочитай `~/.pay-knowledge/.claude/rules/deep-analysis-agent.md` для інструкцій агентам

## Стан
- 96.5% recall, 20 mechanisms, 974 tasks, 133 tests
- Tier 1 done: PI 17/17, CORE 12/23, BO 1/~20
- Tier 3-4 done: all 361 tasks з repos_changed

## Що робити

### Крок 1: Знайти невирішені Tier 1 задачі

```bash
cd ~/.pay-knowledge && python3 -c "
import sqlite3, json
db = sqlite3.connect('db/knowledge.db')
# CORE Tier 1 already analyzed:
analyzed_core = {'CORE-2595','CORE-2586','CORE-2581','CORE-2610','CORE-2329','CORE-2451','CORE-2558','CORE-1615','CORE-2351','CORE-2607','CORE-2563','CORE-2564'}
rows = db.execute('SELECT ticket_id, repos_changed, summary FROM task_history WHERE ticket_id LIKE \"CORE-%\" AND repos_changed IS NOT NULL AND repos_changed != \"[]\"').fetchall()
remaining = [(r[0], len(json.loads(r[1])), r[2][:60]) for r in rows if r[0] not in analyzed_core and len(json.loads(r[1])) >= 7]
print(f'{len(remaining)} CORE Tier 1 remaining:')
for tid, cnt, summ in sorted(remaining, key=lambda x: -x[1])[:15]:
    print(f'  {tid:12} {cnt:2} repos | {summ}')
"
```

### Крок 2: Запустити Tier 1 батч (3 агенти, по 1-2 задачі)

Для кожного агента використовуй цей шаблон:
```
TIER 1 deep analysis. Read ~/.pay-knowledge/.claude/rules/deep-analysis-tiers.md.
Task: {TASK_ID} ({N} repos, "{summary}")
Trace the FULL flow through actual code. Read methods/, libs/, consts.
Run benchmark_recall.py --task={TASK_ID} --filter-phantoms.
Classify each missed repo by root cause.
NO mcp__pay-knowledge__* tools. DB: ~/.pay-knowledge/db/knowledge.db, Raw: ~/.pay-knowledge/raw/
```

### Крок 3: Після кожних 5-10 Tier 1 аналізів

1. **Зберегти findings** в lessons.md
2. **Запустити pattern mining агента**:
   - Які нові root causes знайдені?
   - Які repos найчастіше missed?
   - Чи є нові co-change pairs?
3. **Якщо знайдено actionable pattern** → імплементувати → benchmark → commit
4. **Оновити baselines** якщо recall змінився

### Крок 4: Також BO Tier 1

Після CORE — перейти на BO. Знайти BO задачі з 3+ repos і misses:
```bash
cd ~/.pay-knowledge && CODE_RAG_HOME=~/.pay-knowledge ACTIVE_PROFILE=pay-com python3 scripts/benchmark_recall.py --group=BO --filter-phantoms 2>&1 | grep "missed=" | head -15
```

### Крок 5: Recurring cron (кожні 30 хв)

Постав cron який:
1. Перевіряє чи агенти закінчили
2. Якщо так — збирає результати, комітить
3. Запускає наступний батч
4. Кожні 10 задач — pattern mining
5. **Якщо помилка** — логує і продовжує з наступною задачею

```
Autonomous overnight cycle:
1. Check if agents completed → collect results → commit
2. Launch next batch (3 agents, CORE then BO Tier 1)
3. Every 10 tasks: pattern mine → implement if actionable → benchmark
4. Update lessons.md after each finding
5. If error: log to lessons.md and continue
6. Max 3 agents at a time
7. Commit after each completed batch
```

## Правила
- Parallel agents by default (max 3)
- Commit після кожного батчу
- Не зупинятись на помилках — логувати і продовжувати
- Кожні 10 задач — шукати паттерни
- benchmark_recall.py --filter-phantoms для перевірки
- НЕ модифікувати profile scripts (git-ignored)
- Оновлювати lessons.md після кожного відкриття
