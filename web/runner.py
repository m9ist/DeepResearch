"""Запуск раундов в фоне внутри веб-процесса. Один активный Run за раз.

Кооперативная пауза: ставим threading.Event, loop его видит через should_stop,
перестаёт запускать новые задания (in-flight доигрывают) и уходит в paused.
Hard-kill in-flight pi-процессов — отдельная доработка (см. BACKLOG).
"""
from __future__ import annotations

import threading

from orchestrator import procs
from orchestrator.loop import run
from orchestrator.store import RunStore


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: str | None = None        # имя текущего Run'а
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def active(self) -> str | None:
        return self._active

    def is_active(self, name: str) -> bool:
        return self._active == name and self._thread is not None and self._thread.is_alive()

    def start(self, run_dir, name: str) -> None:
        with self._lock:
            if self._active and self._thread and self._thread.is_alive():
                raise RuntimeError(f"уже идёт Run «{self._active}» — дождись или поставь паузу")
            self._active = name
            self._stop = threading.Event()
            store = RunStore(run_dir)

            def _target() -> None:
                try:
                    run(store, should_stop=self._stop.is_set)
                except Exception as e:  # noqa: BLE001
                    store.log_event("run_error", error=str(e)[:300])
                    store.save_state({**store.state(), "status": "paused"})
                finally:
                    self._active = None

            self._thread = threading.Thread(target=_target, name=f"run-{name}", daemon=True)
            self._thread.start()

    def pause(self, name: str) -> None:
        if self._active == name:
            self._stop.set()      # перестать запускать новые задания
            procs.kill_all()      # hard-kill: убить in-flight pi-процессы немедленно


manager = RunManager()
