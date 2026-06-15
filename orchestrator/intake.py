"""Intake — старт исследования: пинг агента, короткое имя, создание Run'а и
генерация .cmd-лаунчеров pi/claude для интерактивного интервью.

Интервью идёт в отдельном окне терминала (pi/claude — TUI, в браузер не встроить).
Сессия pi кладётся в `<run>/sessions` через `--session-dir`; продолжение —
`--continue` с тем же флагом. Для claude сессии в ~/.claude (подхват best-effort).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .pi_runner import run_pi
from .store import RunStore

ROOT = Path(__file__).resolve().parent.parent          # .../deep_research
RUNS = ROOT / "runs"
INTAKE_INSTRUCTIONS = ROOT / "prompts" / "intake_instructions.md"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "run"


# ---- проверка коннекта к агенту ----
def ping_agent(agent: str, provider: str = "ollama", model: str = "qwen/qwen3.6-35b-a3b") -> tuple[bool, str]:
    try:
        if agent == "claude":
            if not shutil.which("claude"):
                return False, "claude не найден в PATH"
            r = subprocess.run([shutil.which("claude"), "--version"], capture_output=True, text=True, timeout=30)
            return (r.returncode == 0), (r.stdout or r.stderr or "").strip()[:120]
        proc = run_pi("ping. reply ok.", provider=provider, model=model, cwd=str(ROOT), timeout=60)
        return (proc.returncode == 0), (proc.stdout or proc.stderr or "").strip()[:120]
    except subprocess.TimeoutExpired:
        return False, "таймаут — агент не ответил"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


# ---- короткое имя для запуска ----
def suggest_name(idea: str, provider: str = "ollama", model: str = "qwen/qwen3.6-35b-a3b") -> str:
    prompt = (
        "Придумай короткое имя-слаг для исследования (англ., kebab-case, 2-4 слова, "
        "только латиница/цифры/дефис). Ответь ТОЛЬКО слагом.\n\nИдея: " + idea[:500]
    )
    proc = run_pi(prompt, provider=provider, model=model, cwd=str(ROOT), timeout=60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "pi error")[:200])
    last = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return slugify(last[-1]) if last else "run"


# ---- генерация лаунчеров ----
def _write_cmd(path: Path, lines: list[str]) -> None:
    path.write_text("@echo off\r\n" + "\r\n".join(lines) + "\r\n", encoding="utf-8")


def _build_launchers(run_dir: Path, agent: str, slug: str, provider: str, model: str) -> None:
    rd = str(run_dir)
    brief = str(run_dir / "intake_brief.md")
    sess = str(run_dir / "sessions")
    if agent == "claude":
        intake = [f'cd /d "{rd}"',
                  'claude "Прочитай файл intake_brief.md в этой папке и проведи по нему '
                  'интервью со мной (по одному вопросу), затем создай spec.md."']
        cont = [f'cd /d "{rd}"', "claude --continue"]
    else:
        intake = [f'cd /d "{rd}"',
                  f'pi @"{brief}" --session-dir "{sess}" --provider {provider} --model "{model}" --name "{slug}"']
        cont = [f'cd /d "{rd}"', f'pi --continue --session-dir "{sess}"']
    _write_cmd(run_dir / "intake.cmd", intake)
    _write_cmd(run_dir / "continue.cmd", cont)


REFINE_BRIEF_TEMPLATE = """# Доуточнение ТЗ исследования

Это НЕ новое исследование, а уточнение УЖЕ идущего. Ниже — исходная идея,
текущий ТЗ и список незавершённых заданий бэклога.

## Как действуй
1. Спроси меня (по одному вопросу за раз), что мне нравится / не нравится в
   текущем направлении и на чём сфокусироваться.
2. По итогам обсуждения ПЕРЕЗАПИШИ файл `spec.md` — новую, уточнённую версию ТЗ.
3. Сформируй файл `refine_decisions.json` СТРОГО в таком формате:

   {{"drop": ["t0009"], "keep": ["t0007"],
     "add": [{{"kind": "search", "title": "...", "query": "поисковый запрос"}},
             {{"kind": "fetch", "title": "...", "url": "https://..."}}],
     "reason": {{"t0009": "почему убрали"}}}}

   - `drop`/`keep` — id ТОЛЬКО из списка незавершённых заданий ниже (что убрать / оставить).
   - `add` — НОВЫЕ задания под уточнённый фокус: `search` с `query` (поисковый запрос)
     либо `fetch` с `url`. ОБЯЗАТЕЛЬНО предложи новые задания, если уточнения открыли
     новые вопросы/направления — иначе исследование не сдвинется. Если новых не нужно — `add: []`.
4. НИЧЕГО не запускай (никаких раундов/поиска) — только обнови `spec.md` и запиши
   `refine_decisions.json`. В конце скажи мне нажать в веб-морде «подхватить решения».

## Исходная идея
{idea}

## Текущий ТЗ (spec.md)
{spec}

## Незавершённые задания (ready) — пометь актуальность
{tasks}
"""


def build_refine(run_dir: Path, agent: str = "pi",
                 provider: str = "ollama", model: str = "qwen/qwen3.6-35b-a3b") -> int:
    """Сгенерировать бриф уточнения (идея + текущий ТЗ + незавершённые задания) и
    .cmd-лаунчеры (своя сессия sessions-refine). Возвращает число ready-заданий в брифе."""
    store = RunStore(run_dir)
    idea = (run_dir / "idea.txt").read_text(encoding="utf-8").strip() if (run_dir / "idea.txt").exists() else "(idea.txt нет)"
    spec = store.spec_text().strip() or "(spec.md пуст)"
    ready = [t for t in store.backlog() if t.get("status") == "ready"]
    rows = [f"- {t['id']} [{t.get('kind', '')}] {t.get('title', '')}"
            + (f" — {t.get('query') or t.get('url')}" if (t.get('query') or t.get('url')) else "")
            for t in ready]
    tasks = "\n".join(rows) or "(незавершённых заданий нет)"
    (run_dir / "refine_brief.md").write_text(
        REFINE_BRIEF_TEMPLATE.format(idea=idea, spec=spec, tasks=tasks), encoding="utf-8")
    (run_dir / "sessions-refine").mkdir(exist_ok=True)
    rd, brief, sess = str(run_dir), str(run_dir / "refine_brief.md"), str(run_dir / "sessions-refine")
    if agent == "claude":
        start = [f'cd /d "{rd}"',
                 'claude "Прочитай файл refine_brief.md в этой папке и действуй строго по нему '
                 '(обнови spec.md и запиши refine_decisions.json)."']
    else:
        start = [f'cd /d "{rd}"',
                 f'pi @"{brief}" --session-dir "{sess}" --provider {provider} --model "{model}" --name "{run_dir.name}-refine"']
    _write_cmd(run_dir / "refine.cmd", start)
    store.log_event("refine_built", agent=agent, ready=len(ready))
    return len(ready)


def create_intake_run(idea: str, name: str, agent: str = "pi",
                      provider: str = "ollama", model: str = "qwen/qwen3.6-35b-a3b") -> str:
    slug = slugify(name)
    run_name = f"{datetime.now().strftime('%Y-%m-%d')}-{slug}"
    run_dir = RUNS / run_name
    store = RunStore(run_dir)
    store.init(spec_text="", config={"worker_provider": provider, "worker_model": model, "intake_agent": agent})
    (run_dir / "sessions").mkdir(exist_ok=True)

    # стартовый state и идея
    st = store.state(); st["status"] = "intake"; store.save_state(st)
    (run_dir / "idea.txt").write_text(idea, encoding="utf-8")

    # бриф интервью = инструкции + идея (доставляется агенту как @file / чтением)
    instructions = INTAKE_INSTRUCTIONS.read_text(encoding="utf-8") if INTAKE_INSTRUCTIONS.exists() else ""
    (run_dir / "intake_brief.md").write_text(
        instructions + f"\n\n---\n\n## Изначальная идея пользователя\n\n{idea}\n", encoding="utf-8")

    _build_launchers(run_dir, agent, slug, provider, model)
    store.log_event("intake_created", agent=agent, slug=slug)
    return run_name
