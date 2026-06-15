"""RunStore — вся файловая IO одного Run'а. Истина на диске (см. ADR-0001).

Записи атомарны (tmp в той же папке + os.replace), чтобы hard-kill Worker'а
не оставлял рваных файлов.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


DEFAULT_CONFIG = {
    "concurrency": 3,
    "worker_provider": "ollama",
    "worker_model": "qwen/qwen3.6-35b-a3b",
    "rounds_budget": 3,
    "round_time_budget_sec": {"first": 420, "rest": 900},
    "max_urls_per_task": 3,
    "search_provider": "searxng",
    "searxng_url": "http://localhost:8888",
}


class RunStore:
    def __init__(self, run_dir: str | os.PathLike):
        self.dir = Path(run_dir)
        self.findings_dir = self.dir / "findings"
        self.prompts_dir = self.dir / "prompts"
        self.reports_dir = self.dir / "reports"
        # Воркеры бегут в потоках и пишут backlog/events/findings — сериализуем.
        self._lock = threading.RLock()

    # ---- scaffolding ----
    def init(self, spec_text: str = "", config: dict | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.findings_dir.mkdir(exist_ok=True)
        self.prompts_dir.mkdir(exist_ok=True)
        self.reports_dir.mkdir(exist_ok=True)
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        _atomic_write(self.dir / "config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        self.save_state({
            "status": "idle", "round": 0, "rounds_budget": cfg["rounds_budget"],
            "started_at": now_iso(), "updated_at": now_iso(),
        })
        # spec.md пишем ровно как передано (для intake — пусто, пока интервью
        # не запишет настоящий ТЗ; пустой spec не считается готовым — см. guard).
        if not (self.dir / "spec.md").exists():
            _atomic_write(self.dir / "spec.md", spec_text)
        for f in ("backlog.jsonl", "events.jsonl"):
            (self.dir / f).touch(exist_ok=True)

    # ---- config / state ----
    def config(self) -> dict:
        return json.loads((self.dir / "config.json").read_text(encoding="utf-8"))

    def update_config(self, **changes) -> dict:
        with self._lock:
            cfg = self.config()
            cfg.update(changes)
            _atomic_write(self.dir / "config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
            return cfg

    def state(self) -> dict:
        return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        state["updated_at"] = now_iso()
        _atomic_write(self.dir / "state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    def spec_text(self) -> str:
        p = self.dir / "spec.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ---- backlog (jsonl) ----
    def backlog(self) -> list[dict]:
        p = self.dir / "backlog.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _write_backlog(self, tasks: list[dict]) -> None:
        text = "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks)
        _atomic_write(self.dir / "backlog.jsonl", text)

    def add_task(self, task: dict) -> dict:
        with self._lock:
            tasks = self.backlog()
            if "id" not in task:
                task["id"] = f"t{len(tasks) + 1:04d}"
            task.setdefault("status", "ready")
            task.setdefault("round_added", self.state().get("round", 0))
            tasks.append(task)
            self._write_backlog(tasks)
            return task

    def update_task(self, task_id: str, **changes) -> None:
        with self._lock:
            tasks = self.backlog()
            for t in tasks:
                if t["id"] == task_id:
                    t.update(changes)
                    break
            self._write_backlog(tasks)

    def get_task(self, task_id: str) -> dict | None:
        return next((t for t in self.backlog() if t["id"] == task_id), None)

    def next_ready(self) -> dict | None:
        return next((t for t in self.backlog() if t.get("status") == "ready"), None)

    # ---- events log (append-only jsonl) ----
    def events(self) -> list[dict]:
        p = self.dir / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def log_event(self, event: str, **fields) -> None:
        rec = {"ts": now_iso(), "event": event, **fields}
        with self._lock, (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- artifacts ----
    def write_prompt(self, task_id: str, text: str) -> str:
        self.prompts_dir.mkdir(exist_ok=True)
        _atomic_write(self.prompts_dir / f"{task_id}.txt", text)
        return f"prompts/{task_id}.txt"

    def write_finding(self, frontmatter: dict, body: str) -> tuple[str, str]:
        """Аллокация id + запись файла — атомарно под локом (иначе два воркера
        возьмут один номер). Возвращает (finding_id, относительный путь)."""
        with self._lock:
            self.findings_dir.mkdir(exist_ok=True)
            nums = [int(p.stem) for p in self.findings_dir.glob("*.md") if p.stem.isdigit()]
            fid = f"{(max(nums) + 1) if nums else 1:04d}"
            fm = {"id": fid, **frontmatter}
            fm_text = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
            doc = f"---\n{fm_text}\n---\n\n{body.strip()}\n"
            _atomic_write(self.findings_dir / f"{fid}.md", doc)
            return fid, f"findings/{fid}.md"

    def findings(self) -> list[dict]:
        """Разобранные Finding'и: {id, fm (frontmatter), body}."""
        out = []
        for p in sorted(self.findings_dir.glob("*.md")):
            txt = p.read_text(encoding="utf-8")
            fm: dict = {}
            body = txt
            if txt.startswith("---"):
                _, fm_block, body = txt.split("---", 2)
                for line in fm_block.strip().splitlines():
                    k, _sep, v = line.partition(":")
                    try:
                        fm[k.strip()] = json.loads(v.strip())
                    except json.JSONDecodeError:
                        fm[k.strip()] = v.strip()
            out.append({"id": p.stem, "fm": fm, "body": body.lstrip("\n")})
        return out

    def write_console(self, task_id: str, text: str) -> str:
        d = self.dir / "console"
        d.mkdir(exist_ok=True)
        _atomic_write(d / f"{task_id}.log", text)
        return f"console/{task_id}.log"

    def write_report_named(self, stem: str, text: str) -> str:
        self.reports_dir.mkdir(exist_ok=True)
        _atomic_write(self.reports_dir / f"{stem}.md", text.strip() + "\n")
        return f"reports/{stem}.md"

    def write_report(self, round_no: int, text: str) -> str:
        return self.write_report_named(f"round-{round_no:02d}", text)
