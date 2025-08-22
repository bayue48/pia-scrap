# PIA SCRAP: Novelpia → EPUB Scraper

Turn a Novelpia novel page (e.g. `https://global.novelpia.com/novel/1213`) into a clean **EPUB** with chapter titles, status, metadata, and cover.

> **Use responsibly.** This tool is for personal/offline reading of content your account can legitimately access. Respect the site’s **Terms of Service** and copyright.

---

## ✨ Features

* **EPUB output** with About page, ToC, per‑chapter files.
* **Correct novel title & status** (reads `.nv-stat-badge`).
* **Cover image** (from `og:image` / fallbacks) set as EPUB cover and shown on About page.
* **Chapter discovery** from Novelpia’s **paginated list**:

  * Navigates `.ch-list-section` and `.pagination` (20 items/page).
  * Clicks each `.list-item`/`.ch-info-wrapper` to capture the `/viewer/<id>` URL.
  * Falls back to anchors/ARIA/onclick if needed; can also **walk Next** inside the reader if no ToC is available.
* **Polite scraping**: robots.txt check, throttling, clear logs.
* **Auth**: accepts **Netscape cookies.txt** and/or **Playwright/Chrome storageState JSON**.

---

## 🧰 Requirements

* Python 3.9+
* Packages: `playwright`, `beautifulsoup4`, `python-slugify`, `ebooklib`, `httpx`
* Playwright browser: Chromium

Install:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 🚀 Quick Start

1. Clone the repository.

2. (Recommended) Export your **cookies** so the script can open free/ad-gated chapters you can view in your browser.

* **Netscape cookies.txt**

  1. Log in at `https://global.novelpia.com/` and confirm you can open a free chapter.
  2. Use a browser extension such as **Get cookies.txt** to export cookies **for this domain**.
  3. Save as `cookies.txt` (Netscape format).

* **Playwright/Chrome storageState JSON** (captures HttpOnly cookies more reliably)

  * From Playwright code or via tooling, export a `storage.json` with a `cookies` array.

3. Run:

```bash
python novelpia_epub.py --url https://global.novelpia.com/novel/1213 --cookies-txt cookies.txt
```

Output:

```
output/<novel-slug>/<novel-slug>.epub
output/<novel-slug>/metadata.json
output/<novel-slug>/chapters.jsonl
```

---

## 🔧 CLI

```
python novelpia_epub.py --url <NOVEL_URL> [--cookies-txt cookies.txt] [--cookies-json storage.json]
                        [--out output] [--max-chapters N]
```

**Arguments**

* `--url` (required) — Novel page, e.g. `https://global.novelpia.com/novel/1213`.
* `--cookies-txt` — Netscape cookies.txt (exported from your browser).
* `--cookies-json` — Playwright/Chrome storageState JSON; may include HttpOnly cookies.
* `--out` — Output folder (default: `output`).
* `--max-chapters` — Optional limit (useful for testing), e.g. `--max-chapters 10`.

---

## 🧠 How it Works

1. Loads the novel page and extracts metadata (title, author, tags, **status**).
2. Reads the ToC in `.ch-list-section`, which shows **20 items per page** with a `.pagination` bar.
3. For each page:

   * Iterates `.list-item` elements, grabs `Ch.N` + visible title (e.g., `Ch.0 Prologue`).
   * **Clicks the item** to open its `/viewer/<id>` page, captures the URL, then `go_back()` to the same ToC page.
4. If no ToC is found (rare), falls back to anchor/ARIA/onclick scanning and, as a last resort, enters a reader page and **walks Next**.
5. Fetches only `/viewer/` pages, pulls the reading container HTML, and writes an EPUB.
6. **Chapter titles** in the EPUB sidebar are taken from the **ToC list title** by default.

---

## 📦 EPUB Contents

* **About.xhtml**: Title, Author, Status, Source link, Description, **Cover image** (if found).
* `0001.xhtml`, `0002.xhtml`, … — chapters named by discovery order (ToC number and title shown in the page header and ToC).
* Embedded **cover.jpg** (if found) and a minimal stylesheet.

---

## 🪪 Legal & Fair Use

* The script checks `robots.txt` and is intended for **personal offline reading** of content you can already access.
* Do not re‑host, distribute, or scrape beyond what’s permitted by Novelpia’s **Terms of Service**.

---

## 🐞 Troubleshooting

* **Stuck after navigation**

  * Try running **non‑headless**: set `headless=False` in the script’s `chromium.launch`.
  * Some regions may show consent or bot‑check prompts; click them once, then re‑run.

* **“No chapters discovered”**

  * Ensure you’re logged in and can open a free chapter in your browser.
  * Re‑export cookies for **global.novelpia.com**. StorageState JSON often works best.

* **Identical chapter titles**

  * Fixed by default: titles come from the ToC (`Ch.N …`) or a per‑chapter header inside the reader.

* **About‑only EPUB**

  * Means no readable `/viewer/` page was accessible with the provided cookies.

* **Slow/fragile pages**

  * You can lower Playwright timeouts or adjust throttling. The script prints stage logs like `[stage] collecting chapters…` and `[toc] page N …`.

---

## 🧩 Extending

* Ranged download (`--from`, `--to`)
* Resume / skip existing
* Inline images extraction from reader
* Alternate outputs (EPUB + Markdown bundle or PDF)

---

## ✅ Example Session

```
[auth] loaded 6 cookies from cookies.txt
[nav] https://global.novelpia.com/novel/1213
[debug] cookies visible after goto: 15 total for *novelpia.com
[stage] extracting metadata…
[meta] title='Miss, Please Don’t Kill Yourself' status='Completed' author='…'
[stage] collecting chapters…
[toc] total_chapters=410  per_page=20  total_pages=21
[toc] page 1 …
[toc] items on page 1: 20
[toc]  + Ch.0 Prologue -> https://global.novelpia.com/viewer/249268
…
[ok] using 410 /viewer/ chapters
[open] 0001 Ch.0 Prologue -> https://global.novelpia.com/viewer/249268
…
[ok] EPUB -> output/miss-please-dont-kill-yourself/miss-please-dont-kill-yourself.epub
```

---

## 📄 License

Provided “as is”, for personal use only. No warranty. Respect the website’s ToS and copyright.
