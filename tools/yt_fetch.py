"""Пайплайн загрузки YouTube-транскриптов: кэш → лестница провайдеров с fail-over.

Архитектура — см. docs/adr/0003-transcript-provider-cascade.md:
  1. Кэш по videoId (общий для всех Run'ов).
  2. Лестница Transcript Provider'ов из yt_providers.json (порядок = приоритет),
     fail-over без блокировки: исчерпавший ограничение (кулдаун/квота/бан) узел
     молча пропускается.
  3. Лок (tools/tmp/yt_state.lock) — только на короткую критсекцию «решить+записать
     состояние»; сетевые вызовы вне лока.
  4. Все взаимодействия пишутся в yt_runs.jsonl.

Точка входа: run_fetch(url, video_id, languages, out_path) -> FetchResult | None.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
TMP_DIR = TOOLS_DIR / "tmp"

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?[^#]*v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/|youtube\.com/live/)([A-Za-z0-9_-]{11})"
)

# Дефолтная лестница, если yt_providers.json отсутствует (из коробки не падаем).
DEFAULT_PROVIDERS = [
    {
        "type": "youtube_transcript_api",
        "enabled": True,
        "min_interval_sec": 300,
        "ban_backoff": {"factor": 2, "max_interval_sec": 86400},
        "languages": ["ru", "en"],
    }
]


@dataclass
class Paths:
    providers: Path
    secrets: Path
    state: Path
    lock: Path
    cache: Path
    log: Path


@dataclass
class FetchResult:
    out_path: Path
    title: str
    segments: int
    resolved_by: str  # type провайдера или "cache"


class ProviderBan(Exception):
    """IP-блок/бан — растим ban_level."""


class ProviderMiss(Exception):
    """Окончательный промах/транзиент этого провайдера — без роста бэкоффа."""


def _paths() -> Paths:
    return Paths(
        providers=Path(os.environ.get("DR_YT_PROVIDERS", TOOLS_DIR / "yt_providers.json")),
        secrets=Path(os.environ.get("DR_YT_SECRETS", TOOLS_DIR / "yt_secrets.json")),
        state=Path(os.environ.get("DR_YT_STATE", TMP_DIR / "yt_state.json")),
        lock=TMP_DIR / "yt_state.lock",
        cache=Path(os.environ.get("DR_YT_CACHE_DIR", TMP_DIR / ".yt_cache")),
        log=Path(os.environ.get("DR_YT_LOG", TMP_DIR / "yt_runs.jsonl")),
    )


# --- утилиты ---------------------------------------------------------------

def extract_video_id(url: str) -> str:
    m = VIDEO_ID_RE.search(url)
    if not m:
        raise ValueError(f"Не удалось извлечь videoId из URL: {url}")
    return m.group(1)


def fetch_title(url: str) -> str:
    try:
        oembed = "https://www.youtube.com/oembed?url=" + urllib.parse.quote(url, safe="") + "&format=json"
        with urllib.request.urlopen(oembed, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("title") or extract_video_id(url)
    except Exception:  # noqa: BLE001 — заголовок не критичен, фолбэк на videoId
        return extract_video_id(url)


def ms_to_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception:  # noqa: BLE001 — битый файл → дефолт (состояние не критично)
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def _lock(lockpath: Path, timeout: float = 60.0):
    """Меж-процессный лок через O_CREAT|O_EXCL. По таймауту идём всё равно —
    лок не должен вечно блокировать (как в прежнем троттле)."""
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    deadline = time.time() + timeout
    while True:
        try:
            os.close(os.open(str(lockpath), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            acquired = True
            break
        except FileExistsError:
            if time.time() > deadline:
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            try:
                os.remove(lockpath)
            except OSError:
                pass


def _log(log_path: Path, **fields) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — лог не должен ронять загрузку
        pass


# --- состояние под локом ---------------------------------------------------

def _month_str(now: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(now))


def _check_constraints(pconf: dict, ps: dict, now: float):
    """Под локом. Возвращает причину скипа ('skip_cooldown'/'skip_quota') или None.
    Может прокрутить месяц в ps (мутация — сохранится вызывающим)."""
    if "min_interval_sec" in pconf:
        bb = pconf.get("ban_backoff", {})
        factor = bb.get("factor", 2)
        cap = bb.get("max_interval_sec", 86400)
        eff = min(pconf["min_interval_sec"] * (factor ** ps.get("ban_level", 0)), cap)
        if now - ps.get("last_request_ts", 0.0) < eff:
            return "skip_cooldown"
    if "monthly_quota" in pconf:
        month = _month_str(now)
        if ps.get("month") != month:
            ps["month"] = month
            ps["used_this_month"] = 0
        if ps.get("used_this_month", 0) >= pconf["monthly_quota"]:
            return "skip_quota"
    return None


def _try_claim(paths: Paths, ptype: str, pconf: dict, now: float):
    """Под локом: проверить ограничения и, если годен, захватить слот
    (стемп таймстемпа / инкремент квоты). Возвращает (ok, reason)."""
    with _lock(paths.lock):
        state = _load_json(paths.state, {})
        ps = state.setdefault(ptype, {})
        reason = _check_constraints(pconf, ps, now)
        if reason:
            _save_json(paths.state, state)  # зафиксировать прокрутку месяца, если была
            return False, reason
        if "min_interval_sec" in pconf:
            ps["last_request_ts"] = now
        if "monthly_quota" in pconf:
            ps["used_this_month"] = ps.get("used_this_month", 0) + 1
        _save_json(paths.state, state)
    return True, None


def _set_ban(paths: Paths, ptype: str, outcome: str) -> int:
    """Под локом: success → ban_level=0, ban → +1. Возвращает ban_level после."""
    with _lock(paths.lock):
        state = _load_json(paths.state, {})
        ps = state.setdefault(ptype, {})
        if outcome == "success":
            ps["ban_level"] = 0
        elif outcome == "ban":
            ps["ban_level"] = ps.get("ban_level", 0) + 1
        level = ps.get("ban_level", 0)
        _save_json(paths.state, state)
    return level


# --- кэш -------------------------------------------------------------------

def _cache_get(cache_dir: Path, video_id: str):
    p = cache_dir / f"yt-{video_id}" / "transcript.txt"
    if p.exists():
        return p, _load_json(p.parent / "meta.json", {})
    return None, None


def _cache_put(cache_dir: Path, video_id: str, text: str, meta: dict) -> None:
    d = cache_dir / f"yt-{video_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.txt").write_text(text, encoding="utf-8")
    _save_json(d / "meta.json", meta)


def format_transcript(url: str, title: str, segments):
    """segments: iterable из (start_seconds, text). Возвращает (text, n_сегментов)."""
    lines = [url, title, ""]
    n = 0
    for start, text in segments:
        text = (text or "").strip()
        if not text:
            continue
        lines.append(f"[{ms_to_timestamp(start)}] {text}")
        n += 1
    return "\n".join(lines) + "\n", n


# --- провайдеры ------------------------------------------------------------

def _provider_ytapi(url, video_id, languages, pconf, secret):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as e

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=languages)
    except (e.IpBlocked, e.RequestBlocked, e.PoTokenRequired) as ex:
        raise ProviderBan(type(ex).__name__) from ex
    except Exception as ex:  # noqa: BLE001 — нет субтитров/видео недоступно/транзиент = промах
        raise ProviderMiss(type(ex).__name__) from ex
    segs = [(s.start, s.text) for s in fetched.snippets]
    return segs, None  # заголовок — фолбэком через oembed


def _provider_apify(url, video_id, languages, pconf, secret):
    token = secret.get("token") or os.environ.get("DR_APIFY_TOKEN")
    actor = pconf.get("actor", "supreme_coder~youtube-transcript-scraper")
    timeout = pconf.get("timeout_sec", 280)
    body = json.dumps({"urls": [{"url": url}], "outputFormat": "json", "languages": languages}).encode("utf-8")
    endpoint = (
        f"https://api.apify.com/v2/acts/{urllib.parse.quote(actor, safe='~')}"
        f"/run-sync-get-dataset-items?token={urllib.parse.quote(token)}"
    )
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            items = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:  # 4xx/5xx (вкл. 429) — промах, квота считается отдельно
        raise ProviderMiss(f"HTTP {ex.code}") from ex
    except Exception as ex:  # noqa: BLE001 — таймаут/сеть = промах
        raise ProviderMiss(type(ex).__name__) from ex
    if not items:
        raise ProviderMiss("empty")
    item = items[0]
    tr = item.get("transcript") or []
    if not tr:
        raise ProviderMiss("no_transcript")
    import html
    segs = [(seg.get("start", 0), html.unescape(seg.get("text", ""))) for seg in tr]
    title = (item.get("videoDetails") or {}).get("title")
    if title:
        title = html.unescape(title)
    return segs, title


PROVIDERS = {
    "youtube_transcript_api": {"fetch": _provider_ytapi, "needs_secret": False},
    "apify_supreme_coder": {"fetch": _provider_apify, "needs_secret": True},
}


def _has_token(secret: dict) -> bool:
    return bool(secret.get("token") or os.environ.get("DR_APIFY_TOKEN"))


def _load_providers(paths: Paths):
    cfg = _load_json(paths.providers, None)
    if not isinstance(cfg, list) or not cfg:
        return DEFAULT_PROVIDERS
    return cfg


# --- оркестрация -----------------------------------------------------------

def run_fetch(url: str, video_id: str, languages, out_path: Path):
    """Кэш → лестница. Пишет транскрипт в out_path (и в кэш на успехе/из кэша на хите).
    Возвращает FetchResult или None, если вся лестница исчерпана."""
    paths = _paths()
    t_total = time.time()
    out_path = Path(out_path)

    # 1. Кэш
    cpath, meta = _cache_get(paths.cache, video_id)
    if cpath is not None:
        text = cpath.read_text(encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        title = (meta or {}).get("title") or video_id
        n = (meta or {}).get("segments", 0)
        _log(paths.log, ts=now_iso(), video_id=video_id, provider="cache",
             outcome="cache_hit", segments=n, bytes=len(text))
        _log(paths.log, ts=now_iso(), video_id=video_id, resolved_by="cache",
             total_dur_ms=int((time.time() - t_total) * 1000))
        return FetchResult(out_path, title, n, "cache")

    # 2. Лестница
    providers = _load_providers(paths)
    secrets = _load_json(paths.secrets, {})
    for pconf in providers:
        ptype = pconf.get("type")
        if not pconf.get("enabled", True):
            continue
        impl = PROVIDERS.get(ptype)
        if impl is None:
            _log(paths.log, ts=now_iso(), video_id=video_id, provider=ptype, outcome="skip_unknown")
            continue
        secret = secrets.get(ptype, {}) if isinstance(secrets, dict) else {}
        if impl["needs_secret"] and not _has_token(secret):
            _log(paths.log, ts=now_iso(), video_id=video_id, provider=ptype, outcome="skip_no_secret")
            continue

        ok, reason = _try_claim(paths, ptype, pconf, time.time())
        if not ok:
            _log(paths.log, ts=now_iso(), video_id=video_id, provider=ptype, outcome=reason)
            continue

        langs = pconf.get("languages") or languages
        ts_start = now_iso()
        t0 = time.time()
        try:
            segments, title = impl["fetch"](url, video_id, langs, pconf, secret)
        except ProviderBan as ex:
            level = _set_ban(paths, ptype, "ban")
            _log(paths.log, ts_start=ts_start, ts_end=now_iso(), dur_ms=int((time.time() - t0) * 1000),
                 video_id=video_id, provider=ptype, outcome="ban",
                 error_class=str(ex), ban_level_after=level)
            continue
        except ProviderMiss as ex:
            _log(paths.log, ts_start=ts_start, ts_end=now_iso(), dur_ms=int((time.time() - t0) * 1000),
                 video_id=video_id, provider=ptype, outcome="miss", error_class=str(ex))
            continue
        except Exception as ex:  # noqa: BLE001 — неожиданное: логируем как транзиент, идём дальше
            _log(paths.log, ts_start=ts_start, ts_end=now_iso(), dur_ms=int((time.time() - t0) * 1000),
                 video_id=video_id, provider=ptype, outcome="transient",
                 error_class=type(ex).__name__, error_msg=str(ex))
            continue

        _set_ban(paths, ptype, "success")
        title = title or fetch_title(url)
        text, n = format_transcript(url, title, segments)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        _cache_put(paths.cache, video_id, text, {
            "video_id": video_id, "title": title, "lang": langs,
            "provider": ptype, "fetched_at": now_iso(), "segments": n,
        })
        _log(paths.log, ts_start=ts_start, ts_end=now_iso(), dur_ms=int((time.time() - t0) * 1000),
             video_id=video_id, provider=ptype, outcome="success", segments=n, bytes=len(text))
        _log(paths.log, ts=now_iso(), video_id=video_id, resolved_by=ptype,
             total_dur_ms=int((time.time() - t_total) * 1000))
        return FetchResult(out_path, title, n, ptype)

    _log(paths.log, ts=now_iso(), video_id=video_id, resolved_by=None,
         total_dur_ms=int((time.time() - t_total) * 1000))
    return None
