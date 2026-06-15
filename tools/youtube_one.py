"""Скачивает транскрипцию YouTube-видео и сохраняет с тайм-кодами.

Адаптировано из E:\\SecondBrain\\programs\\youtube_one.py под deep_research:
добавлен `--outdir` (транскрипт пишется в <outdir>/yt-<videoId>/transcript.txt),
чтобы расширение (web_fetch) клало сырьё прямо в sources/ папки Run'а.

Зависимость: youtube_transcript_api  (pip install youtube_transcript_api).

Использование:
    python youtube_one.py <URL> --outdir <dir>     # <dir>/yt-<id>/transcript.txt
    python youtube_one.py <URL> -o <path.txt>       # явный файл
    python youtube_one.py <URL> --lang ru,en        # порядок языков

stdout (машиночитаемо):
    URL: <URL>
    VIDEO_ID: <id>
    TITLE: <title>
    OUTPUT: <abs path>
    OK: <N сегментов>
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # иначе кириллица ошибки в логе воркера — кракозябры
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Глобальный (меж-процессный) троттл YT-запросов: YouTube банит IP за «слишком много
# запросов». Воркеры — отдельные процессы, координируемся через файл-маркер времени.
YT_MIN_INTERVAL = float(os.environ.get("DR_YT_MIN_INTERVAL", "1.5"))  # сек между транскрипт-запросами
THROTTLE_FILE = Path(os.environ.get("DR_YT_THROTTLE_FILE",
                     str(Path(tempfile.gettempdir()) / "dr_yt_throttle")))


def throttle(min_interval: float, marker: Path) -> None:
    """Не чаще одного запроса транскрипта раз в min_interval сек по всем воркерам.
    Сериализуемся lock-файлом, выдерживаем интервал относительно метки прошлого запроса."""
    if min_interval <= 0:
        return
    lock = marker.with_suffix(".lock")
    acquired = False
    for _ in range(600):  # ждём лок до ~60с, потом всё равно идём
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.1)
    try:
        try:
            last = float(marker.read_text())
        except Exception:  # noqa: BLE001 — нет метки/битая → считаем «давно»
            last = 0.0
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            marker.write_text(str(time.time()))
        except Exception:  # noqa: BLE001 — метка не критична
            pass
    finally:
        if acquired:
            try:
                os.remove(lock)
            except OSError:
                pass

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?[^#]*v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/|youtube\.com/live/)([A-Za-z0-9_-]{11})"
)


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
    except Exception:
        return extract_video_id(url)


def fetch_transcript(video_id: str, languages):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    return api.fetch(video_id, languages=languages)


def ms_to_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("-o", "--output", help="Путь к выходному файлу")
    ap.add_argument("--outdir", help="Папка: транскрипт ляжет в <outdir>/yt-<id>/transcript.txt")
    ap.add_argument("--lang", default="ru,en", help="Порядок предпочтения языков, через запятую")
    args = ap.parse_args()

    video_id = extract_video_id(args.url)
    languages = [s.strip() for s in args.lang.split(",") if s.strip()]
    title = fetch_title(args.url)

    throttle(YT_MIN_INTERVAL, THROTTLE_FILE)  # выдержать интервал по всем воркерам перед запросом
    try:
        transcript = fetch_transcript(video_id, languages)
    except Exception as e:
        print(f"Ошибка загрузки транскрипта: {e}", file=sys.stderr)
        sys.exit(2)

    if args.output:
        out_path = Path(args.output).resolve()
    elif args.outdir:
        out_path = (Path(args.outdir) / f"yt-{video_id}" / "transcript.txt").resolve()
    else:
        out_path = (Path(__file__).resolve().parent / "tmp" / f"yt-{video_id}" / "transcript.txt").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [args.url, title, ""]
    n = 0
    for s in transcript.snippets:
        text = s.text.strip()
        if not text:
            continue
        lines.append(f"[{ms_to_timestamp(s.start)}] {text}")
        n += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"URL: {args.url}")
    print(f"VIDEO_ID: {video_id}")
    print(f"TITLE: {title}")
    print(f"OUTPUT: {out_path}")
    print(f"OK: {n} сегментов")


if __name__ == "__main__":
    main()
