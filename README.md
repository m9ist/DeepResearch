# Deep Research

Локальный движок «глубокого исследования» в духе Deep Research из Odysseus
(PewDiePie), на своём стеке: **внешний оркестратор на Python владеет state-машиной,
истина — файлы на диске, а воркеры — одноразовые вызовы локального CLI-агента
[pi](https://pi.dev)**. Систему можно остановить в любой момент и продолжить —
всё состояние лежит в папке Run'а, а не в живых процессах.

Веб-морда (FastAPI) даёт «пульт»: запуск/пауза/«+N раундов»/«отчёт сейчас»,
живой дашборд (загрузка LLM, фазы воркеров, консоли с пост-мортемом), рендер
находок и сводок.

## Как это работает (one-pager)

```
            Intake (pi/claude в терминале, по prompt-шаблону)
            интервью с человеком ──> spec.md
                                     │
   ┌─────────────────────────────────▼───────────────────────────────┐
   │  Orchestrator (Python, фоновой поток в FastAPI)                   │
   │  spec.md ──нарезка──> backlog.jsonl                               │
   │   цикл Round'ов (тайм-боксированная волна):                       │
   │     ready-задания ──spawn──> pi -p (Worker) × concurrency         │
   │       Worker: web_search/web_fetch (+YouTube-транскрипт,          │
   │               JS-рендер через headless) → пишет Finding           │
   │     бюджет/пусто ──> Checkpoint: харвест ссылок + LLM-сводка       │
   │   автопродолжение до бюджета раундов                              │
   └───────┬─────────────────────────────────────┬────────────────────┘
           ▼                                      ▼
   SearXNG (docker) · llama.cpp/ollama       runs/<id>/ (истина на ФС)
```

Термины и детали — в [CONTEXT.md](CONTEXT.md) (глоссарий),
[docs/SPEC.md](docs/SPEC.md), [docs/BACKLOG.md](docs/BACKLOG.md),
ADR — в [docs/adr/](docs/adr/).
Пайплайн и операции для агента — в [AGENTS.md](AGENTS.md).

## Раскладка репозитория

```
deep_research/                 <- репозиторий (этот корень = python-пакеты в корне)
├── README.md  AGENTS.md  CONTEXT.md  requirements.txt  serve.cmd  .gitignore
├── .venv/                     <- python-окружение (gitignore)
├── orchestrator/              <- state-машина: planner, loop, worker, checkpoint, store, progress…
├── web/                       <- FastAPI + Jinja (дашборд, SSE, контролы) — запускается как `-m web`
├── extension/index.ts         <- pi-extension: web_search / web_fetch (+YouTube, +JS-рендер)
├── tools/                     <- youtube_one.py, fetch_page.py, rescan_failed.py, compile_rounds.py
├── prompts/                   <- intake_instructions.md (интервью)
├── searxng/                   <- docker-compose + settings.yml
├── docs/                      <- SPEC, BACKLOG, ADR
└── runs/                      <- по папке на исследование (gitignore — данные)
```

## Требования

- **Windows**, **Python 3.10+**, **Node 18+**.
- **[pi](https://pi.dev)** (earendil-works) с настроенным провайдером/моделью в
  `~/.pi/agent/models.json` (воркеры зовут pi). По умолчанию ждём локальный
  OpenAI-совместимый сервер (**llama.cpp `llama-server`** или **Ollama**).
- **Docker** — для SearXNG (поисковый бэкенд).
- LLM-сервер на `http://127.0.0.1:8080/v1` (llama.cpp). Метр загрузки читает
  `http://127.0.0.1:8080/slots`.

## Установка и настройка

```powershell
cd K:\repos\deep_research

# 1. Python-окружение
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium      # headless для JS-сайтов

# 2. SearXNG (поиск) — docker
docker compose -f searxng\docker-compose.yml up -d
#   проверка: curl "http://localhost:8888/search?q=test&format=json"

# 3. pi — модель воркеров
#   в ~/.pi/agent/models.json должен быть провайдер с локальной моделью,
#   напр. провайдер "ollama" → http://127.0.0.1:8080/v1 → qwen/qwen3.6-35b-a3b.

# 4. LLM-сервер (llama.cpp или ollama) на :8080 — поднять отдельно.
```

Тонкие настройки — env воркеров (в `orchestrator/worker.py`) и `config.json` Run'а:
`DR_SEARXNG_URL`, `DR_FETCH_MAX` (15000), `DR_YT_MAX` (80000), `DR_JS_MIN` (400),
`DR_NET_TIMEOUT_MS` (20000), `DR_LLM_SLOTS_URL`.

## Запуск

```powershell
K:\repos\deep_research\serve.cmd          # или: .venv\Scripts\python -m web
```
→ открой **http://127.0.0.1:8765**.

## Поток работы

1. **+ Новое исследование** → выбери агента (pi/claude), впиши идею, «Проверить
   агента», «Предложить имя», «Создать и запустить интервью» → терминал с
   pi-интервью, по итогу рождается `spec.md`. Кнопка **🔄** подхватывает готовый
   `spec.md` без перезагрузки страницы.
2. На странице Run'а — **▶ Запустить раунды**. Оркестратор планирует, гоняет
   раунды с concurrency (слайдер), тащит ссылки «ещё почитать», пишет сводки.
   Модель воркера можно сменить на лету (выпадающий «модель» из `models.json`).
3. **Дашборд «Движуха»**: загрузка LLM (подбор числа воркеров), фазы воркеров,
   консоли (превью+разворот, фуллскрин, пост-мортем по `worker_end`).
4. **Пауза** (hard-kill), **«+1 раунд»/«−1»**, **«📄 Отчёт сейчас»** — в любой момент.
5. Находки и сводки — кликабельны, открываются во встроенном просмотрщике; в
   бэклоге у отработавших заданий — ссылка на сохранённую консоль.
6. **Заглавная**: баннер «идёт исследование» + график загрузки LLM за 12ч (окна
   12ч/3ч/1ч).

## CLI (без веб-морды)

```powershell
$py = ".venv\Scripts\python"
& $py -m orchestrator.cli init  runs\<id> --rounds 3 --concurrency 3 --spec "..."
& $py -m orchestrator.cli plan      runs\<id>
& $py -m orchestrator.cli run       runs\<id>     # автономно: plan→раунды→done
& $py -m orchestrator.cli run-round runs\<id>     # одна волна
```

## Происхождение

Личный проект. `tools/youtube_one.py` адаптирован из личных скриптов; Deep
Research как идея — по мотивам Odysseus (Alibaba Tongyi DeepResearch).
