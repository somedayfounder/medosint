# sf_pipeline — Полный контекст для нового чата

## Что это

Пайплайн для сбора бизнес-историй о компаниях. Для заданной компании (напр., "Southwest Airlines") собирает конкретные именованные истории из знаний LLM, парсит их в структурированный формат (CARQ), отображает в веб-мониторе и позволяет вручную обогатить каждую историю веб-поиском.

**Стек:** Python 3, FastAPI, Uvicorn, SSE (Server-Sent Events), SQLite, Gemini API, Serper API, Tavily API.

---

## Архитектура

```
Пользователь → POST /api/run (company, llm, active_groups, active_models)
                     ↓
           [Python thread: run_pipeline()]
                     ↓
       gather_llm_knowledge()   ← Gemini / GPT / Claude параллельно
           ↓ per-area, per-topic passes
       parse_carq_entries()     ← парсит текст в список историй
                     ↓
       emit() → Queue → SSE → monitor.html
                                    ↓
                          Пользователь нажимает [Enrich]
                                    ↓
                          POST /api/enrich_web (одна история)
                                    ↓
                     enrich_story_web():
                       1. Serper (Google) → URL-ы
                       2. Tavily Extract → HTML-контент
                       3. Flash Lite: факт-карточка по каждому источнику
                       4. Flash: итоговый нарратив A_enriched
```

---

## Ключевые файлы

| Файл | Роль |
|------|------|
| `sf_web_pipeline.py` | Вся логика пайплайна: LLM-вызовы, парсинг, обогащение |
| `server.py` | FastAPI сервер: SSE, API эндпоинты, запуск пайплайна в треде |
| `static/monitor.html` | Веб-монитор (единый HTML без зависимостей) |
| `static/browser.html` | Браузер историй из БД |
| `data.db` | SQLite: истории (stories), использование API (api_usage) |

---

## Запуск

```bash
cd sf_pipeline
pip install fastapi uvicorn requests google-generativeai openai anthropic
python server.py
# http://localhost:8000/monitor
```

`.env` нужны: `GEMINI_API_KEY`, `OPENAI_API_KEY` (опц.), `SERPER_API_KEY`, `TAVILY_API_KEY`.

---

## Формат данных CARQ

Каждая история — блок текста:

```
ID: customer_success-7
C: 2018, San Francisco — Airbnb, CEO Brian Chesky
A: [Полная история на русском, без ограничения длины, конкретные факты, числа, имена]
I: [Два предложения на русском: что нестандартного + универсальный принцип]
Q: airbnb customer success story, chesky host crisis 2018, airbnb community support program, ...
```

- `C` — контекст (год, место, люди)
- `A` — полная история на русском (основное поле)
- `I` — инсайт на русском (нестандартность + принцип)
- `Q` — 5 поисковых запросов на английском, через запятую

---

## Стратегия промптинга: per-topic passes

**Проблема:** Gemini при широком запросе "знаешь ли ты истории о X?" пишет 1-2 истории и останавливается.

**Решение:** Один широкий первый проход + N тематических проходов — по одному на каждый топик из `AREA_TOPICS`.

### AREA_TOPICS

Словарь `{area_name: [список топиков]}`. Пример для "Customer Success":
```python
"Customer Success": [
    "Customer Journey, Customer Experience, CX",
    "Onboarding & Adoption, Customer Onboarding",
    "Retention & Churn, Churn Prevention",
    "NPS, Customer Feedback, Satisfaction",
    "Customer Support & Service Recovery, Escalations",
    ...
]
```

~10-18 топиков на область, всего 5 областей: Personal Growth, Work & Career, Office Survival, Marketing, Leadership / Customer Success / IT / ...

### Структура промпта

```
[Широкий первый проход — все истории что знаешь]

When your list is complete, write exactly:
=== ВТОРОЙ ПРОХОД ===

Now focus exclusively on: Customer Journey, Customer Experience, CX
List every specific story ... that you have NOT yet written.
After each entry, ask yourself: «Знаю ли я ещё одну историю по этой теме?» — if yes, write it.
Keep going until the answer is no. Same format.

When your list is complete, write exactly:
=== ТРЕТИЙ ПРОХОД ===
...
```

**Почему ординальные слова (ВТОРОЙ, ТРЕТИЙ...) а не === ПРОХОД 2 ===?**  
Нумерованные маркеры заставляли модель останавливаться после 2-го прохода. Ординальные слова модель генерирует надёжно до конца.

### Self-prompting

`«Знаю ли я ещё одну историю по этой теме?»` в каждом тематическом проходе — заставляет модель не ограничиваться одной историей на топик.

---

## id_topic_map

После получения ответа, текст разбивается по маркерам проходов. Для каждого нового ID в каждой секции определяется топик:

```python
# Python:
sections = re.split("|".join(re.escape(m) for m in _pass_markers), text)
for i, sec in enumerate(sections):
    topic = _focus_topics[i - 1] if i > 0 else None  # None для первого (широкого) прохода
    ids = set(re.findall(r'ID:\s*(\S+)', sec))
    new_ids = ids - seen_ids
    if topic:
        for id_val in new_ids:
            id_topic_map[id_val] = topic

# Передаётся в:
emit("knowledge_model", {..., "id_topics": id_topic_map})
knowledge_items.append({"area": area_name, "text": text, "id_topics": id_topic_map})
```

`gather_llm_knowledge()` возвращает `list[{"area": str, "text": str, "id_topics": dict}]`.

---

## knowledge_items и parse_carq_entries

`gather_llm_knowledge()` → `list[dict]` где каждый dict:
```python
{"area": "Customer Success", "text": "ID: cs-1\nC: ...\n...", "id_topics": {"cs-7": "Onboarding", ...}}
```

`parse_carq_entries(raw_knowledge, company)` итерирует список, разбивает каждый `text` на блоки по `ID:`, парсит поля C/A/I/Q, добавляет `topic` из `id_topics`.

---

## SSE события (monitor.html слушает)

| Тип | Данные | Смысл |
|-----|--------|-------|
| `pipeline_start` | company, llm, monthly | Пайплайн запущен |
| `knowledge_model` | model, area, text, passes, pass_counts, id_topics, unknown | Полный сырой текст от модели для одной области |
| `story_found` | id, company, area, topic, C, A, I, Q | Одна распарсенная история |
| `log` | msg | Лог-сообщение |
| `pipeline_done` | company, total_stories, llm_cost | Пайплайн завершён |
| `enrich_story` | id, A_enriched, sources | Результат обогащения |

---

## Монитор: две вкладки

**Raw (по умолчанию)** — обрабатывает `knowledge_model` события:
- Организация по областям (area)
- Показывает заголовок с числом историй и статистикой проходов (`p1:6 +2 +1 +2...`)
- Каждый блок: A (история) + I (инсайт) + C (контекст) + Q (ссылки Google) + ID + topic chip
- Кнопка Enrich на каждой истории

**Stories (слева)** — обрабатывает `story_found`:
- Карточки историй
- Фильтры по области

---

## Topic chip в мониторе

Для историй из тематических проходов (не из широкого первого прохода) показывается чип с названием топика.

```js
// В knowledge_model handler:
const topicVal = ev.id_topics && ev.id_topics[idVal];
if (topicVal) {
    const tc = document.createElement('span');
    tc.className = 'chip chip-topic';
    tc.textContent = topicVal.split(',')[0].trim();
    idRow.appendChild(tc);
}
```

CSS: `.chip-topic { background:#f0f4ff; color:#4466cc; ... }`

---

## Обогащение (ручное через Enrich кнопку)

`enrich_story_web(story, serper_key, tavily_key, cheap_llm, expensive_llm)`:

1. Берёт `Q`-запросы истории (до 5 штук)
2. Serper: параллельный поиск → список URL
3. Tavily Extract: скрейпинг всех URL за раз
4. Flash Lite (cheap_llm, max_tokens=1200): для каждого URL — факт-карточка:
   `ПОДТВЕРЖДАЕТ: ... / ДОБАВЛЯЕТ: ... / ПРОТИВОРЕЧИТ: ... / КОНЦЕПТЫ: ...`
5. Flash (expensive_llm, max_tokens=4000): итоговый обогащённый текст `A_enriched`

---

## Текущее состояние (на момент архивации)

### Работает:
- Per-topic passes (9 проходов для Customer Success)
- Self-prompting ("Знаю ли я ещё одну историю?")
- id_topic_map: маппинг историй → топики
- Пайплайн останавливается после парсинга (обогащение — только ручное)
- Факт-карточки при обогащении (исправлен max_tokens=1200, убрана преамбула)

### Ещё не решено:
- **Topic chips в Raw-вкладке** — код добавлен (monitor.html строки ~1166-1173), но нужна проверка через DevTools Console:
  ```
  [topics debug] area: Customer Success  id_topics: {...}  keys: 15
  ```
  Если keys: 0 — Python не передаёт. Если keys: 15 — надо смотреть совпадение idVal.
- **Grounding не логируется** — "на десятке последних запусков либо нет граундинга, либо он не логируется"
- **SCORE поле** — было в старом шаге REFINE, который убран. Больше не генерируется.
- **DB-сохранение** — схема БД ещё со старыми полями (principle, situation), CARQ-истории не сохраняются

---

## API эндпоинты server.py

| Метод | Путь | Параметры |
|-------|------|-----------|
| POST | /api/run | {company, llm, n_queries, active_groups, active_models} |
| GET | /api/stream | SSE поток |
| POST | /api/stop | Остановить пайплайн |
| POST | /api/enrich_web | {story: {id,A,C,I,Q,...}, enrich_model} |
| GET | /api/stories | ?company=&area=&q=&sort=&page= |
| GET | /api/usage_stats | Статистика использования API за месяц |
| POST | /api/clear_stories | Очистить БД |

---

## Конфигурация групп (active_groups)

Пайплайн можно запустить только по определённым группам (областям):
- Из монитора выбираются checkbox-ы
- Передаются как `active_groups: ["customer_success", "marketing"]`
- `active_models: ["gemini"]` — только нужные модели

---

## Файловая структура sf_pipeline/

```
sf_pipeline/
├── sf_web_pipeline.py  # Весь пайплайн (~2000 строк)
├── server.py           # FastAPI (~330 строк)
├── static/
│   ├── monitor.html    # Монитор (~1250 строк)
│   └── browser.html    # Браузер историй
├── data.db             # SQLite (gitignore)
├── output/             # Markdown файлы с историями
├── .env                # API ключи (gitignore)
└── CONTEXT.md          # Этот файл
```
