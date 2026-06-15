"""Worker — один вызов pi на одно задание Backlog'а, со стримингом `--mode json`.

Стриминг даёт две вещи: (1) живые фазы воркера (ищет/читает/думает/пишет) в
`progress`-реестр для дашборда; (2) тело Finding'а берём из события `agent_end`
(текст последнего assistant-сообщения). Файл Finding'а пишет оркестратор —
атомарно, с frontmatter (контроль на стороне ФС-истины).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import procs, progress
from .pi_runner import resolve_pi
from .store import RunStore, now_iso

EXTENSION = str(Path(__file__).resolve().parent.parent / "extension" / "index.ts")
YT_SCRIPT = str(Path(__file__).resolve().parent.parent / "tools" / "youtube_one.py")
FETCH_SCRIPT = str(Path(__file__).resolve().parent.parent / "tools" / "fetch_page.py")


def build_prompt(task: dict, spec_text: str, max_urls: int) -> str:
    kind = task.get("kind", "topic")
    target = task.get("query") or task.get("url") or task.get("title", "")
    how = {
        "search": f"Сделай web_search по запросу «{target}», выбери {max_urls} релевантных источника и web_fetch их.",
        "fetch": f"Сделай web_fetch по URL {target} и при необходимости уточни web_search'ем.",
        "topic": f"Сам подбери запросы, web_search, выбери до {max_urls} источников и web_fetch их.",
    }.get(kind, "Используй web_search и web_fetch по ситуации.")

    return f"""Ты — исследовательский подагент. Выполни ОДИН шаг исследования.

# Задание ({kind})
{task.get("title", target)}
Цель/запрос/URL: {target}

# Как действовать
{how}
Используй инструменты web_search и web_fetch. Не выдумывай факты — опирайся на
прочитанное, давай ссылки. web_fetch по ссылке YouTube возвращает ТРАНСКРИПТ
видео (а не страницу) — используй его. Сырьё (страницы/транскрипты) система
сохраняет автоматически. Для богатых источников (видео, лонгриды) делай саммари
развёрнутым: связный пересказ + ключевые тезисы автора.

ВАЖНО: если вывод инструмента помечен «обрезано/transcript обрезан» — это лимит
вывода, а НЕ отсутствие данных. Не ищи продолжение в вебе (его там нет), работай
с тем, что получил.

# Контекст исследования (Spec, для фокуса)
{spec_text[:1500]}

# Что вернуть
Верни ТОЛЬКО markdown-документ строго по формату ниже. Без преамбулы, без
обрамляющих ``` , без frontmatter (его добавит система):

## Куда сходил
<какие запросы/URL и что за источники>

## Что вытащил (саммари)
<сжатое содержание с inline-ссылками [title](url)>

## Ещё почитать
- [title](url) — почему стоит
(каждая строка — реальная ссылка из выдачи; если нет — напиши «нет»)

## Смежные темы
- <тема> — чем релевантна

## Рекомендация
<стоит ли копать глубже и куда>
"""


def _clean_body(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _phase_from_event(task_id: str, o: dict) -> None:
    t = o.get("type")
    if t == "tool_execution_start":
        tn, a = o.get("toolName"), o.get("args", {})
        arg = a.get("query") or a.get("url") or ""
        if tn == "web_search":
            progress.set_phase(task_id, "ищет", arg)
        elif tn == "web_fetch":
            progress.set_phase(task_id, "читает", arg)
        else:
            progress.set_phase(task_id, tn or "инструмент")
        progress.append_line(task_id, f"→ {tn} {arg}".rstrip())
    elif t == "tool_execution_end":
        progress.set_phase(task_id, "анализирует")
        progress.append_line(task_id, "✓ готово")
    elif t == "message_update":
        ev = o.get("assistantMessageEvent") or {}
        content = (ev.get("partial") or {}).get("content") or []
        last = content[-1] if content else {}
        kind = last.get("type")
        if kind == "thinking":
            progress.set_phase(task_id, "думает")
            progress.set_tail(task_id, "💭 " + (last.get("thinking") or "")[-180:])
        elif kind == "text":
            progress.set_phase(task_id, "пишет находку")
            progress.set_tail(task_id, "✍ " + (last.get("text") or "")[-180:])
        elif kind == "toolCall":
            progress.set_phase(task_id, "зовёт инструмент")


def _log_message(task_id: str, msg: dict) -> None:
    """Полный вывод в консоль из завершённого сообщения (без фильтрации)."""
    role = msg.get("role")
    if role == "toolResult":
        txt = " ".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text").strip()
        prev = txt[:600] + (" […обрезано]" if len(txt) > 600 else "")
        progress.append_line(task_id, f"⟵ {msg.get('toolName', '')}: {prev}")
        return
    if role == "assistant":
        for c in msg.get("content", []):
            ct = c.get("type")
            if ct == "thinking" and (c.get("thinking") or "").strip():
                progress.append_line(task_id, "💭 " + c["thinking"].strip())
            elif ct == "text" and (c.get("text") or "").strip():
                progress.append_line(task_id, "✍ " + c["text"].strip())
            # toolCall пропускаем — он уже залогирован живьём через tool_execution_start


def _save_console(store: RunStore, task_id: str) -> None:
    """Сохранить консоль воркера в console/<task_id>.log (пост-мортем) до clear()."""
    lines = progress.dump(task_id)
    if lines:
        try:
            store.write_console(task_id, "\n".join(lines))
        except Exception:  # noqa: BLE001 — лог не критичен
            pass


def _extract_body(messages: list) -> str | None:
    for m in reversed(messages or []):
        if m.get("role") == "assistant":
            texts = [c.get("text", "") for c in m.get("content", []) if c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                return joined
    return None


def run_worker(store: RunStore, task_id: str, timeout: int = 600) -> str:
    cfg = store.config()
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")

    prompt = build_prompt(task, store.spec_text(), int(cfg.get("max_urls_per_task", 3)))
    prompt_rel = store.write_prompt(task_id, prompt)
    store.update_task(task_id, status="in_progress")
    progress.set_phase(task_id, "старт")

    cmd = resolve_pi() + [
        "-p", prompt, "-e", EXTENSION, "-t", "web_search,web_fetch",
        "--provider", cfg["worker_provider"], "--model", cfg["worker_model"],
        "--no-session", "--mode", "json",
    ]
    env = {
        **os.environ,
        "DR_SEARXNG_URL": cfg.get("searxng_url", "http://localhost:8888"),
        "DR_RUN_DIR": str(store.dir),       # куда расширение пишет sources/
        "DR_YT_SCRIPT": YT_SCRIPT,           # youtube_one.py
        "DR_FETCH_SCRIPT": FETCH_SCRIPT,     # fetch_page.py (Playwright, JS-фолбэк)
        "DR_PYTHON": sys.executable,         # тот же python, что у оркестратора (venv)
    }

    started = now_iso()
    store.log_event("worker_start", task_id=task_id, model=cfg["worker_model"], prompt=prompt_rel)

    proc = subprocess.Popen(cmd, env=env, cwd=str(store.dir), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    procs.register(task_id, proc)
    result: dict = {"body": None}

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            _phase_from_event(task_id, o)
            if o.get("type") == "message_end":
                _log_message(task_id, o.get("message", {}))
            if o.get("type") == "agent_end":
                result["body"] = _extract_body(o.get("messages", []))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        procs.unregister(task_id)
        store.update_task(task_id, status="ready")
        _save_console(store, task_id)
        progress.clear(task_id)
        store.log_event("worker_end", task_id=task_id, status="timeout")
        raise
    th.join(timeout=5)
    procs.unregister(task_id)

    body = result["body"]
    if proc.returncode != 0 or not body:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        store.update_task(task_id, status="ready")
        _save_console(store, task_id)
        progress.clear(task_id)
        # returncode при hard-kill (Pause) — не «ошибка», задание просто вернулось в ready
        status = "killed" if proc.returncode and proc.returncode < 0 else "error"
        store.log_event("worker_end", task_id=task_id, status=status,
                        returncode=proc.returncode, stderr=err[:500])
        raise RuntimeError(f"pi exited {proc.returncode} / нет тела: {err[:300]}")

    _fid, finding_rel = store.write_finding({
        "task_id": task_id, "round": store.state().get("round", 0),
        "kind": task.get("kind", "topic"), "source_url": task.get("url", ""),
        "model": cfg["worker_model"], "started": started, "finished": now_iso(),
    }, _clean_body(body))

    _save_console(store, task_id)
    progress.clear(task_id)
    store.update_task(task_id, status="done", finding=finding_rel)
    store.log_event("worker_end", task_id=task_id, status="ok", finding=finding_rel)
    return finding_rel
