"""Checkpoint: конец Round'а — собрать ссылки «ещё почитать» в Backlog,
синтезировать сводку (Report) и решить, продолжать ли.
"""
from __future__ import annotations

import re
import time

from .pi_runner import run_pi
from .store import RunStore

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
MAX_HARVEST = 6  # костыльный потолок новых заданий за чекпоинт (без молчаливого среза)

REPORT_PROMPT = """Ты пишешь промежуточную сводку (Report) по раунду исследования.
Раунд {round} из {rounds_budget}. В бэклоге осталось ready-заданий: {ready}.

Ниже — находки этого раунда. Сделай сводку строго по формату markdown:

# Сводка — Round {round}

## Что нашли за раунд
<ключевое со ссылками на источники>

## Перспективные идеи / зацепки
<что ценно, куда копать>

## Состояние
- Находок за раунд: {n_findings}
- В бэклоге ready: {ready}
- Раунд {round} из {rounds_budget}

## План на следующий раунд
<какие задания приоритетны>

Когда ссылаешься на конкретную находку — давай КЛИКАБЕЛЬНУЮ ОТНОСИТЕЛЬНУЮ ссылку
на её summary СТРОГО в формате [finding 0011](../findings/0011.md) (id — из
«### NNNN» ниже; именно с «../», т.к. сводка лежит в reports/, а находки в
findings/). НЕ давай абсолютных путей. Плюс внешние ссылки [title](url), как обычно.

Верни ТОЛЬКО этот markdown. Без преамбулы и без обрамляющих ```.

# Находки раунда
{findings}
"""


def _norm(u: str) -> str:
    return u.strip().lower().split("#")[0].rstrip("/")


def _section(body: str, header: str) -> str:
    out, on = [], False
    for ln in body.splitlines():
        if ln.strip().startswith("## "):
            on = header.lower() in ln.lower()
            continue
        if on:
            out.append(ln)
    return "\n".join(out)


def harvest_links(store: RunStore, round_no: int) -> list[dict]:
    """Из секций «Ещё почитать» находок раунда — новые fetch-задания (с дедупом)."""
    seen = {_norm(t["url"]) for t in store.backlog() if t.get("url")}
    cands: list[tuple[str, str, str]] = []
    for f in store.findings():
        if f["fm"].get("round") != round_no:
            continue
        for title, url in LINK_RE.findall(_section(f["body"], "Ещё почитать")):
            n = _norm(url)
            if n in seen:
                continue
            seen.add(n)
            cands.append((title, url, f["id"]))

    added = []
    for title, url, fid in cands[:MAX_HARVEST]:
        added.append(store.add_task({"kind": "fetch", "title": title[:120], "url": url,
                                     "source": f"finding:{fid}"}))
    store.log_event("harvest", round=round_no, added=len(added), dropped=max(0, len(cands) - MAX_HARVEST))
    return added


def synth_report(store: RunStore, round_no: int) -> str:
    cfg = store.config()
    fs = [f for f in store.findings() if f["fm"].get("round") == round_no]
    ready = len([t for t in store.backlog() if t.get("status") == "ready"])
    ctx = "\n\n".join(f"### {f['id']} ({f['fm'].get('kind', '')})\n{f['body'][:1500]}" for f in fs)
    prompt = REPORT_PROMPT.format(round=round_no, rounds_budget=store.state().get("rounds_budget"),
                                  ready=ready, n_findings=len(fs), findings=ctx[:9000])
    # Ретраим: синтез часто падает из-за временной недоступности LLM. На стойком отказе
    # НЕ пишем фейковую сводку (она засчитала бы раунд пройденным) — бросаем исключение:
    # раунд останется незавершённым, run() встанет на паузу, на resume синтез повторится.
    last = ""
    for attempt in range(3):
        proc = run_pi(prompt, provider=cfg["worker_provider"], model=cfg["worker_model"],
                      cwd=str(store.dir), timeout=300)
        text = _clean(proc.stdout)
        if proc.returncode == 0 and text:
            return store.write_report(round_no, _fix_finding_links(text))
        last = f"rc={proc.returncode}, {(proc.stderr or '').strip()[:200] or 'пустой ответ'}"
        store.log_event("synth_retry", round=round_no, attempt=attempt + 1, error=last)
        if attempt < 2:
            time.sleep(3)
    raise RuntimeError(f"синтез сводки Round {round_no} не удался после 3 попыток: {last}")


def _fix_finding_links(text: str) -> str:
    """Сводка лежит в reports/, находки — в findings/ → ссылка всегда ../findings/.
    Схлопывает любой префикс (findings/, ../findings/, ../../findings/) к ../findings/."""
    return re.sub(r"\]\((?:\.\./)*findings/", "](../findings/", text or "")


def _clean(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def interim_report(store: RunStore) -> str:
    """Внеплановая сводка по требованию («Отчёт сейчас») — по ВСЕМ находкам."""
    cfg = store.config()
    fs = store.findings()
    ready = len([t for t in store.backlog() if t.get("status") == "ready"])
    rnd = store.state().get("round")
    ctx = "\n\n".join(f"### {f['id']} ({f['fm'].get('kind', '')})\n{f['body'][:1200]}" for f in fs)
    prompt = REPORT_PROMPT.format(round=f"{rnd} (промежуточный)", rounds_budget=store.state().get("rounds_budget"),
                                  ready=ready, n_findings=len(fs), findings=ctx[:9000])
    store.log_event("interim_report_start", findings=len(fs))
    proc = run_pi(prompt, provider=cfg["worker_provider"], model=cfg["worker_model"],
                  cwd=str(store.dir), timeout=300)
    text = _clean(proc.stdout)
    if proc.returncode != 0 or not text:
        text = f"# Промежуточная сводка\n\n(синтез не удался; находок: {len(fs)})"
    seq = len(list(store.reports_dir.glob("interim-*.md"))) + 1
    rel = store.write_report_named(f"interim-{seq:02d}", _fix_finding_links(text))
    store.log_event("interim_report", report=rel)
    return rel


def checkpoint(store: RunStore, round_no: int) -> bool:
    """Собрать ссылки, написать сводку, решить — продолжать ли. Возвращает решение."""
    store.save_state({**store.state(), "phase": "synth"})  # сигнал UI: идёт синтез сводки раунда
    try:
        harvested = harvest_links(store, round_no)
        report_rel = synth_report(store, round_no)
        ready = [t for t in store.backlog() if t.get("status") == "ready"]
        cont = round_no < int(store.state().get("rounds_budget", 1)) and len(ready) > 0
        store.log_event("checkpoint", round=round_no, report=report_rel,
                        harvested=len(harvested), backlog_ready=len(ready), will_continue=cont)
        return cont
    finally:
        store.save_state({**store.state(), "phase": None})  # снять метку (в т.ч. при провале синтеза)
