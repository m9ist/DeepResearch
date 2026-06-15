"""In-memory реестр прогресса воркеров (task_id → фаза + мини-консоль).

Оркестратор и веб в одном процессе, поэтому потокобезопасного словаря хватает.
Воркер по ходу стриминга pi пишет: фазу, дискретные строки лога (→ инструмент,
✓ готово) и живой «хвост» (что модель думает/пишет сейчас). SSE это читает.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

_lock = threading.Lock()
_state: dict[str, dict] = {}


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _get(task_id: str) -> dict:
    s = _state.get(task_id)
    if s is None:
        s = {"phase": "", "detail": "", "lines": deque(maxlen=2000), "tail": ""}
        _state[task_id] = s
    return s


def set_phase(task_id: str, phase: str, detail: str = "") -> None:
    with _lock:
        s = _get(task_id)
        s["phase"] = phase
        s["detail"] = (detail or "")[:80]


def append_line(task_id: str, line: str) -> None:
    with _lock:
        s = _get(task_id)
        s["lines"].append(f"{_ts()} {line}")  # без обрезки — полный вывод
        s["tail"] = ""


def set_tail(task_id: str, tail: str) -> None:
    with _lock:
        _get(task_id)["tail"] = f"{_ts()} {(tail or '')}"[:200]


def dump(task_id: str) -> list[str]:
    """Все строки лога воркера (+ хвост) — для персиста перед clear()."""
    with _lock:
        s = _state.get(task_id)
        if not s:
            return []
        lines = list(s["lines"])
        if s.get("tail"):
            lines.append(s["tail"])
        return lines


def clear(task_id: str) -> None:
    with _lock:
        _state.pop(task_id, None)


def get_phases() -> dict[str, dict]:
    with _lock:
        return {k: {"phase": v["phase"], "detail": v["detail"],
                    "lines": list(v["lines"]), "tail": v["tail"]}  # все строки, без фильтрации
                for k, v in _state.items()}
