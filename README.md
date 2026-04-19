# PIA SCRAP (API): Novelpia → EPUB

Create a clean EPUB from a Novelpia novel using Novelpia’s API. Given a `novel_id` (e.g., `49`), the script fetches the novel data, episode list, pulls episode data, embeds images and cover, and writes a nicely structured EPUB with metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## Features

* API-based fetch (no browser automation).
* **Parallel Fetching**: Uses `ThreadPoolExecutor` for high-performance concurrent chapter downloads.
* **Progress Reporting**: Real-time visual feedback with `tqdm` progress bars.
* **Flexible Chapter Selection**: Support for downloading specific chapter ranges (`--start`/`--end`).
* **Environment Variable Support**: Securely store credentials in a `.env` file via `python-dotenv`.
* **Advanced Automation**: Automatically handles rate limits (429) with smart backoff and session expiration (401) with auto re-login.
* Proper EPUB with cover, About page, per‑chapter files, ToC, NCX/Nav.
* Preserves inline images (downloaded and embedded).

---

## What It Does

* Authenticates against `https://api-global.novelpia.com` and stores `login_at` token + cookies in `.api.json`.
* Calls `novel/episode/list` to collect metadata and episodes.
* For each episode, requests a ticket, extracts the `_t` token, then fetches the episode data.
* Normalizes HTML (images, structure), embeds images into the EPUB, adds a minimal stylesheet.
* Adds an About page with Title, Author, Status, Source, Description, and cover when available.

---

## Requirements

* Python 3.9+
* Packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`

Install packages:

```bash
pip install -r requirements.txt
```

---

## CLI

```
python main.py NOVEL_ID [--user EMAIL] [--pass PASSWORD]
                   [--out DIR] [--max-chapters N]
                   [--start START_CHAPTER] [--end END_CHAPTER]
                   [--lang en] [--proxy URL] [--throttle SECONDS]
                   [--debug] [--txt]
```

Arguments

* `NOVEL_ID` (positional) — numeric `novel_no`, e.g. `49`.
* `--user`, `--pass` — login once; tokens saved to `.api.json` for reuse.
* `--out` — output directory (default: `output`).
* `--max-chapters` — fetch up to N episodes (0 or unset = all).
* `--start`, `--start-chapter` — start fetching from this chapter number.
* `--end`, `--end-chapter` — stop fetching at this chapter number.
* `--lang` — EPUB language code (default `en`).
* `--proxy` — HTTP/HTTPS proxy, e.g. `http://host:port`.
* `--throttle` — seconds to wait between episode/ticket/content calls (default `2.0`).
* `--debug` — verbose request logs and optional JSON dumps for failures.
* `--txt` — export as .txt per episode instead of EPUB.

---

## Quick Start

1) First run with your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python main.py 49 --user you@example.com --pass "your-password"
```

2) Subsequent runs can reuse stored tokens (no password on the command line):

```bash
python main.py 49
```

### Environment Variables (.env)
You can create a `.env` file in the root directory to store your credentials securely:

```env
NOVELPIA_EMAIL=your_email@example.com
NOVELPIA_PASSWORD=your_password
```
A template is provided in `.env.example`.

---

## Output Details

Output files are written under `output/<title>/`:

```
output/<title>/<title>.epub or output/<title>/<episode-title>.txt
```

---

## Example Session

```
[auth] Logged in as: FoggyRam2237
[info] extracting metadata…
[info] title='Occult Hunter of the Another World Academy' author='boratbitbam' chapter=134 status='Completed'
[info] ticket for episode 1; Death is another beginning. …
…
[info] ticket for episode 134; Epilogue …
[success] Wrote EPUB: output\occult-hunter-of-the-another-world-academy\occult-hunter-of-the-another-world-academy.epub  |  Title: Occult Hunter of the Another World Academy  |  Chapters: 134
```

---

## Tips & Troubleshooting

* **Auto-Recovery**: 401/expired tokens are now automatically handled if credentials are found in `.env` or provided via CLI.
* **Smart Backoff**: 429/Rate limits trigger an automatic exponential backoff and dynamic throttle adjustment.
* Missing images — some external hosts may block requests; those images will remain as external links.
* HTTP debug — pass `--debug` to print masked headers/params and short body previews.

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
