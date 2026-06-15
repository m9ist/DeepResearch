// Deep Research — pi extension: web_search (SearXNG) + web_fetch.
//
// Намеренно без импортов и зависимостей: `parameters` — обычный JSON-Schema
// объект (TypeBox всё равно генерит такой же), `fetch` глобальный (Node 22+).
// pi транспилирует .ts через esbuild и стирает типы, поэтому pi не типизируем.
//
// Конфиг через env (оркестратор выставляет при спавне Worker'а):
//   DR_SEARXNG_URL   — базовый URL SearXNG (по умолчанию http://localhost:8888)
//   DR_FETCH_MAX     — лимит символов web_fetch (по умолчанию 15000)

const SEARXNG_URL = (process.env.DR_SEARXNG_URL || "http://localhost:8888").replace(/\/+$/, "");
const FETCH_MAX = Number(process.env.DR_FETCH_MAX || 15000);
const YT_MAX = Number(process.env.DR_YT_MAX || 80000);  // транскрипт длиннее — у модели ctx 160k
const NET_TIMEOUT = Number(process.env.DR_NET_TIMEOUT_MS || 20000);

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

// Сигнал = сигнал инструмента + сетевой таймаут (иначе зависший сервер держит воркера).
function withTimeout(signal: AbortSignal | undefined): AbortSignal {
  const t = AbortSignal.timeout(NET_TIMEOUT);
  return signal ? AbortSignal.any([signal, t]) : t;
}

/** Грубое HTML→текст: вырезать script/style, снять теги, декодировать базовые сущности. */
function htmlToText(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<\/(p|div|h[1-6]|li|tr|br|section|article)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/[ \t ]+/g, " ")
    .replace(/\n\s*\n\s*\n+/g, "\n\n")
    .trim();
}

// --- YouTube + персист сырья (env выставляет оркестратор при спавне воркера) ---
const RUN_DIR = process.env.DR_RUN_DIR || "";
const YT_SCRIPT = process.env.DR_YT_SCRIPT || "";
const FETCH_SCRIPT = process.env.DR_FETCH_SCRIPT || "";   // fetch_page.py (Playwright)
const JS_MIN = Number(process.env.DR_JS_MIN || 400);       // мало текста → JS-сайт → рендерим
const PYTHON = process.env.DR_PYTHON || "python";
const YT_RE = /(?:youtube\.com\/watch\?[^#]*v=|youtu\.be\/|youtube\.com\/shorts\/|youtube\.com\/embed\/|youtube\.com\/live\/)([A-Za-z0-9_-]{11})/;

function ytId(url: string): string | null {
  const m = YT_RE.exec(url || "");
  return m ? m[1] : null;
}

function hashName(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

async function saveRaw(name: string, content: string): Promise<string | null> {
  if (!RUN_DIR) return null;
  try {
    const fs = await import("node:fs/promises");
    const path = await import("node:path");
    const dir = path.join(RUN_DIR, "sources");
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, name), content, "utf-8");
    return "sources/" + name;
  } catch {
    return null;
  }
}

// Транскрипт через python-скрипт youtube_one.py (youtube_transcript_api).
async function fetchYouTube(url: string, vid: string): Promise<string> {
  const cp = await import("node:child_process");
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const sources = path.join(RUN_DIR, "sources");
  const stdout: string = await new Promise((resolve, reject) => {
    cp.execFile(PYTHON, [YT_SCRIPT, url, "--outdir", sources],
      { timeout: 300000, encoding: "utf-8", maxBuffer: 4 << 20 },  // 300с: холодный старт apify-фолбэка
      (err: any, out: string, errout: string) =>
        err ? reject(new Error((errout || err.message || "").toString())) : resolve(out.toString()));
  });
  const mo = /^OUTPUT:\s*(.+)$/m.exec(stdout);
  const mt = /^TITLE:\s*(.+)$/m.exec(stdout);
  if (!mo) throw new Error("youtube_one.py: нет OUTPUT");
  const body = await fs.readFile(mo[1].trim(), "utf-8");
  const rel = "sources/yt-" + vid + "/transcript.txt";
  const title = mt ? mt[1].trim() : vid;
  const clipped = body.length > YT_MAX
    ? body.slice(0, YT_MAX) + `\n\n[...транскрипт обрезан из ${body.length} символов. Полный текст сохранён в ${rel}. Это лимит вывода инструмента — в вебе продолжения НЕТ, не ищи его поиском.]`
    : body;
  return `Транскрипт YouTube «${title}» (сохранён: ${rel}):\n\n${clipped}`;
}

// Рендер JS-страницы через headless Chromium (fetch_page.py) → текст в stdout.
async function renderPage(url: string): Promise<string> {
  const cp = await import("node:child_process");
  return await new Promise((resolve, reject) => {
    cp.execFile(PYTHON, [FETCH_SCRIPT, url], { timeout: 70000, encoding: "utf-8", maxBuffer: 16 << 20 },
      (err: any, out: string, errout: string) =>
        err ? reject(new Error((errout || err.message || "").toString())) : resolve((out || "").toString()));
  });
}

export default function (pi: any) {
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description:
      "Поиск в вебе через локальный SearXNG. Возвращает список результатов " +
      "(title, url, snippet). Используй для нахождения источников по запросу.",
    promptSnippet: "Найти в вебе источники по запросу.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "Поисковый запрос." },
        k: { type: "number", description: "Сколько результатов вернуть (по умолчанию 8)." },
      },
      required: ["query"],
    },
    async execute(_toolCallId: string, params: { query: string; k?: number }, signal: AbortSignal) {
      const k = params.k && params.k > 0 ? Math.min(params.k, 20) : 8;
      const url = `${SEARXNG_URL}/search?q=${encodeURIComponent(params.query)}&format=json`;
      try {
        const res = await fetch(url, { signal: withTimeout(signal), headers: { Accept: "application/json" } });
        if (!res.ok) return textResult(`web_search: SearXNG вернул HTTP ${res.status} (${SEARXNG_URL}).`);
        const json: any = await res.json();
        const results = Array.isArray(json.results) ? json.results.slice(0, k) : [];
        if (results.length === 0) return textResult(`web_search: ничего не найдено по «${params.query}».`);
        const lines = results.map((r: any, i: number) =>
          `${i + 1}. ${r.title || "(без заголовка)"}\n   ${r.url}\n   ${(r.content || "").trim()}`);
        return textResult(`Результаты по «${params.query}»:\n\n${lines.join("\n\n")}`);
      } catch (err: any) {
        return textResult(`web_search: ошибка обращения к SearXNG (${SEARXNG_URL}): ${err?.message || String(err)}`);
      }
    },
  });

  pi.registerTool({
    name: "web_fetch",
    label: "Web Fetch",
    description:
      "Скачать страницу по URL и вернуть её текстовое содержимое (HTML очищается " +
      "до текста, обрезается до лимита). Используй, чтобы прочитать найденный источник.",
    promptSnippet: "Скачать и прочитать страницу по URL.",
    parameters: {
      type: "object",
      properties: {
        url: { type: "string", description: "Полный URL страницы." },
      },
      required: ["url"],
    },
    async execute(_toolCallId: string, params: { url: string }, signal: AbortSignal) {
      // YouTube — тащим транскрипт через утилиту (кэш → лестница провайдеров с fail-over).
      // Страница-фолбэк для видео бесполезна (JS-скелет без транскрипта) → не рендерим, отвечаем терминально.
      const vid = (RUN_DIR && YT_SCRIPT) ? ytId(params.url) : null;
      if (vid) {
        try {
          return textResult(await fetchYouTube(params.url, vid));
        } catch (e: any) {
          const msg = (e?.message || String(e)).toString();
          return textResult(
            `[Транскрипт YouTube недоступен: перепробованы все провайдеры (${msg.slice(0, 160)}). ` +
            `Причина может быть временной (IP-бан YouTube, исчерпана квота или сбой apify-фолбэка) — ` +
            `повтори это задание позже. НЕ ищи транскрипт в вебе и не пытайся достать его со страницы — его там нет.]`);
        }
      }
      try {
        const res = await fetch(params.url, {
          signal: withTimeout(signal),
          headers: { "User-Agent": "Mozilla/5.0 (deep_research worker)" },
        });
        if (!res.ok) return textResult(`web_fetch: HTTP ${res.status} по ${params.url}.`);
        const ctype = res.headers.get("content-type") || "";
        const raw = await res.text();
        let text = ctype.includes("html") ? htmlToText(raw) : raw;
        // JS-сайт отдал пустой каркас → рендерим через headless (Playwright)
        let jsNote = "";
        if (text.length < JS_MIN && FETCH_SCRIPT && PYTHON && /^https?:/i.test(params.url)) {
          try {
            const rendered = (await renderPage(params.url)).trim();
            if (rendered.length > text.length) { text = rendered; jsNote = "[JS-рендер через headless-браузер]\n\n"; }
          } catch { /* остаёмся с тем, что было */ }
        }
        const rel = await saveRaw(`web-${hashName(params.url)}.txt`, text);  // персист сырья
        const clipped = text.length > FETCH_MAX
          ? text.slice(0, FETCH_MAX) + `\n\n[...обрезано на ${FETCH_MAX} символах из ${text.length}]`
          : text;
        const note = rel ? `\n\n[сырьё сохранено: ${rel}]` : "";
        return textResult(`${jsNote}Содержимое ${params.url}:\n\n${clipped}${note}`);
      } catch (err: any) {
        return textResult(`web_fetch: ошибка загрузки ${params.url}: ${err?.message || String(err)}`);
      }
    },
  });
}
