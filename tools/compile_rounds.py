"""Собрать сводки раундов Run'а в единый документ.

Операция «оформи раунды исследования N-M единым документом»: склеивает
reports/round-NN.md за диапазон в один файл reports/compiled-<N>-<M>.md
(виден во встроенном просмотрщике как сводка).

Запуск (из корня репо):
    .venv\\Scripts\\python deep_research\\tools\\compile_rounds.py deep_research\\runs\\<id> 2 8
    .venv\\Scripts\\python deep_research\\tools\\compile_rounds.py deep_research\\runs\\<id> 2 8 -o reports\\digest.md
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("frm", type=int)
    ap.add_argument("to", type=int)
    ap.add_argument("-o", "--output", help="относительно папки Run'а; по умолч. reports/compiled-<N>-<M>.md")
    args = ap.parse_args()

    run = Path(args.run_dir)
    reports = run / "reports"
    parts = [f"# Сводный отчёт — раунды {args.frm}–{args.to}\n"]
    used = []
    for n in range(args.frm, args.to + 1):
        p = reports / f"round-{n:02d}.md"
        if not p.exists():
            continue
        used.append(n)
        body = p.read_text(encoding="utf-8").strip()
        parts.append(f"\n\n---\n\n## Раунд {n}\n\n{body}")

    if not used:
        print(f"нет сводок round-{args.frm:02d}..round-{args.to:02d} в {reports}", file=sys.stderr)
        sys.exit(1)

    out_rel = args.output or f"reports/compiled-{args.frm:02d}-{args.to:02d}.md"
    out = run / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts) + "\n", encoding="utf-8")
    print(f"собрано раундов: {used}")
    print(f"OUTPUT: {out}")


if __name__ == "__main__":
    main()
