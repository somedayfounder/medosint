# Someday Founder Pipeline

Сбор бизнес-принципов + STAR-историй из знаний Claude по компаниям.

## Запуск

ВАЖНО: всегда использовать scriptPath, не name. args обязателен.

```
Workflow({ scriptPath: "sf_pipeline/sf_workflow.js", args: { company: "IKEA" } })
```

Никогда не хардкодить компанию в скрипт. Никогда не использовать Workflow({ name: "sf-pipeline" }) — args не передаются.

Или список:
```
Workflow({ scriptPath: "sf_pipeline/sf_workflow.js", args: { companies: ["IKEA", "FedEx"] } })
```

## ⚠️ ОБЯЗАТЕЛЬНО после завершения Workflow — записать файлы И ВЫВЕСТИ ТОП В ТЕКСТЕ ОТВЕТА

КРИТИЧНО: топ-30 ВСЕГДА выводить в тексте ответа пользователю, не через bash stdout. Без этого шага задача не считается выполненной.

Когда Workflow завершится (придёт task-notification), сразу же без вопросов:

**Шаг 1** — записать файл через Bash/Python:

```python
import json, re, datetime
from pathlib import Path

output_dir = Path("sf_pipeline/output")
output_dir.mkdir(exist_ok=True)

with open(TASK_OUTPUT_FILE) as f:
    data = json.load(f)

results = data["result"]
if not isinstance(results, list):
    results = [results]

for company_result in results:
    company = company_result["company"]
    areas = company_result["areas"]
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    fname = output_dir / f"{company.replace(' ', '_')}_{ts}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"# {company}\n\n")
        for a in areas:
            count = len(re.findall(r'\nID:', a["text"]))
            f.write(f"## {a['area']} ({count} entries)\n\n")
            f.write(a["text"])
            f.write("\n\n---\n\n")
    total = company_result["count"]
    print(f"{fname} ({total} записей)")
```

**Шаг 2** — прочитать топ из JSON и вывести В ТЕКСТЕ ОТВЕТА (не через print/bash), в виде нумерованного списка:

```python
import json
with open(TASK_OUTPUT_FILE) as f:
    data = json.load(f)
results = data["result"]
if not isinstance(results, list):
    results = [results]
for r in results:
    print(r["company"], len(r.get("summary", [])))
    for e in r.get("summary", []):
        print(e["story_ru"], e["principle_ru"])
```

Затем сформировать текстовый ответ пользователю в формате:

**Файл записан:** `sf_pipeline/output/Company_timestamp.md` (N записей)

**ТОП-30: Company**

1. story_ru
   → principle_ru

2. ...

## Правила

- При суммировании результатов — только из файлов (grep), никогда из памяти
- Не добавлять примеры из других компаний
- Запись файлов — через Bash/Python после Workflow, не через агентов
- Один файл на компанию: `output/{Company_Name}.md`
- Топ выводить В ТЕКСТЕ ОТВЕТА, не через bash stdout — иначе обрезается
