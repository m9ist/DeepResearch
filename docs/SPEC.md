# ТЗ — Deep Research v1 (тонкий вертикальный срез)

Термины — заглавными с большой буквы — определены в [../CONTEXT.md](../CONTEXT.md).
Архитектура — в [adr/0001](./adr/0001-external-orchestrator-fs-truth.md).

## 1. Цель v1

Доказать сквозной цикл **plan → search → findings → checkpoint → синтез** на
одном Run'е, под управлением минимальной веб-морды. Не полировать — получить
работающую петлю, на которой отлаживается схема.

## 2. В scope v1 / вне scope

**IN:**
- SearXNG (docker) + pi-extension с инструментами `web_search` / `web_fetch`.
- Orchestrator: нарезка Spec → Backlog, цикл Round'ов с Checkpoint'ами,
  спавн Worker'ов (`pi -p`), сбор Finding'ов, LLM-синтез Report на Checkpoint'е,
  автопродолжение до бюджета раундов.
- Worker: один `pi -p` на задание, пишет один Finding атомарно.
- Web: список Run'ов; рендер `.md`; кнопка «открыть во внешнем просмотрщике»;
  Pause (hard); «сформируй промежуточный отчёт»; «+N раундов»; регулятор
  concurrency.
- Intake: через интерактивный pi в терминале (prompt-template), пишет `spec.md`.
- Лог запусков: `events.jsonl` (каждый запуск агента — с промптом и временем).

**OUT (→ BACKLOG):** периодические таймер-Report'ы; live-смена модели Worker'а
из UI; веб-чат Intake; несколько Run'ов одновременно; category-режимы
(product/compare/howto/factcheck); векторная память/RAG между Run'ами.

## 3. Топология процессов

Один процесс FastAPI. Orchestrator крутится как `asyncio`-задача внутри него.
Один активный Run за раз. Worker'ы — дочерние процессы `pi -p` (subprocess),
параллелизм ограничен `concurrency`. Краш веб-аппа допустим: состояние
восстанавливается из папки Run'а при рестарте (см. §6).

## 4. Раскладка папки Run'а

```
runs/<YYYY-MM-DD>-<slug>/
├── spec.md          # Spec: задача исследования (выход Intake)
├── config.json      # параметры Run'а (см. §5)
├── state.json       # рантайм-состояние машины (см. §6)
├── backlog.jsonl    # очередь заданий, по одному JSON на строку (см. §7)
├── events.jsonl     # лог всех запусков/событий (см. §8)
├── prompts/
│   └── <task_id>.txt   # точный промпт, с которым стартовал Worker
├── findings/
│   └── NNNN.md         # Finding (см. §9)
└── reports/
    └── round-NN.md     # Report по Checkpoint'у Round'а NN (см. §10)
```

Слаг Run'а — из темы Intake (kebab-case, транслит). Нумерация findings —
сквозная zero-padded (`0001`).

## 5. `config.json` — параметры Run'а

```json
{
  "concurrency": 3,
  "worker_provider": "ollama",
  "worker_model": "qwen/qwen3.6-35b-a3b",
  "rounds_budget": 3,
  "round_timeout_min": 10,
  "max_urls_per_task": 3,
  "search_provider": "searxng",
  "searxng_url": "http://localhost:8888"
}
```

Live-редактируемое из веб-морды (mid-Run): `concurrency`, `rounds_budget`
(кнопки «+1/−1»), `worker_model` и `round_timeout_min` (5/10/30/0=без таймаута,
применяются со следующего раунда). `round_timeout_min` — тайм-бокс раунда в минутах
(было `round_time_budget_sec` с first/rest — заменено единым настраиваемым значением).

## 6. `state.json` — машина состояний

```json
{
  "status": "running",        // idle | running | paused | done
  "round": 2,                 // текущий/последний Round
  "round_started_at": "2026-06-14T08:31:05Z",
  "rounds_budget": 3,
  "started_at": "2026-06-14T08:20:00Z",
  "updated_at": "2026-06-14T08:33:40Z"
}
```

Состояния и переходы:

```
idle ──start──> running ──(бюджет времени Round'а истёк | backlog пуст)──> [Checkpoint]
  Checkpoint: написать reports/round-NN.md, re-plan (втянуть «ещё почитать"
  из Finding'ов в backlog), decide:
    round < rounds_budget И есть ready-задания ──> running (Round+1)
    иначе                                       ──> done
running ──Pause──> paused      (hard-kill in-flight Worker'ов; их задания → ready)
paused  ──Resume──> running    (продолжает текущий незакрытый Round)
любое   ──«+N»──> rounds_budget += N   (статус не меняет)
любое   ──«отчёт сейчас»──> синтез внепланового Report (Run не останавливает)
```

**Восстановление после рестарта:** при старте веб-аппа прочитать `state.json`
активного Run'а; `running` трактовать как «прервано» → перевести в `paused`,
in_progress-задания в `backlog.jsonl` вернуть в `ready`. Дальше — по Resume.

## 7. `backlog.jsonl` — задания

Одно задание = одна строка JSON:

```json
{"id":"t0007","kind":"search","title":"pi extension API surface","query":"pi.dev registerTool extension","status":"ready","round_added":1,"source":"spec"}
{"id":"t0012","kind":"fetch","title":"pi-subagents README","url":"https://github.com/tintinweb/pi-subagents","status":"done","round_added":2,"source":"finding:0005"}
```

- `kind`: `search` (запрос → результаты) | `fetch` (конкретный URL → разбор) |
  `topic` (подтема, Worker сам ищет).
- `status`: `ready` | `in_progress` | `done` | `dropped`.
- `source`: `spec` | `finding:NNNN` (откуда задание родилось — для трассировки).
- Дедуп: не добавлять задание с уже виденным нормализованным URL/запросом.
- Запись атомарна (перезапись через `.tmp` + rename) либо append-only с
  компакцией на Checkpoint'е.

## 8. `events.jsonl` — лог запусков

Требование «любой запуск агента логируется (промпт такой-то запущен тогда-то)».
Одна строка на событие:

```json
{"ts":"2026-06-14T08:31:06Z","event":"worker_start","task_id":"t0007","pid":21344,"model":"lmstudio/...","prompt":"prompts/t0007.txt"}
{"ts":"2026-06-14T08:31:58Z","event":"worker_end","task_id":"t0007","pid":21344,"status":"ok","finding":"findings/0007.md","duration_sec":52}
{"ts":"2026-06-14T08:36:10Z","event":"checkpoint","round":1,"report":"reports/round-01.md","findings_total":6,"backlog_ready":9}
{"ts":"...","event":"pause"} {"ts":"...","event":"resume"} {"ts":"...","event":"rounds_budget_changed","to":5}
```

Полный текст промпта Worker'а кладётся в `prompts/<task_id>.txt`, в событии —
ссылка на него.

## 9. Finding — `findings/NNNN.md`

Атомарная запись (`.tmp` → rename): hard-kill не должен оставлять рваный `.md`.
Шаблон:

```markdown
---
id: "0007"
task_id: "t0007"
round: 1
kind: search
source_url: "https://..."        # для fetch/перехода
model: "lmstudio/unsloth/gpt-oss-20b"
started: "2026-06-14T08:31:06Z"
finished: "2026-06-14T08:31:58Z"
---

## Куда сходил
<запрос/URL и что это за источник>

## Что вытащил (саммари)
<сжатое содержание, с inline-цитатами [title](url)>

## Ещё почитать
- [title](url) — почему стоит  → станет заданием в Backlog
- ...

## Смежные темы
- <тема> — чем релевантна

## Рекомендация
<стоит ли копать глубже; на что обратить внимание оркестратору>
```

Секция «Ещё почитать» — машинно-парсимая (список ссылок), оркестратор втягивает
её в Backlog на Checkpoint'е.

## 10. Report — `reports/round-NN.md`

LLM-синтез на Checkpoint'е из Finding'ов этого Run'а (минимум — последних / всех):

```markdown
# Сводка — Round NN (<timestamp>)

## Что нашли за раунд
<ключевые находки со ссылками на findings/NNNN.md и внешние [url]>

## Перспективные идеи / зацепки
<что выглядит ценным, куда копать>

## Состояние
- Finding'ов всего: M
- В бэклоге ready: K  (ещё столько ссылок «на почитать»)
- Раунд NN из <rounds_budget>

## План на следующий раунд
<какие задания приоритетны; коррекция направления, если нужна>
```

## 11. Компоненты и их границы

| Компонент | Где | Ответственность |
|---|---|---|
| **pi-extension** | `extension/` (TS) | `web_search` → SearXNG json; `web_fetch` → readability→markdown, обрезка до лимита. |
| **prompt-templates** | `prompts/` (md) | `intake` (интервью→spec.md); `worker` (как делать одно задание и формат Finding'а). |
| **Orchestrator** | `orchestrator/` (Py) | нарезка Spec→Backlog; цикл Round'ов; спавн/убийство Worker'ов; парсинг Finding'ов; синтез Report; state.json/events.jsonl. |
| **Web** | `web/` (FastAPI+Jinja+htmx) | список Run'ов; рендер md; «открыть внешне»; Pause/Resume; «отчёт сейчас»; «+N»; concurrency. SSE для live-обновления. |
| **SearXNG** | docker | поисковый бэкенд (без ключей). |

## 12. Критерии приёмки v1

1. `pi` + prompt-template intake создаёт `runs/<id>/spec.md` по интервью.
2. Запуск Run'а из веб-морды нарезает Backlog и гонит Round 1 в пределах
   `round_timeout_min`, спавня ≤ `concurrency` Worker'ов.
3. Каждый Worker оставляет валидный `findings/NNNN.md` по шаблону §9;
   hard-kill в середине не оставляет рваных файлов.
4. На Checkpoint'е появляется `reports/round-01.md`, ссылки «ещё почитать»
   втянуты в Backlog, при `round < rounds_budget` стартует Round 2 сам.
5. Pause убивает in-flight Worker'ов и возвращает их задания в `ready`;
   рестарт веб-аппа восстанавливает Run в `paused` без потери Finding'ов.
6. «+N раундов» увеличивает бюджет; «отчёт сейчас» пишет внеплановый Report.
7. `events.jsonl` содержит запуск каждого Worker'а с ссылкой на его промпт.
