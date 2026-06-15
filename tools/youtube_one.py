"""Скачивает транскрипцию YouTube-видео и сохраняет с тайм-кодами.

Тонкий CLI поверх пайплайна yt_fetch (кэш → лестница провайдеров с fail-over,
см. docs/adr/0003-transcript-provider-cascade.md). Интерфейс и stdout-контракт
сохранены для расширения (extension/index.ts).

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
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # иначе кириллица ошибки в логе воркера — кракозябры
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))  # для import yt_fetch
import yt_fetch  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("-o", "--output", help="Путь к выходному файлу")
    ap.add_argument("--outdir", help="Папка: транскрипт ляжет в <outdir>/yt-<id>/transcript.txt")
    ap.add_argument("--lang", default="ru,en", help="Порядок предпочтения языков, через запятую")
    args = ap.parse_args()

    video_id = yt_fetch.extract_video_id(args.url)
    languages = [s.strip() for s in args.lang.split(",") if s.strip()]

    if args.output:
        out_path = Path(args.output).resolve()
    elif args.outdir:
        out_path = (Path(args.outdir) / f"yt-{video_id}" / "transcript.txt").resolve()
    else:
        out_path = (Path(__file__).resolve().parent / "tmp" / f"yt-{video_id}" / "transcript.txt").resolve()

    result = yt_fetch.run_fetch(args.url, video_id, languages, out_path)
    if result is None:
        print("Ошибка загрузки транскрипта: все провайдеры исчерпаны", file=sys.stderr)
        sys.exit(2)

    print(f"URL: {args.url}")
    print(f"VIDEO_ID: {video_id}")
    print(f"TITLE: {result.title}")
    print(f"OUTPUT: {result.out_path}")
    print(f"OK: {result.segments} сегментов")


if __name__ == "__main__":
    main()
