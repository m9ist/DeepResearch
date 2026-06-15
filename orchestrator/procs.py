"""Реестр живых процессов воркеров — чтобы Pause мог сделать hard-kill.

Воркер регистрирует свой Popen на старте и снимает на финише; менеджер при паузе
зовёт kill_all(). Один процесс на всё (оркестратор + веб), поэтому общий словарь.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_procs: dict[str, object] = {}


def register(task_id: str, proc) -> None:
    with _lock:
        _procs[task_id] = proc


def unregister(task_id: str) -> None:
    with _lock:
        _procs.pop(task_id, None)


def kill_all() -> list[str]:
    """Убить все живые процессы воркеров. Возвращает список task_id."""
    with _lock:
        items = list(_procs.items())
        _procs.clear()
    killed = []
    for tid, p in items:
        try:
            p.kill()
            killed.append(tid)
        except Exception:  # noqa: BLE001 — процесс мог уже завершиться
            pass
    return killed
