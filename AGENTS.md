# AGENTS.md — пайплайн и операции

Памятка для AI-агента (Claude Code / pi), работающего в этом репозитории.
Описывает, как устроен движок и как выполнять типовые операции по фразам Олега.

`PY = .venv\Scripts\python` (запускать из корня репо). Веб-морда: `serve.cmd` → http://127.0.0.1:8765.

## 1. Что это

Локальный «глубокий ресёрч»: внешний **оркестратор** (Python) владеет state-машиной,
истина — файлы в `runs/<id>/`. **Воркеры** — одноразовые `pi -p`, каждый делает
одно задание и пишет один **Finding**. Подробности: [README.md](README.md),
[CONTEXT.md](CONTEXT.md) (глоссарий), [docs/SPEC.md](docs/SPEC.md).

## 2. Папка Run'а (`runs/<id>/`)

| Файл/папка | Что |
|---|---|
| `spec.md` | ТЗ — ТЕКУЩАЯ версия (его читают все); при изменении старая копируется в историю |
| `spec_versions/spec.vN.md` | неизменяемая история ТЗ (`store.snapshot_spec`, идемпотентно); видны во вьюере |
| `config.json` | `concurrency`, `worker_provider/worker_model`, `rounds_budget`, `round_timeout_min` (0=без таймаута), лимиты |
| `state.json` | `status` (intake/running/paused/done), `round` (=число сводок), `rounds_budget`, `round_budget_sec`, `phase` (`synth` пока идёт сводка) |
| `backlog.jsonl` | задания: `{id, kind(search/fetch/topic), title, query?/url?, status(ready/in_progress/done/dropped), round_added, source}` |
| `events.jsonl` | лог: `worker_start/end`, `round_start/end`, `harvest`, `checkpoint`, `synth_retry`, `checkpoint_failed`, `task_dropped/kept`, `refine_*`, … |
| `findings/NNNN.md` | находки (summary одного задания) |
| `reports/round-NN.md`, `interim-NN.md`, `compiled-*.md` | сводки |
| `sources/` | сырьё: транскрипты YT, отрендеренные страницы |
| `console/<task_id>.log` | пост-мортем консоли воркера (пишется и на провале — с причиной) |
| `*.cmd` (intake/continue/refine) | лаунчеры терминальных диалогов pi/claude |
| `refine_brief.md`, `refine_decisions.json` | доуточнение ТЗ: бриф диалога + решения агента по заданиям (`{drop,keep}`) |
| `prompts/`, `sessions/`, `sessions-refine/` | промпты воркеров; сессия pi-интервью; сессия диалога уточнения |

## 3. State-машина (кратко)

`intake` → (есть `spec.md`) → **Round**: волна воркеров с `concurrency`, тайм-бокс →
**Checkpoint**: харвест ссылок «ещё почитать» в backlog + LLM-сводка + решение
продолжать → автопродолжение до `rounds_budget` → `done`. Пауза = hard-kill
in-flight, задания → `ready`. Истина на диске → можно остановить и продолжить.

**Раунд засчитан только когда написан его Report** (`reports/round-NN.md`):
число пройденных раундов = число этих файлов (`loop.completed_rounds`). Раунд,
прерванный паузой/рестартом до сводки, не засчитывается — переигрывается с тем же
номером, бюджет не тратится. Восстановление после рестарта (`web/app._recover_orphans`)
ставит `running→paused` и `state.round` = число написанных сводок.

**Синтез сводки** на Checkpoint'е ретраится 3× (`checkpoint.synth_report`); при
стойком отказе (напр. LLM лёг) фейк-сводка НЕ пишется — `run()` ловит исключение,
логирует `checkpoint_failed` и встаёт на паузу (раунд не засчитан, на resume
синтез повторится). Пока идёт синтез — `state.phase="synth"` (UI: баннер «идёт синтез»).
Задания со статусом `dropped` в раунды не берутся (`run_round` фильтрует только `ready`).

## 4. Каталог операций (фраза → что делать)

### «давай фейл-задания закинем в бэклог» / «перефетчить провалившиеся»
Источники, что не открылись (JS-сайт, 403, заблокированный транскрипт), вернуть в
backlog как свежие `ready` fetch-задания (идемпотентно — повтор не плодит дубли).
```
PY tools\rescan_failed.py runs\<id>
```
После — запустить раунды, чтобы их перебрать (уже с JS-рендером/транскриптом).

### «оформи раунды исследования N-M единым документом» / «сведи раунды N-M»
Склеить сводки раундов в один документ (`reports/compiled-<N>-<M>.md`, виден во вьюере).
```
PY tools\compile_rounds.py runs\<id> N M
PY tools\compile_rounds.py runs\<id> 2 8 -o reports\digest.md
```

### «запусти / продолжи раунды»
Веб: кнопка **▶ Запустить/Продолжить раунды**. CLI (автономно plan→раунды→done):
```
PY -m orchestrator.cli run runs\<id>
```
Если бюджет исчерпан — сначала «+N раундов».

### «поставь паузу»
Веб: **⏸ Пауза** (hard-kill, задания → ready). Программно — `web/runner.py manager.pause`.

### «ещё N раундов» / «отчёт сейчас» / «больше-меньше воркеров» / «смени модель»
Веб-кнопки: «+1 раунд» / «−1» (`bump-rounds?n=±1`, `−1` не ниже пройденных),
«📄 Отчёт сейчас», выпадающий «воркеров» + «Применить», выпадающий «модель» (список
из `~/.pi/agent/models.json`) + «Применить». Эндпоинты `/run/{id}/bump-rounds`,
`/report-now`, `/set-concurrency`, `/set-model`; операции `checkpoint.interim_report`,
`config.concurrency`/`worker_provider`+`worker_model` (применяются со следующего воркера).

### «подхватить spec после интервью» (без F5)
Кнопка 🔄 рядом с «Запустить/Продолжить интервью»: `GET /run/{id}/spec-status` →
`{has_spec, spec_html, mtime}`; фронт вставляет ТЗ, раскрывает контролы, прячет
«Запустить интервью». Нужна, т.к. SSE-снапшот не несёт `has_spec`.

### «новое исследование» / «проведи интервью»
Веб: **+ Новое исследование** → диалог → терминал с pi-интервью → `spec.md` →
«Запустить раунды».

### «доуточнить ТЗ» / «поправить план по ходу»
Группа **«Доуточнить ТЗ»** в панели Spec (видна при готовом `spec.md`): выбрать
агента (pi/claude) → **✎ Доуточнить** (`/refine-launch`) генерит `refine_brief.md`
(исходная идея + текущий ТЗ + незавершённые `ready`-задания) и открывает терминал
в сессии `sessions-refine`. Агент обсуждает уточнения, ПЕРЕЗАПИСЫВАЕТ `spec.md`,
пишет `refine_decisions.json` (`{drop,keep,add}`). Затем **🔄 подхватить решения**
(`/refine-apply`) проставляет `drop`→`dropped`, `keep`→`ready` И добавляет НОВЫЕ
задания из `add` (search/fetch под уточнённый фокус) — иначе уточнение не даёт новых вопросов.
Раунды сам не запускает. Не нравится — повторный «✎ Доуточнить» (свежая сессия).

### «не делать это задание» / «вернуть задание»
В строке бэклога: **✕** (`/task/{id}/drop`, `ready→dropped`) и **↺**
(`/task/{id}/keep`, `dropped→ready`). Трогает только `ready↔dropped` (done/in_progress — 400).
`dropped` исключены из раундов.

### «таймаут раунда» (5/10/30 мин / без)
Селектор **«таймаут»** в контролах → `/set-timeout?m=` пишет `round_timeout_min`
(0=без таймаута). Применяется со следующего раунда; таймер текущего — `⏱` в шапке.

### «не тащит транскрипт YouTube» / «IP-бан»
`youtube_transcript_api` банит IP за частые запросы. Лечение — лестница провайдеров
с fail-over (`tools/yt_fetch.py`, конфиг `tools/yt_providers.json`, ADR 0003): при
бане YT-API (бэкофф ×2, ≤1 запрос/5мин) запрос проваливается на платный apify-фолбэк.
Кэш по `videoId` и учёт квоты/бана — общие (`tools/tmp/`). Если apify не настроен
(пустой токен в `tools/yt_secrets.json` / `DR_APIFY_TOKEN`) — фолбэка нет, и при
исчерпании лестницы `web_fetch` отдаёт воркеру терминальную пометку, НЕ лезет в
headless. Стойкий бан без apify — подождать (временный) или резидентный прокси.

### «добавь задание вручную»
```
PY -m orchestrator.cli add-task runs\<id> --kind fetch --title "..." --url "https://..."
```

### «посмотреть, что нашли»
Веб-страница Run'а (находки/сводки кликабельны, бэклог, события, дашборд) или
файлы в `runs/<id>/findings|reports`.

## 5. Где что в коде

- `orchestrator/loop.py` — `run` (драйвер) + `run_round` (волна, concurrency).
- `orchestrator/worker.py` — спавн `pi -p --mode json`, фазы/консоль (пишется и на провале), запись Finding.
- `orchestrator/planner.py` — `slice_spec` (ТЗ → backlog).
- `orchestrator/intake.py` — `create_intake_run` (новое исследование), `build_refine` (доуточнение ТЗ), лаунчеры pi/claude.
- `orchestrator/checkpoint.py` — харвест ссылок, `synth_report` (3 ретрая, иначе исключение→пауза), `interim_report`.
- `orchestrator/store.py` — вся ФС-IO (атомарно); `progress.py` — фазы/консоль воркеров.
- `web/app.py` — FastAPI: страницы, SSE (`events-stream`), контролы, рендер md;
  заглавная — баннер активного Run'а + `/api/home-status` (поллинг).
- `web/runner.py` — `RunManager`: запуск/пауза раунда в фоне. `web/llm_meter.py` —
  `/slots`: короткая шкала + поминутная история за 12ч (персист в `.llm_load.json`).
- `extension/index.ts` — pi-инструменты `web_search`/`web_fetch` (+YouTube, +JS-рендер).
- `tools/` — `youtube_one.py`, `fetch_page.py`, `rescan_failed.py`, `compile_rounds.py`.

Запуск пакетов: из корня репо, `PY -m web` (морда), `PY -m orchestrator.cli …` (CLI).

## 6. Правила

- Истина — файлы Run'а; не держать состояние в процессах.
- Записи атомарны (`.tmp`→replace); консоль/находки переживают рестарт.
- Не коммитить `runs/` (данные, .gitignore). Веб создаёт `runs/` сам.
