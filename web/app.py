"""Read-only вьюер: список Run'ов, рендер md (spec/findings/reports), таймлайн,
бэклог, кнопка «открыть .md во внешнем просмотрщике». Контролов пока нет.

Запуск:  python -m deep_research.web   (или uvicorn deep_research.web.app:app)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from orchestrator import progress
from orchestrator.checkpoint import interim_report
from orchestrator.intake import build_refine, create_intake_run, ping_agent, suggest_name
from orchestrator.store import RunStore
from .llm_meter import meter
from .runner import manager

meter.start()

BASE = Path(__file__).resolve().parent
RUNS = BASE.parent / "runs"
PI_MODELS = Path.home() / ".pi" / "agent" / "models.json"


def pi_models() -> list[dict]:
    """Плоский список {provider, model} из ~/.pi/agent/models.json (для выбора модели воркера)."""
    try:
        data = json.loads(PI_MODELS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый json → пустой список
        return []
    out = []
    for prov, pv in (data.get("providers") or {}).items():
        for m in pv.get("models") or []:
            if m.get("id"):
                out.append({"provider": prov, "model": m["id"]})
    return out
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["ts"] = lambda s: (s or "").replace("T", " ").replace("Z", "")
app = FastAPI(title="Deep Research Viewer")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

_MD = ["extra", "tables", "fenced_code", "sane_lists"]
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def _fix_lists(text: str) -> str:
    """python-markdown не даёт списку «прерывать» абзац без пустой строки.
    LLM часто пишет «Заголовок:\n1. …\n2. …» вплотную → схлопывается в абзац.
    Вставляем пустую строку перед началом списка (вне fenced-блоков)."""
    out: list[str] = []
    in_fence = False
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _LIST_RE.match(line) and out:
            prev = out[-1]
            if prev.strip() and not _LIST_RE.match(prev):
                out.append("")
        out.append(line)
    return "\n".join(out)


def run_summary(spec_text: str) -> str:
    """Краткий смысл исследования из spec.md (для широкой колонки списка)."""
    lines = spec_text.splitlines()
    for ln in lines:  # заголовок вида «# Spec — <смысл>»
        if ln.startswith("# "):
            t = ln[2:].strip()
            if "—" in t:
                return t.split("—", 1)[1].strip()[:160]
            break
    for ln in lines:  # иначе — первая содержательная строка
        s = ln.strip().lstrip("-*> ").strip()
        if not s or s.startswith("#"):
            continue
        for pre in ("Вопрос исследования", "Вопрос"):
            if s.startswith(pre):
                s = s[len(pre):].lstrip(": ").strip()
                break
        return s[:160]
    return ""


def render_md(text: str) -> str:
    return md.markdown(_fix_lists(text), extensions=_MD)


def _relink_internal(html: str, name: str) -> str:
    """Ссылки вида (../)findings/NNNN.md и (../)reports/X.md → во внутренний просмотрщик."""
    return re.sub(
        r'href="(?:\.\./)?((?:findings|reports)/[^"]+\.md)"',
        lambda m: f'href="/run/{name}/doc?path={m.group(1)}"', html)


def _is_run(d: Path) -> bool:
    return d.is_dir() and ((d / "state.json").exists() or (d / "config.json").exists())


def list_runs() -> list[dict]:
    out = []
    for d in sorted([p for p in RUNS.iterdir() if _is_run(p)], reverse=True) if RUNS.exists() else []:
        s = RunStore(d)
        try:
            st = s.state()
        except Exception:
            st = {}
        bl = s.backlog()
        out.append({
            "name": d.name,
            "status": st.get("status", "?"),
            "round": st.get("round"),
            "rounds_budget": st.get("rounds_budget"),
            "findings": len(list((d / "findings").glob("*.md"))) if (d / "findings").exists() else 0,
            "ready": sum(1 for t in bl if t.get("status") == "ready"),
            "tasks": len(bl),
            "updated": st.get("updated_at", ""),
            "summary": run_summary(s.spec_text()),
        })
    out.sort(key=lambda r: r["updated"], reverse=True)  # свежие сверху (ISO сортируется лексикографически)
    return out


def _active_info() -> dict | None:
    """Сводка по реально идущему Run'у (manager.active() — истина живости) для заглавной."""
    name = manager.active()
    if not name:
        return None
    d = RUNS / name
    if not _is_run(d):
        return None
    s = RunStore(d)
    try:
        st = s.state()
    except Exception:  # noqa: BLE001
        st = {}
    bl = s.backlog()
    return {
        "name": name, "status": st.get("status", "running"),
        "round": st.get("round"), "rounds_budget": st.get("rounds_budget"),
        "findings": len(list((d / "findings").glob("*.md"))) if (d / "findings").exists() else 0,
        "ready": sum(1 for t in bl if t.get("status") == "ready"),
        "summary": run_summary(s.spec_text()),
    }


def _run_dir(name: str) -> Path:
    d = (RUNS / name).resolve()
    if not str(d).startswith(str(RUNS.resolve())) or not _is_run(d):
        raise HTTPException(404, "run not found")
    return d


def _safe_md(run_dir: Path, rel: str) -> Path:
    p = (run_dir / rel).resolve()
    if not str(p).startswith(str(run_dir.resolve())) or p.suffix != ".md" or not p.exists():
        raise HTTPException(404, "file not found")
    return p


def _has_console(run_dir: Path, task_id: str) -> bool:
    return bool(task_id) and (run_dir / "console" / f"{task_id}.log").exists()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "runs": list_runs(), "active": _active_info(), "llm": meter.snapshot(history=True),
    })


@app.get("/api/home-status")
def home_status():
    """Лёгкий поллинг для заглавной: активный Run + загрузка LLM (с длинной шкалой)."""
    return {"active": _active_info(), "llm": meter.snapshot(history=True)}


@app.get("/run/{name}", response_class=HTMLResponse)
def run_view(request: Request, name: str):
    d = _run_dir(name)
    s = RunStore(d)
    try:
        state = s.state()
    except Exception:
        state = {}
    findings = [{"id": f["id"], "kind": f["fm"].get("kind", ""), "task_id": f["fm"].get("task_id", ""),
                 "rel": f"findings/{f['id']}.md"} for f in s.findings()]
    reports = [{"name": p.name, "rel": f"reports/{p.name}"}
               for p in sorted((d / "reports").glob("*.md"))] if (d / "reports").exists() else []
    backlog = s.backlog()
    for t in backlog:
        t["has_console"] = _has_console(d, t.get("id", ""))
    return templates.TemplateResponse(request, "run.html", {
        "name": name, "state": state, "config": s.config() if (d / "config.json").exists() else {},
        "spec_html": render_md(s.spec_text()), "spec_versions": s.spec_versions(),
        "findings": findings, "reports": reports,
        "backlog": backlog, "events": list(reversed(s.events())),
        "has_intake": (d / "intake.cmd").exists(), "has_spec": (d / "spec.md").exists() and bool(s.spec_text().strip()),
        "spec_mtime": (d / "spec.md").stat().st_mtime if (d / "spec.md").exists() else None,
        "models": pi_models(),
        "is_active": manager.is_active(name), "active_other": manager.active() if (manager.active() and manager.active() != name) else None,
    })


@app.get("/run/{name}/doc", response_class=HTMLResponse)
def doc_view(request: Request, name: str, path: str):
    d = _run_dir(name)
    p = _safe_md(d, path)
    return templates.TemplateResponse(request, "doc.html", {
        "name": name, "path": path,
        "html": _relink_internal(render_md(p.read_text(encoding="utf-8")), name),
    })


@app.post("/run/{name}/open")
def open_external(name: str, path: str):
    d = _run_dir(name)
    p = _safe_md(d, path)
    try:
        os.startfile(str(p))  # noqa: S606 — открыть .md во внешнем редакторе (Windows)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"не удалось открыть: {e}")
    return {"ok": True}  # без редиректа — браузер остаётся на месте


# ---- Intake: старт исследования ----
class PingIn(BaseModel):
    agent: str = "pi"


class NameIn(BaseModel):
    agent: str = "pi"
    idea: str


class CreateIn(BaseModel):
    agent: str = "pi"
    idea: str
    name: str


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request):
    return templates.TemplateResponse(request, "new.html", {})


@app.post("/api/ping")
def api_ping(b: PingIn):
    ok, msg = ping_agent(b.agent)
    return {"ok": ok, "msg": msg}


@app.post("/api/suggest-name")
def api_suggest(b: NameIn):
    try:
        return {"name": suggest_name(b.idea)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@app.post("/api/create")
def api_create(b: CreateIn):
    run = create_intake_run(b.idea, b.name, b.agent)
    return {"run": run}


def _run_snapshot(name: str, d: Path) -> dict:
    s = RunStore(d)
    try:
        st = s.state()
    except Exception:  # noqa: BLE001
        st = {}
    bl = s.backlog()
    ev = s.events()
    starts: dict[str, str] = {}
    for e in ev:
        if e.get("event") == "worker_start":
            starts[e.get("task_id")] = e.get("ts")
    in_prog = [t for t in bl if t.get("status") == "in_progress"]
    phases = progress.get_phases()
    inflight = [{"task_id": t["id"], "kind": t.get("kind", ""), "title": (t.get("title") or t.get("url") or "")[:80],
                 "started": starts.get(t["id"]),
                 "phase": phases.get(t["id"], {}).get("phase", ""),
                 "detail": phases.get(t["id"], {}).get("detail", ""),
                 "lines": phases.get(t["id"], {}).get("lines", []),
                 "tail": phases.get(t["id"], {}).get("tail", "")} for t in in_prog]
    counters = {
        "findings": len(list((d / "findings").glob("*.md"))) if (d / "findings").exists() else 0,
        "ready": sum(1 for t in bl if t.get("status") == "ready"),
        "done": sum(1 for t in bl if t.get("status") == "done"),
        "in_progress": len(in_prog), "total": len(bl),
    }
    recent = [{"ts": e.get("ts"), "event": e.get("event"),
               **{k: v for k, v in e.items() if k in ("task_id", "round", "added", "finding", "done")}}
              for e in ev[-14:]]
    findings = [{"id": f["id"], "kind": f["fm"].get("kind", ""), "task_id": f["fm"].get("task_id", "")}
                for f in s.findings()]
    reports = [p.name for p in sorted((d / "reports").glob("*.md"))] if (d / "reports").exists() else []
    backlog = [{"id": t.get("id"), "status": t.get("status"), "kind": t.get("kind", ""),
                "title": t.get("title", ""), "url": t.get("url", ""), "source": t.get("source", ""),
                "stage": t.get("round_added", 0), "has_console": _has_console(d, t.get("id", ""))} for t in bl]
    active_other = manager.active() if (manager.active() and manager.active() != name) else None
    try:
        concurrency = s.config().get("concurrency")
    except Exception:  # noqa: BLE001
        concurrency = None
    return {
        "status": st.get("status"), "round": st.get("round"), "rounds_budget": st.get("rounds_budget"),
        "round_started_at": st.get("round_started_at"), "round_budget_sec": st.get("round_budget_sec"),
        "phase": st.get("phase"), "is_active": manager.is_active(name),
        "concurrency": concurrency,
        "active_other": active_other, "counters": counters, "inflight": inflight, "recent": recent,
        "findings": findings, "reports": reports, "backlog": backlog, "llm": meter.snapshot(),
    }


@app.get("/run/{name}/console/{task_id}")
def console_log(name: str, task_id: str):
    d = _run_dir(name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", task_id):
        raise HTTPException(400, "bad task_id")
    p = d / "console" / f"{task_id}.log"
    if not p.exists():
        raise HTTPException(404, "консоль не сохранена")
    text = p.read_text(encoding="utf-8")
    entries: list[str] = []
    for line in text.split("\n"):
        if not entries or re.match(r"^\d{2}:\d{2}:\d{2} ", line):
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    title = task_id
    t = RunStore(d).get_task(task_id)
    if t:
        title = f"{task_id} · {t.get('kind', '')} · {(t.get('title') or t.get('url') or '')[:80]}"
    return {"title": title, "lines": entries}


@app.get("/run/{name}/events-stream")
async def events_stream(name: str, request: Request):
    d = _run_dir(name)

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(_run_snapshot(name, d), ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _searxng_ok(url: str) -> bool:
    try:
        urllib.request.urlopen(url.rstrip("/") + "/search?q=ping&format=json", timeout=4)
        return True
    except Exception:  # noqa: BLE001
        return False


@app.post("/run/{name}/run-rounds")
def run_rounds(name: str):
    d = _run_dir(name)
    s = RunStore(d)
    if not s.spec_text().strip():
        raise HTTPException(400, "нет ТЗ (spec.md) — сначала проведи интервью")
    st = s.state()
    if int(st.get("round", 0)) >= int(st.get("rounds_budget", 1)):
        raise HTTPException(400, "бюджет раундов исчерпан — нажми «+1 раунд»")
    if manager.active() and manager.active() != name:
        raise HTTPException(409, f"уже идёт другой Run: {manager.active()}")
    if manager.is_active(name):
        raise HTTPException(409, "этот Run уже идёт")
    cfg = s.config()
    if cfg.get("search_provider", "searxng") == "searxng" and not _searxng_ok(cfg.get("searxng_url", "http://localhost:8888")):
        raise HTTPException(400, "SearXNG не отвечает — подними docker (deep_research/searxng)")
    manager.start(d, name)
    return {"ok": True}


@app.post("/run/{name}/pause")
def pause_run(name: str):
    _run_dir(name)
    manager.pause(name)
    return {"ok": True}


@app.post("/run/{name}/report-now")
def report_now(name: str):
    d = _run_dir(name)
    s = RunStore(d)
    if not s.findings():
        raise HTTPException(400, "пока нет находок для отчёта")

    def _work():
        try:
            interim_report(s)
        except Exception as e:  # noqa: BLE001
            s.log_event("interim_report_error", error=str(e)[:200])

    threading.Thread(target=_work, name=f"report-{name}", daemon=True).start()
    return {"ok": True}


@app.post("/run/{name}/set-concurrency")
def set_concurrency(name: str, n: int):
    n = max(1, min(16, n))
    RunStore(_run_dir(name)).update_config(concurrency=n)
    return {"ok": True, "concurrency": n}


@app.post("/run/{name}/set-timeout")
def set_timeout(name: str, m: int):
    if m not in (0, 5, 10, 30):
        raise HTTPException(400, "допустимо: 0 (без таймаута), 5, 10, 30 мин")
    RunStore(_run_dir(name)).update_config(round_timeout_min=m)
    return {"ok": True, "round_timeout_min": m}


@app.post("/run/{name}/set-model")
def set_model(name: str, provider: str, model: str):
    if (provider, model) not in {(m["provider"], m["model"]) for m in pi_models()}:
        raise HTTPException(400, "модель не из списка models.json")
    RunStore(_run_dir(name)).update_config(worker_provider=provider, worker_model=model)
    return {"ok": True, "provider": provider, "model": model}


@app.post("/run/{name}/bump-rounds")
def bump_rounds(name: str, n: int = 1):
    s = RunStore(_run_dir(name))
    st = s.state()
    floor = max(int(st.get("round", 0) or 0), 1)  # не ниже пройденных раундов и не меньше 1
    st["rounds_budget"] = max(int(st.get("rounds_budget", 0)) + n, floor)
    s.save_state(st)
    s.log_event("rounds_budget_changed", to=st["rounds_budget"])
    return {"ok": True, "rounds_budget": st["rounds_budget"]}


@app.get("/run/{name}/spec-status")
def spec_status(name: str):
    """Подхват spec.md после интервью без перезагрузки страницы: фронт зовёт по 🔄."""
    d = _run_dir(name)
    s = RunStore(d)
    text = s.spec_text()
    sp = d / "spec.md"
    has = sp.exists() and bool(text.strip())
    if has:
        s.snapshot_spec()  # зафиксировать версию ТЗ при подхвате (интервью/уточнение)
    return {"has_spec": has, "spec_html": render_md(text) if has else "",
            "mtime": sp.stat().st_mtime if sp.exists() else None}


@app.post("/run/{name}/launch")
def launch_intake(name: str, which: str = "intake"):
    d = _run_dir(name)
    cmd = d / ("continue.cmd" if which == "continue" else "intake.cmd")
    if not cmd.exists():
        raise HTTPException(404, "лаунчер не найден")
    try:
        os.startfile(str(cmd))  # noqa: S606 — открыть терминал с pi/claude (Windows)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"не удалось запустить: {e}")
    return {"ok": True}


@app.post("/run/{name}/refine-launch")
def refine_launch(name: str, agent: str = "pi"):
    """Доуточнение ТЗ: генерим свежий бриф (идея+спек+незавершённые задания) и стартуем pi/claude в терминале."""
    d = _run_dir(name)
    s = RunStore(d)
    s.snapshot_spec()  # зафиксировать текущую версию ТЗ ДО того, как агент перезапишет spec.md
    cfg = s.config()
    build_refine(d, agent, provider=cfg.get("worker_provider", "ollama"),
                 model=cfg.get("worker_model", "qwen/qwen3.6-35b-a3b"))
    cmd = d / "refine.cmd"
    try:
        os.startfile(str(cmd))  # noqa: S606
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"не удалось запустить: {e}")
    return {"ok": True}


@app.post("/run/{name}/refine-apply")
def refine_apply(name: str):
    """Подхватить решения агента: refine_decisions.json → проставить статусы (drop→dropped, keep→ready)."""
    d = _run_dir(name)
    s = RunStore(d)
    s.snapshot_spec()  # зафиксировать новую версию ТЗ (агент уже перезаписал spec.md в диалоге)
    p = d / "refine_decisions.json"
    if not p.exists():
        raise HTTPException(404, "refine_decisions.json ещё нет — агент не записал решения")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"не разобрать refine_decisions.json: {str(e)[:120]}")
    dropped = kept = added = 0
    missing: list[str] = []
    for tid in (data.get("drop") or []):
        t = s.get_task(tid)
        if not t:
            missing.append(tid)
        elif t.get("status") == "ready":
            s.update_task(tid, status="dropped"); dropped += 1
    for tid in (data.get("keep") or []):
        t = s.get_task(tid)
        if t and t.get("status") == "dropped":
            s.update_task(tid, status="ready"); kept += 1
    for nt in (data.get("add") or []):                       # новые задания под уточнённый фокус
        title = (nt.get("title") or nt.get("query") or nt.get("url") or "").strip()
        if not title:
            continue
        task = {"kind": nt.get("kind", "search"), "title": title[:200], "source": "refine"}
        if nt.get("query"):
            task["query"] = nt["query"]
        if nt.get("url"):
            task["url"] = nt["url"]
        s.add_task(task); added += 1
    s.log_event("refine_applied", dropped=dropped, kept=kept, added=added, missing=len(missing))
    return {"ok": True, "dropped": dropped, "kept": kept, "added": added, "missing": missing}


@app.post("/run/{name}/task/{tid}/drop")
def task_drop(name: str, tid: str):
    s = RunStore(_run_dir(name))
    t = s.get_task(tid)
    if not t:
        raise HTTPException(404, "задание не найдено")
    if t.get("status") != "ready":
        raise HTTPException(400, f"убрать можно только ready (сейчас {t.get('status')})")
    s.update_task(tid, status="dropped")
    s.log_event("task_dropped", task_id=tid)
    return {"ok": True, "status": "dropped"}


@app.post("/run/{name}/task/{tid}/keep")
def task_keep(name: str, tid: str):
    s = RunStore(_run_dir(name))
    t = s.get_task(tid)
    if not t:
        raise HTTPException(404, "задание не найдено")
    if t.get("status") != "dropped":
        raise HTTPException(400, f"вернуть можно только dropped (сейчас {t.get('status')})")
    s.update_task(tid, status="ready")
    s.log_event("task_kept", task_id=tid)
    return {"ok": True, "status": "ready"}


def _recover_orphans() -> None:
    """При старте сервера активных потоков нет — любой 'running' в файлах залип
    после прошлого падения. Переводим в paused, in_progress → ready (SPEC §6)."""
    if not RUNS.exists():
        return
    for d in (p for p in RUNS.iterdir() if _is_run(p)):
        s = RunStore(d)
        try:
            st = s.state()
        except Exception:  # noqa: BLE001
            continue
        if st.get("status") == "running":
            for t in s.backlog():
                if t.get("status") == "in_progress":
                    s.update_task(t["id"], status="ready")
            # незавершённый раунд (без Report'а) не засчитываем — round = число пройденных стадий
            completed = len(list((d / "reports").glob("round-*.md"))) if (d / "reports").exists() else 0
            s.save_state({**st, "status": "paused", "round": completed, "phase": None})
            s.log_event("recovered_to_paused", round=completed)


_recover_orphans()
