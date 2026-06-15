"""Пересканировать находки Run'а и закинуть ПРОВАЛИВШИЕСЯ источники обратно в бэклог.

Операция «давай фейл-задания закинем в бэклог»: ищет в findings/*.md пометки о
неудаче (JS-рендер, недоступно, 403, транскрипт заблокирован, пустой каркас),
вытаскивает связанные URL и добавляет их как свежие `ready` fetch-задания —
чтобы перефетчить уже с JS-рендером / YouTube-транскриптом.

Запуск (из корня репо):
    .venv\\Scripts\\python deep_research\\tools\\rescan_failed.py deep_research\\runs\\<id>
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень репо → import orchestrator
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from orchestrator.store import RunStore  # noqa: E402

MARK = re.compile(
    r"(js[- ]?ренд|недоступ|не удалось|не доступн|транскрибац\w*\s+не|"
    r"пуст\w*\s+(?:скелет|страниц|каркас)|blocked|\b403\b|client-side|skeleton)", re.I)
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BARE = re.compile(r"https?://[^\s)\]]+")
DOMAIN = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:com|org|net|io|dev|ai|space|tech|co|ru|me|app|engineer))\b", re.I)


def norm(u: str) -> str:
    return u.strip().lower().rstrip("/")


def valid(u: str) -> bool:
    if "youtube.com/watch?v=" in u and re.search(r"v=[A-Za-z0-9_-]{11}", u) is None:
        return False
    return len(u) > 12


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: rescan_failed.py <run_dir>", file=sys.stderr)
        sys.exit(2)
    store = RunStore(sys.argv[1])
    findings_dir = store.dir / "findings"
    cands: dict[str, tuple[str, str]] = {}  # norm-url -> (title, url)

    def add(url: str, title: str) -> None:
        if valid(url):
            cands.setdefault(norm(url), (title[:90], url))

    for f in sorted(findings_dir.glob("*.md")):
        txt = f.read_text(encoding="utf-8")
        if not MARK.search(txt):
            continue
        m = re.search(r'^source_url:\s*"([^"]+)"', txt, re.M)
        if m and m.group(1).startswith("http"):
            add(m.group(1), f"rescan {f.stem}")
        domains = set()
        for ln in txt.splitlines():
            if not MARK.search(ln):
                continue
            for t, u in LINK.findall(ln):
                add(u, t)
            for u in BARE.findall(ln):
                add(u, u)
            domains |= {d.lower() for d in DOMAIN.findall(ln)}
        for t, u in LINK.findall(txt):  # развернуть: все ссылки находки на «провальные» домены
            if domains and any(dom in urlparse(u).netloc.lower() for dom in domains):
                add(u, t)

    # идемпотентность: пропускаем URL уже в очереди (ready/in_progress) или уже rescan'нутые
    skip = {norm(t["url"]) for t in store.backlog()
            if t.get("url") and (t.get("status") in ("ready", "in_progress")
                                 or str(t.get("source", "")).startswith("rescan"))}
    added = 0
    for n, (title, url) in cands.items():
        if n in skip:
            continue
        t = store.add_task({"kind": "fetch", "title": title or url, "url": url, "source": "rescan-failed"})
        print(f"  + {t['id']}  {url}")
        added += 1
    store.log_event("rescan_requeue", added=added)
    print(f"добавлено ready-заданий: {added}")


if __name__ == "__main__":
    main()
