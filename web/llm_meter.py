"""Семплер загрузки LLM через llama.cpp `/slots`.

llama-server на :8080 отдаёт массив слотов с `is_processing`. Семплируем часто
и считаем «% времени, что LLM занята» за окно — это показывает, простаивает ли
модель (значит воркеров мало / упёрлись в сеть) или загружена под завязку.

Две шкалы: короткая (последние ~минуты, deque сэмплов) для live-ощущения и
длинная — поминутная агрегация за 12ч (`hist12h`), переживает рестарт через
персист в файл (десятки рестартов в день не должны обнулять дневной график).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

SLOTS_URL = os.environ.get("DR_LLM_SLOTS_URL", "http://127.0.0.1:8080/slots")
INTERVAL = float(os.environ.get("DR_LLM_SAMPLE_SEC", "0.7"))
HISTORY_FILE = Path(os.environ.get(
    "DR_LLM_HISTORY", str(Path(__file__).resolve().parent.parent / ".llm_load.json")))
WINDOW_SEC = 12 * 3600   # глубина длинной шкалы
BUCKET_SEC = 60          # один бакет = минута → 720 точек за 12ч


class LlmMeter:
    def __init__(self, maxlen: int = 120) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)  # доля занятых слотов 0..1 (короткая шкала)
        self.total = 0
        self.busy = 0
        self.ok = False
        self._thread: threading.Thread | None = None
        # длинная шкала: поминутные средние [ts_epoch, busy_fraction] за последние 12ч
        self._hist: list[list[float]] = []
        self._bsum = 0.0          # сумма долей в текущем бакете
        self._bn = 0              # число сэмплов в текущем бакете
        self._bstart: float | None = None
        self._load_hist()

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="llm-meter", daemon=True)
        self._thread.start()

    # ---- персист длинной шкалы ----
    def _load_hist(self) -> None:
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            cutoff = time.time() - WINDOW_SEC
            self._hist = [[float(t), float(v)] for t, v in data if float(t) >= cutoff]
        except Exception:  # noqa: BLE001 — нет файла/битый → пустая история
            self._hist = []

    def _save_hist(self) -> None:
        try:
            HISTORY_FILE.write_text(json.dumps(self._hist), encoding="utf-8")
        except Exception:  # noqa: BLE001 — персист не критичен
            pass

    def _flush_bucket(self, now: float) -> None:
        if self._bn:                                   # пустой бакет (LLM был недоступен) — пропускаем, не пишем 0%
            self._hist.append([now, round(self._bsum / self._bn, 3)])
        cutoff = now - WINDOW_SEC
        self._hist = [b for b in self._hist if b[0] >= cutoff]
        self._bsum, self._bn, self._bstart = 0.0, 0, now
        self._save_hist()

    def _loop(self) -> None:
        while True:
            now = time.time()
            if self._bstart is None:
                self._bstart = now
            try:
                with urllib.request.urlopen(SLOTS_URL, timeout=3) as r:
                    slots = json.loads(r.read().decode("utf-8", "replace"))
                total = len(slots)
                busy = sum(1 for s in slots if s.get("is_processing"))
                self.ok, self.total, self.busy = True, total, busy
                frac = busy / total if total else 0.0
                self._samples.append(frac)
                self._bsum += frac
                self._bn += 1
            except Exception:  # noqa: BLE001 — сервер мог быть недоступен, это нормально
                self.ok = False
            if now - self._bstart >= BUCKET_SEC:
                self._flush_bucket(now)
            time.sleep(INTERVAL)

    def snapshot(self, history: bool = False) -> dict:
        """Короткая шкала всегда; длинная (сетка 12ч + средние по окнам) — только при history=True
        (страница Run'а её не использует, незачем гонять 720 чисел в каждом SSE-тике)."""
        s = list(self._samples)
        pct = round(100 * sum(s) / len(s)) if s else 0
        out = {
            "ok": self.ok, "total": self.total, "busy": self.busy,
            "pct": pct, "spark": [round(x, 2) for x in s[-60:]],
        }
        if history:
            hist = list(self._hist)  # копия на момент чтения (GIL хватает)
            now = time.time()
            # Сетка окна: слот 0 = 12ч назад, последний = сейчас; пустые минуты = 0 (не растянутый бар).
            n = WINDOW_SEC // BUCKET_SEC
            start = now - WINDOW_SEC
            grid = [0.0] * n
            for ts, val in hist:
                i = int((ts - start) // BUCKET_SEC)
                if 0 <= i < n:
                    grid[i] = round(val, 3)

            def avg_since(sec: int) -> int:                   # средн. занятость по записанным минутам окна
                vals = [v for ts, v in hist if ts >= now - sec]
                return round(100 * sum(vals) / len(vals)) if vals else 0

            out["hist12h"] = grid
            out["avgw"] = {"12h": avg_since(12 * 3600), "3h": avg_since(3 * 3600), "1h": avg_since(3600)}
        return out


meter = LlmMeter()
