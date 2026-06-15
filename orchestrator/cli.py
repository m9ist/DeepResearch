"""CLI оркестратора (v1, для отладки одного Worker'а).

  python -m deep_research.orchestrator.cli init <run_dir> [--spec "..."]
  python -m deep_research.orchestrator.cli add-task <run_dir> --kind search --title "..." [--query "..."] [--url "..."]
  python -m deep_research.orchestrator.cli run-task <run_dir> <task_id>
  python -m deep_research.orchestrator.cli run-next <run_dir>
  python -m deep_research.orchestrator.cli plan <run_dir>
  python -m deep_research.orchestrator.cli run-round <run_dir>
"""
from __future__ import annotations

import argparse
import sys

from .loop import run, run_round
from .planner import slice_spec
from .store import RunStore
from .worker import run_worker


def main(argv=None) -> int:
    # Консоль Windows бывает cp1251 — не падаем на кириллице/«→» в выводе.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="deep_research.orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("run_dir"); p.add_argument("--spec", default="")
    p.add_argument("--rounds", type=int, default=None); p.add_argument("--concurrency", type=int, default=None)
    p = sub.add_parser("add-task"); p.add_argument("run_dir")
    p.add_argument("--kind", default="search", choices=["search", "fetch", "topic"])
    p.add_argument("--title", required=True); p.add_argument("--query", default=""); p.add_argument("--url", default="")
    p = sub.add_parser("run-task"); p.add_argument("run_dir"); p.add_argument("task_id")
    p.add_argument("--timeout", type=int, default=600)
    p = sub.add_parser("run-next"); p.add_argument("run_dir"); p.add_argument("--timeout", type=int, default=600)
    p = sub.add_parser("plan"); p.add_argument("run_dir")
    p = sub.add_parser("run-round"); p.add_argument("run_dir")
    p = sub.add_parser("run"); p.add_argument("run_dir")

    args = ap.parse_args(argv)
    store = RunStore(args.run_dir)

    if args.cmd == "init":
        overrides = {}
        if args.rounds is not None:
            overrides["rounds_budget"] = args.rounds
        if args.concurrency is not None:
            overrides["concurrency"] = args.concurrency
        store.init(spec_text=args.spec, config=overrides or None)
        print(f"init: {store.dir}")
        return 0

    if args.cmd == "add-task":
        task = {"kind": args.kind, "title": args.title}
        if args.query:
            task["query"] = args.query
        if args.url:
            task["url"] = args.url
        t = store.add_task(task)
        print(f"add-task: {t['id']} ({t['kind']}) {t['title']}")
        return 0

    if args.cmd in ("run-task", "run-next"):
        if args.cmd == "run-next":
            t = store.next_ready()
            if t is None:
                print("run-next: нет ready-заданий")
                return 1
            task_id = t["id"]
        else:
            task_id = args.task_id
        print(f"running worker for {task_id} ...")
        finding = run_worker(store, task_id, timeout=args.timeout)
        print(f"done: {finding}")
        return 0

    if args.cmd == "plan":
        added = slice_spec(store)
        print(f"plan: {len(added)} заданий")
        for t in added:
            print(f"  {t['id']} ({t['kind']}) {t['title']}")
        return 0

    if args.cmd == "run-round":
        res = run_round(store)
        print(f"round {res['round']}: done={res['done']} failed={res['failed']} skipped={res['skipped']}")
        return 0

    if args.cmd == "run":
        res = run(store)
        print(f"run завершён: раундов={res['rounds']} status={res['status']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
