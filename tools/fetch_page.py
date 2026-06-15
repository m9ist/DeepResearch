"""Рендер JS-страницы через headless Chromium (Playwright) → текст в stdout.

Фолбэк для web_fetch на SPA/JS-сайтах, где обычный fetch отдаёт пустой каркас.

Зависимости: pip install playwright ; playwright install chromium

Использование:
    python fetch_page.py <URL>
stdout — видимый текст страницы (body innerText). Ненулевой код + stderr — ошибка.
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: fetch_page.py <URL>", file=sys.stderr)
        sys.exit(2)
    url = sys.argv[1]
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print(f"playwright не установлен: {e}", file=sys.stderr)
        sys.exit(3)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (deep_research worker)")
            try:
                page.goto(url, wait_until="load", timeout=40000)
            except Exception:  # noqa: BLE001 — даже при таймауте возьмём, что отрендерилось
                pass
            page.wait_for_timeout(1500)  # добить клиентский рендер
            text = page.inner_text("body")
            browser.close()
    except Exception as e:  # noqa: BLE001
        print(f"render error: {e}", file=sys.stderr)
        sys.exit(2)

    text = (text or "").strip()
    if not text:
        print("пустой рендер", file=sys.stderr)
        sys.exit(2)
    print(text)


if __name__ == "__main__":
    main()
