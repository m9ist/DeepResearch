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
| `spec.md` | ТЗ (выход интервью-Intake) |
| `config.json` | `concurrency`, `worker_provider/worker_model`, `rounds_budget`, лимиты |
| `state.json` | `status` (intake/running/paused/done), `round`, `rounds_budget` |
| `backlog.jsonl` | задания: `{id, kind(search/fetch/topic), title, query?/url?, status(ready/in_progress/done), source}` |
| `events.jsonl` | лог: `worker_start/end`, `round_start/end`, `harvest`, `checkpoint`, … |
| `findings/NNNN.md` | находки (summary одного задания) |
| `reports/round-NN.md`, `interim-NN.md`, `compiled-*.md` | сводки |
| `sources/` | сырьё: транскрипты YT, отрендеренные страницы |
| `console/<task_id>.log` | пост-мортем консоли воркера |
| `prompts/`, `sessions/` | промпты воркеров; сессия pi-интервью |

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

### «добавь задание вручную»
```
PY -m orchestrator.cli add-task runs\<id> --kind fetch --title "..." --url "https://..."
```

### «посмотреть, что нашли»
Веб-страница Run'а (находки/сводки кликабельны, бэклог, события, дашборд) или
файлы в `runs/<id>/findings|reports`.

## 5. Где что в коде

- `orchestrator/loop.py` — `run` (драйвер) + `run_round` (волна, concurrency).
- `orchestrator/worker.py` — спавн `pi -p --mode json`, фазы/консоль, запись Finding.
- `orchestrator/planner.py` — `slice_spec` (ТЗ → backlog).
- `orchestrator/checkpoint.py` — харвест ссылок, `synth_report`, `interim_report`.
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
