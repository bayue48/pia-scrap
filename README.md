# PIA SCRAP (API): Novelpia → EPUB

Create a clean EPUB from a Novelpia novel using Novelpia’s API. Given a `novel_id` (e.g., `49`), the script fetches the novel data, episode list, pulls episode data, embeds images and cover, and writes a nicely structured EPUB with metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## Features

* API-based fetch (no browser automation).
* Proper EPUB with cover, About page, per‑chapter files, ToC, NCX/Nav.
* Preserves inline images (downloaded and embedded).
* Handles token refresh and optional throttling to reduce rate limits.

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
* Packages: `requests`, `beautifulsoup4`, `ebooklib`

Install packages:

```bash
pip install -r requirements.txt
```

---

## CLI

```
python pia.py NOVEL_ID [--user EMAIL] [--pass PASSWORD]
                   [--out DIR] [--max-chapters N]
                   [--lang en] [--proxy URL] [--throttle SECONDS]
                   [--debug]
```

Arguments

* `NOVEL_ID` (positional) — numeric `novel_no`, e.g. `49`.
* `--user`, `--pass` — login once; tokens saved to `.api.json` for reuse.
* `--out` — output directory (default: `output`).
* `--max-chapters` — fetch up to N episodes (0 or unset = all).
* `--lang` — EPUB language code (default `en`).
* `--proxy` — HTTP/HTTPS proxy, e.g. `http://host:port`.
* `--throttle` — seconds to wait between episode/ticket/content calls (default `2.0`).
* `--debug` — verbose request logs and optional JSON dumps for failures.

---

## Quick Start

1) First run with your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python pia.py 49 --user you@example.com --pass "your-password"
```

2) Subsequent runs can reuse stored tokens (no password on the command line):

```bash
python pia.py 49
```

---

## Output Details

Alongside the EPUB, the tool writes:

* `metadata.json` — title, author, tags (when available), total chapters, status, description, source URL.
* `chapters.jsonl` — one JSON line per chapter: index, title, URL of the web reader for that episode.

Output files are written under `output/<title>/`:

```
output/<title>/<title>.epub
output/<title>/metadata.json
output/<title>/chapters.jsonl
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

* 401/expired token — add `--user` and `--pass` once to refresh; tokens are persisted.
* Many 429/5xx responses — increase `--throttle` or add `--proxy`.
* Missing images — some external hosts may block requests; those images will remain as external links.
* HTTP debug — pass `--debug` to print masked headers/params and short body previews.

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
