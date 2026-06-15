"""Нарезка Spec → Backlog: один LLM-вызов превращает spec.md в стартовые задания."""
from __future__ import annotations

import json

from .pi_runner import run_pi
from .store import RunStore

PLANNER_PROMPT = """Ты — планировщик исследования. Прочитай ТЗ (Spec) ниже и разбей
его на 3–6 СТАРТОВЫХ заданий для подагентов-исследователей.

Каждое задание — объект:
  - "kind": "search" | "fetch" | "topic"
  - "title": краткое название задания (рус.)
  - "query": поисковый запрос (для kind=search)  — необязательно для fetch
  - "url": конкретный URL (для kind=fetch)         — только если он явно дан

Покрой ключевые подвопросы Spec, не дублируй. Предпочитай kind=search.

Верни ТОЛЬКО JSON-массив объектов. Без преамбулы, без обрамляющих ```.

# Spec
{spec}
"""


def _extract_json_array(text: str) -> list[dict]:
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j == -1 or j < i:
        raise ValueError(f"в выводе планировщика нет JSON-массива:\n{text[:400]}")
    return json.loads(text[i:j + 1])


def slice_spec(store: RunStore, timeout: int = 300) -> list[dict]:
    cfg = store.config()
    prompt = PLANNER_PROMPT.format(spec=store.spec_text()[:4000])
    proc = run_pi(prompt, provider=cfg["worker_provider"], model=cfg["worker_model"],
                  cwd=str(store.dir), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"планировщик: pi exited {proc.returncode}: {(proc.stderr or '')[:400]}")

    items = _extract_json_array(proc.stdout)
    added = []
    for it in items:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        task = {"kind": it.get("kind", "search"), "title": it["title"], "source": "spec"}
        if it.get("query"):
            task["query"] = it["query"]
        if it.get("url"):
            task["url"] = it["url"]
        added.append(store.add_task(task))
    store.log_event("plan", added=len(added))
    return added
