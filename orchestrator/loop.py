"""Цикл Round'а + автономный драйвер.

run_round  — одна тайм-боксированная волна Worker'ов с concurrency.
run        — автономно крутит раунды: (plan если пусто) → волна → Checkpoint →
             решение продолжать, до бюджета раундов или пока есть ready.

should_stop — необязательный коллбэк кооперативной паузы: когда вернёт True,
перестаём запускать новые задания (in-flight доигрывают), и драйвер уходит в
paused после ближайшего Checkpoint'а.
"""
from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .checkpoint import checkpoint, harvest_links
from .planner import slice_spec
from .store import RunStore, now_iso
from .worker import run_worker

_NEVER = lambda: False  # noqa: E731
CONCURRENCY_CAP = 16  # потолок слотов; слайдер живёт в [1..CAP], читается на лету


def completed_rounds(store: RunStore) -> int:
    """Число ПРОЙДЕННЫХ раундов = число написанных Report'ов (reports/round-NN.md).
    Раунд считается завершённым только когда Checkpoint написал его сводку; раунд,
    прерванный паузой/рестартом до сводки, не засчитывается — переигрывается."""
    return len(list(store.reports_dir.glob("round-*.md")))


def run_round(store: RunStore, should_stop=None) -> dict:
    stop = should_stop or _NEVER
    cfg = store.config()
    st = store.state()
    # рабочий раунд = (пройдено по Report'ам) + 1; незавершённый раунд переигрывается с тем же номером
    rnd = completed_rounds(store) + 1
    budget = cfg["round_time_budget_sec"]["first" if rnd == 1 else "rest"]

    def cur_conc() -> int:
        try:
            return max(1, min(CONCURRENCY_CAP, int(store.config().get("concurrency", 3))))
        except Exception:  # noqa: BLE001
            return 3

    store.save_state({**st, "status": "running", "round": rnd, "round_started_at": now_iso()})
    store.log_event("round_start", round=rnd, budget_sec=budget, concurrency=cur_conc())
    deadline = time.monotonic() + budget

    ready = [t["id"] for t in store.backlog() if t.get("status") == "ready"]
    done, failed, idx = 0, 0, 0
    pending: set = set()

    def can_submit() -> bool:
        # concurrency читаем на лету — слайдер из UI меняет число слотов в реальном времени
        return idx < len(ready) and len(pending) < cur_conc() and time.monotonic() < deadline and not stop()

    with ThreadPoolExecutor(max_workers=CONCURRENCY_CAP) as ex:
        while can_submit():
            pending.add(ex.submit(run_worker, store, ready[idx])); idx += 1
        while pending:
            # timeout=2: просыпаемся, даже если никто не завершился, чтобы
            # подхватить повышение concurrency со слайдера почти сразу
            finished, pending = wait(pending, timeout=2, return_when=FIRST_COMPLETED)
            for fut in finished:
                try:
                    fut.result(); done += 1
                except Exception as e:
                    failed += 1
                    store.log_event("worker_exception", error=str(e)[:300])
            while can_submit():
                pending.add(ex.submit(run_worker, store, ready[idx])); idx += 1

    skipped = len(ready) - idx
    store.log_event("round_end", round=rnd, done=done, failed=failed, skipped=skipped)
    return {"round": rnd, "done": done, "failed": failed, "skipped": skipped}


def run(store: RunStore, should_stop=None) -> dict:
    """Автономный прогон Run'а до бюджета раундов / пустого бэклога / паузы."""
    stop = should_stop or _NEVER
    if not store.backlog():
        slice_spec(store)

    rounds = 0
    while True:
        # Решение «крутить ли раунд» — ДО его запуска (иначе при исчерпанном
        # бюджете каждое нажатие «Запустить» прокручивало бы лишний раунд).
        # Считаем по ПРОЙДЕННЫМ раундам (Report'ам), а не по счётчику round
        # (он может указывать на незавершённый раунд).
        done = completed_rounds(store)
        st = store.state()
        budget = int(st.get("rounds_budget", 1))
        has_ready = any(t.get("status") == "ready" for t in store.backlog())
        if done >= budget or not has_ready:
            store.save_state({**st, "status": "done", "round": done})
            return {"rounds": rounds, "status": "done"}

        run_round(store, should_stop)
        rnd = int(store.state().get("round", 0))
        rounds += 1
        if stop():
            # hard-kill пауза: воркеры уже убиты, их задания вернулись в ready.
            # Дешёвый харвест ссылок (без LLM-синтеза) — чтобы не потерять «ещё
            # почитать»; сводку по требованию даёт «Отчёт сейчас». Раунд НЕ
            # завершён (сводки нет) → в state пишем число реально пройденных.
            harvest_links(store, rnd)
            store.save_state({**store.state(), "status": "paused", "round": completed_rounds(store)})
            return {"rounds": rounds, "status": "paused"}
        checkpoint(store, rnd)  # синтез сводки + харвест; решение продолжать — на след. итерации сверху
