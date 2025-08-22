import argparse
import asyncio
import json
import re
from dataclasses import dataclass, asdict
from html import escape as hesc
from pathlib import Path
from typing import List, Optional, Dict, Any

import httpx
from bs4 import BeautifulSoup
from slugify import slugify
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout
from ebooklib import epub
import math

async def get_total_chapters(page: Page) -> Optional[int]:
    try:
        val = await page.evaluate("""
        () => {
          const el = document.querySelector('.ch-list-header .header-tit .text-primary-text');
          if(!el) return null;
          const n = parseInt((el.textContent||'').replace(/[^0-9]/g,''), 10);
          return Number.isFinite(n) ? n : null;
        }
        """)
        return int(val) if val else None
    except Exception:
        return None

async def get_current_page_num(page: Page) -> Optional[int]:
    try:
        txt = await page.locator('.pagination .page-btn.current').first.text_content()
        return int((txt or '').strip())
    except Exception:
        return None

async def goto_page(page: Page, target: int) -> bool:
    """Navigate pagination to the given page number."""
    cur = await get_current_page_num(page)
    if cur == target:
        return True

    # Try direct numeric button in current group
    try:
        btn = page.locator(f'.pagination .page-btn:not(.arrow):has-text("{target}")').first
        if await btn.count():
            await btn.click(timeout=2000)
            await page.wait_for_function(
                f"() => document.querySelector('.pagination .page-btn.current')?.textContent?.trim() === '{target}'",
                timeout=6000
            )
            return True
    except Exception:
        pass

    # Step groups using › until our target number appears
    for _ in range(60):  # enough for very long lists
        try:
            nxt = page.locator('.pagination .page-btn.arrow:has-text("›")').first
            if not await nxt.count():
                break
            await nxt.click(timeout=2000)
            await page.wait_for_timeout(250)
            # Try the numeric again
            btn = page.locator(f'.pagination .page-btn:not(.arrow):has-text("{target}")').first
            if await btn.count():
                await btn.click(timeout=2000)
                await page.wait_for_function(
                    f"() => document.querySelector('.pagination .page-btn.current')?.textContent?.trim() === '{target}'",
                    timeout=6000
                )
                return True
        except Exception:
            pass

    return False

async def click_item_capture_viewer(page: Page, item_index: int) -> Optional[str]:
    """
    Click the Nth list item to navigate to /viewer/<id>, capture URL, then go back.
    Returns the viewer URL or None if not obtained.
    """
    # Prefer clicking the whole .list-item; fallbacks try child wrappers.
    selectors = [
        f".ch-list-section .list-item:nth-of-type({item_index+1})",
        f".ch-list-section .list-item:nth-of-type({item_index+1}) .ch-info-wrapper",
        f".ch-list-section .list-item:nth-of-type({item_index+1}) .ch-info",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if not await el.count():
                continue
            await el.scroll_into_view_if_needed()
            before = page.url
            await el.click(timeout=2000)
            try:
                await page.wait_for_url(re.compile(r".*/viewer/\d+.*"), timeout=6000)
            except Exception:
                pass
            new_url = page.url
            if "/viewer/" in new_url and new_url != before:
                # go back to the list we were on
                await page.go_back(wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector(".ch-list-section", timeout=6000)
                except Exception:
                    pass
                return new_url
            # If nothing changed, try next selector
        except Exception:
            continue
    return None

# --------- Config ---------
BASE = "https://global.novelpia.com"
ROBOTS_URL = f"{BASE}/robots.txt"
DEFAULT_UA = "Mozilla/5.0 (compatible; NovelpiaPersonalScraper/1.2) Python/Playwright"
NAV_TIMEOUT = 25_000
SCROLL_PAUSE_MS = 300
MAX_SCROLLS_PAGE = 50
LIST_SCROLL_ROUNDS = 180
LIST_SCROLL_PAUSE_MS = 280
THROTTLE_MS = 700


# --------- Data ---------
@dataclass
class Chapter:
    idx: int
    title: str
    url: str

@dataclass
class NovelMeta:
    url: str
    title: str
    author: Optional[str]
    tags: List[str]
    description: Optional[str]
    status: Optional[str] = None


# --------- Helpers ---------
async def allowed_by_robots(path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": DEFAULT_UA}) as client:
            r = await client.get(ROBOTS_URL)
            r.raise_for_status()
            rules = r.text.splitlines()
        disallows, allows, current_all = [], [], False
        for line in rules:
            s = line.strip()
            if not s or s.startswith("#"): continue
            if s.lower().startswith("user-agent:"):
                current_all = (s.split(":",1)[1].strip() == "*")
            elif current_all and s.lower().startswith("disallow:"):
                disallows.append(s.split(":",1)[1].strip() or "/")
            elif current_all and s.lower().startswith("allow:"):
                allows.append(s.split(":",1)[1].strip())
        def matches(p: str) -> bool: return p and path.startswith(p)
        if any(matches(p) for p in allows):
            ld = max((p for p in disallows if matches(p)), key=len, default="")
            la = max((p for p in allows if matches(p)), key=len, default="")
            return len(la) >= len(ld)
        if any(matches(p) for p in disallows): return False
        return True
    except Exception:
        return True  # fail open; set False if you want to be stricter


def parse_netscape_cookies(txt_path: Path) -> List[Dict]:
    """
    Netscape cookies.txt: domain  flag  path  secure  expiry  name  value
    """
    cookies: List[Dict[str, Any]] = []
    for raw in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            parts = re.split(r"\s+", line)
        if len(parts) < 7:
            continue
        domain, include_sub, path, secure, expiry, name, value = parts[:7]
        c: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,                 # keep exact domain as provided
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": False,
            "sameSite": "Lax",
        }
        try:
            exp = int(expiry)
            if exp > 0:
                c["expires"] = exp
        except Exception:
            pass
        cookies.append(c)
    return cookies


async def ensure_login(context, cookies_json: Optional[str], cookies_txt: Optional[str]):
    # storageState JSON (Playwright/Chrome)
    if cookies_json:
        p = Path(cookies_json)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "cookies" in data:
                    data = data["cookies"]
                await context.add_cookies(data)  # type: ignore
                print(f"[auth] loaded {len(data)} cookies from JSON")
            except Exception as e:
                print(f"[warn] cookies JSON load failed: {e}")
        else:
            print(f"[warn] cookies JSON not found: {cookies_json}")

    # Netscape cookies.txt
    if cookies_txt:
        p = Path(cookies_txt)
        if p.exists():
            try:
                data = parse_netscape_cookies(p)
                await context.add_cookies(data)  # type: ignore
                print(f"[auth] loaded {len(data)} cookies from cookies.txt")
            except Exception as e:
                print(f"[warn] cookies.txt load failed: {e}")
        else:
            print(f"[warn] cookies.txt not found: {cookies_txt}")


async def debug_cookie_echo(page: Page, label=""):
    try:
        ck = await page.context.cookies()
        scoped = [c for c in ck if "novelpia.com" in c.get("domain","")]
        print(f"[debug] cookies visible {label}: {len(scoped)} total for *novelpia.com")
    except Exception:
        pass


async def auto_scroll_full(page: Page):
    last_h = await page.evaluate("() => document.body.scrollHeight")
    for _ in range(MAX_SCROLLS_PAGE):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        new_h = await page.evaluate("() => document.body.scrollHeight")
        if new_h == last_h:
            break
        last_h = new_h


def _abs_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"{BASE}{href}"
    return href

async def fetch_cover_bytes_from_page(page: Page) -> Optional[bytes]:
    # try common meta tags first
    cover_url = None
    try:
        cover_url = await page.locator('meta[property="og:image"]').first.get_attribute("content")
        cover_url = _abs_url(cover_url)
    except Exception:
        pass
    if not cover_url:
        try:
            tw = await page.locator('meta[name="twitter:image"]').first.get_attribute("content")
            cover_url = _abs_url(tw)
        except Exception:
            pass
    if not cover_url:
        # visible image fallbacks
        try:
            sel = ".nv-cover img, .cover img, img[alt*='cover' i], img[src*='cover']"
            src = await page.locator(sel).first.get_attribute("src")
            cover_url = _abs_url(src)
        except Exception:
            pass

    if not cover_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": DEFAULT_UA}) as client:
            r = await client.get(cover_url)
            r.raise_for_status()
            return r.content
    except Exception:
        return None


# --------- Metadata ---------
async def extract_meta(page: Page, novel_url: str) -> NovelMeta:
    print("[stage] extracting metadata…")

    # Title from <title> or og:title, strip "Novelpia - "
    title = ""
    try:
        pt = await page.title()
        if pt:
            title = re.sub(r"^\s*Novelpia\s*-\s*", "", pt.strip(), flags=re.I)
            title = re.sub(r"\s*-\s*Novelpia\s*$", "", title, flags=re.I)
    except Exception:
        pass
    if not title:
        try:
            ogt = await page.locator('meta[property="og:title"]').first.get_attribute("content")
            if ogt:
                ogt = re.sub(r"^\s*Novelpia\s*-\s*", "", ogt.strip(), flags=re.I)
                ogt = re.sub(r"\s*-\s*Novelpia\s*$", "", ogt, flags=re.I)
                title = ogt
        except Exception:
            pass

    # Author / tags / status via fast DOM evaluate (no waiting)
    js = """
    () => {
      const q = (s) => document.querySelector(s);
      const qa = (s) => Array.from(document.querySelectorAll(s));
      const txt = (el) => (el && (el.textContent||'').trim()) || '';
      // Try label "Author" -> next sibling
      let author = '';
      const labels = qa('*');
      for (const el of labels) {
        if (/^Author$/i.test((el.textContent||'').trim())) {
          const sib = el.nextElementSibling;
          author = txt(sib);
          if (author) break;
        }
      }
      if (!author) {
        const m = qa('*').map(e => txt(e)).find(t => /^Author\\s*[:\\-]?/i.test(t));
        if (m) author = m.replace(/^Author\\s*[:\\-]?/i,'').trim();
      }
      // tags starting with '#'
      const tags = qa('a,span').map(e => txt(e)).filter(t => /^#/.test(t)).map(t => t.replace(/^#/, '').trim());
      // status badge
      let status = txt(q('.nv-stat-badge'));
      if (/comp|completed/i.test(status)) status = 'Completed';
      else if (/ongoing/i.test(status)) status = 'Ongoing';
      return {author, tags: Array.from(new Set(tags)), status};
    }
    """
    try:
        got = await page.evaluate(js)
    except Exception:
        got = {"author": "", "tags": [], "status": None}

    author = (got.get("author") or "").strip() or None
    tags = got.get("tags") or []
    status = got.get("status") or None

    # Description (best-effort, non-blocking)
    description = None
    try:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.select("article, .description, .prose, .detail, .content, p")
        best = ""
        for n in blocks[:60]:
            t = n.get_text(" ", strip=True)
            if len(t) > len(best): best = t
        description = best or None
    except Exception:
        pass

    meta = NovelMeta(novel_url, title or "Untitled", author, tags, description, status)
    print(f"[meta] title={meta.title!r} status={meta.status!r} author={meta.author!r}")
    return meta


async def scroll_chapter_list(page: Page, *args, **kwargs):
    # Not needed for paginated list; keep as a stub for compatibility.
    return


async def collect_chapters_from_section(page: Page, novel_url: str) -> List[Chapter]:
    chapters: List[Chapter] = []
    seen = set()

    # Wait for the section (fast-fail)
    try:
        await page.wait_for_selector(".ch-list-section", timeout=8000)
    except Exception:
        print("[info] .ch-list-section not found within 8s; falling back")
        return chapters

    total = await get_total_chapters(page)
    per_page = 20  # based on your HTML (20 items per page)
    total_pages = math.ceil(total / per_page) if total else None
    print(f"[toc] total_chapters={total}  per_page={per_page}  total_pages={total_pages or 'unknown'}")

    # Start from whichever page is current (usually 1)
    cur = await get_current_page_num(page) or 1

    visited_pages: set[int] = set()
    safety_pages = total_pages or 200  # reasonable upper bound if total unknown

    page_iter = 0
    while page_iter < safety_pages:
        page_iter += 1
        cur = await get_current_page_num(page) or cur
        if cur in visited_pages:
            # Try to advance to the next visible page number (or break if none)
            try:
                nxt_btn = page.locator('.pagination .page-btn:not(.arrow):not(.current)').first
                if await nxt_btn.count():
                    target_txt = (await nxt_btn.text_content() or "").strip()
                    if target_txt.isdigit() and await goto_page(page, int(target_txt)):
                        cur = int(target_txt)
                    else:
                        break
                else:
                    # As a last resort, click › once
                    nxt = page.locator('.pagination .page-btn.arrow:has-text("›")').first
                    if await nxt.count():
                        await nxt.click(timeout=1500)
                        await page.wait_for_timeout(250)
                        continue
                    break
            except Exception:
                break

        visited_pages.add(cur)
        print(f"[toc] page {cur} …")

        # Count items on this page
        wrappers = page.locator(".ch-list-section .list-item")
        count = await wrappers.count()
        print(f"[toc] items on page {cur}: {count}")

        # Walk each item, capture its viewer URL
        for i in range(count):
            # Extract display title (Ch.N + Chapter Title) for nicer metadata
            num_txt = ""
            title_txt = ""
            try:
                num_txt = (await wrappers.nth(i).locator(".chapter-number").first.text_content()) or ""
                num_txt = num_txt.strip()
            except Exception:
                pass
            try:
                title_txt = (await wrappers.nth(i).locator(".chapter-title").first.text_content()) or ""
                title_txt = title_txt.strip()
            except Exception:
                pass
            pretty_title = (f"{num_txt} {title_txt}".strip() or f"Chapter {(len(chapters)+1)}").strip()

            url = await click_item_capture_viewer(page, i)
            if not url:
                # If click didn't navigate, try to sniff a nested href (rare)
                try:
                    href = await wrappers.nth(i).locator("a[href*='/viewer/']").first.get_attribute("href")
                    if href:
                        url = href if href.startswith("http") else (BASE + href)
                except Exception:
                    pass

            if url and "/viewer/" in url and url not in seen:
                seen.add(url)
                chapters.append(Chapter(idx=len(chapters)+1, title=pretty_title, url=url))
                print(f"[toc]  + {pretty_title} -> {url}")

        # Move to the next page number if known; else try next visible numeric or ›
        if total_pages and cur >= total_pages:
            break

        # Prefer next numeric button (current + 1)
        target = (cur + 1)
        if await goto_page(page, target):
            cur = target
            continue

        # If that didn’t exist (pagination groups), click › then click the lowest visible number
        try:
            nxt = page.locator('.pagination .page-btn.arrow:has-text("›")').first
            if await nxt.count():
                await nxt.click(timeout=1500)
                await page.wait_for_timeout(250)
                # pick the smallest numeric on the bar
                nums = page.locator('.pagination .page-btn:not(.arrow)')
                ncnt = await nums.count()
                tgt = None
                for j in range(ncnt):
                    t = (await nums.nth(j).text_content() or "").strip()
                    if t.isdigit():
                        tgt = int(t)
                        break
                if tgt and await goto_page(page, tgt):
                    cur = tgt
                    continue
        except Exception:
            pass

        # If we couldn't advance, stop.
        break

    return chapters


async def collect_chapters(page: Page, novel_url: str) -> List[Chapter]:
    # Primary: paginated virtual list
    primary = await collect_chapters_from_section(page, novel_url)
    if primary:
        return primary

    # Fallbacks (keep your previous anchor/ARIA/onclick heuristics here)
    # … (no changes needed if you already have them) …
    return []


# --------- Walking via Next (fallback when no TOC) ---------
async def walk_next_chapters(page: Page, start_url: str, max_steps: int = 300) -> List[Chapter]:
    chapters: List[Chapter] = []
    seen = set()
    url = start_url
    idx = 1

    for _ in range(max_steps):
        if not url or url in seen:
            break
        seen.add(url)

        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)

            for label in [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]:
                    try:
                        btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                        if await btn.is_visible():
                            await btn.click(timeout=1500)
                            await page.wait_for_timeout(300)
                    except Exception:
                        pass

        except PWTimeout:
            pass

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else f"Chapter {idx}"
        chapters.append(Chapter(idx=idx, title=title or f"Chapter {idx}", url=url))

        # Try to find a Next
        next_url = None
        # common labels
        for label in [r"^Next$", r"^Next Episode$", r"^Next Chapter$", r"^다음$", r"^다음 화$", r"^▶$", r"^›$"]:
            try:
                btn = page.get_by_text(re.compile(label, re.I)).first
                if await btn.is_visible():
                    href = await btn.get_attribute("href")
                    if href:
                        next_url = href if href.startswith("http") else (BASE + href)
                    else:
                        await btn.click(timeout=2000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)

                            for label in [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]:
                                try:
                                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                                    if await btn.is_visible():
                                        await btn.click(timeout=1500)
                                        await page.wait_for_timeout(300)
                                except Exception:
                                    pass

                        except PWTimeout:
                            pass
                        next_url = page.url
                    break
            except Exception:
                pass

        # anchors
        if not next_url:
            try:
                cand = await page.locator("a[rel='next'], a[aria-label*='Next i'], a:has-text('Next')").first.get_attribute("href")
                if cand:
                    next_url = cand if cand.startswith("http") else (BASE + cand)
            except Exception:
                pass

        if not next_url or next_url == url:
            break

        url = next_url
        idx += 1
        await page.wait_for_timeout(THROTTLE_MS)

    return chapters


# --------- Content extraction & EPUB ---------
def extract_readable(html: str, list_title: str = "", novel_title: Optional[str] = None) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    # find main reading container
    containers = [
        "[id*='viewer']", ".viewer", ".view-contents", ".read-contents",
        "article", ".article", ".reader", ".chapter", ".prose",
        ".ql-editor", ".content", "[data-reader]", "[data-contents]"
    ]
    node = None
    for sel in containers:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            break

    # try to extract an in-page chapter header
    header_title = ""
    if node:
        for sel in [".chapter-title", ".ep-title", ".title", "header h1", "h1", "h2"]:
            h = node.select_one(sel)
            if h:
                header_title = h.get_text(" ", strip=True)
                if header_title:
                    break

    # decide the final title
    def is_good(t: str) -> bool:
        if not t:
            return False
        s = t.strip()
        if s.lower().startswith("novelpia -"):
            return False
        if novel_title and s.strip() == novel_title.strip():
            return False
        # if it's too generic or very short, reject
        if len(s) < 4:
            return False
        return True

    final_title = header_title if is_good(header_title) else (list_title or header_title or "Chapter")

    # body HTML
    if node:
        body_html = "".join(str(x) for x in node.contents)
    else:
        # paragraph fallback
        paras = [p.get_text(" ", strip=True) for p in soup.select("p") if len(p.get_text(strip=True)) > 10]
        if not paras:
            raise RuntimeError("No readable content found (might be gated).")
        from html import escape as hesc
        body_html = "".join(f"<p>{hesc(t)}</p>" for t in paras)

    return {"title": final_title, "html": body_html}


async def build_epub(novel: NovelMeta, chapters_payload: List[Dict], out_dir: Path, cover_bytes: Optional[bytes] = None) -> Path:
    book = epub.EpubBook()
    book.set_identifier(slugify(novel.title or novel.url))
    book.set_title(novel.title or "Untitled")
    if cover_bytes:
        book.set_cover("cover.jpg", cover_bytes)
    if novel.author:
        book.add_author(novel.author)
    if novel.tags:
        book.add_metadata('DC', 'subject', ", ".join(novel.tags))
    if novel.description:
        book.add_metadata('DC', 'description', novel.description)

    # About page (with Status)
    intro = epub.EpubHtml(title="About", file_name="about.xhtml", lang="en")
    intro.content = f"""
    <h1>{hesc(novel.title)}</h1>
    {"<p><img src='cover.jpg' alt='Cover' style='max-width:100%;height:auto;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>" if cover_bytes else ""}
    <p><strong>Author:</strong> {hesc(novel.author or 'Unknown')}</p>
    <p><strong>Status:</strong> {hesc(novel.status or 'Unknown')}</p>
    <p><strong>Source:</strong> <a href="{novel.url}">{novel.url}</a></p>
    <p>{hesc((novel.description or '').strip()[:2000])}</p>
    """
    book.add_item(intro)

    items = [intro]
    spine = ['nav', intro]
    toc = [epub.Link('about.xhtml', 'About', 'about')]

    for ch in chapters_payload:
        idx = ch["idx"]
        title = (ch["title"] or f"Chapter {idx}").strip()
        body_html = ch["html"]
        file_name = f"{str(idx).zfill(4)}.xhtml"

        chap = epub.EpubHtml(title=title, file_name=file_name, lang="en")
        chap.content = f"<h2>{hesc(title)}</h2>\n{body_html}"
        book.add_item(chap)
        items.append(chap)
        spine.append(chap)
        toc.append(epub.Link(file_name, title, f"ch{idx}"))

    book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    css = epub.EpubItem(uid="style_nav", file_name="style/style.css", media_type="text/css",
                        content="body{font-family:serif;line-height:1.5} h1,h2{font-family:sans-serif}")
    book.add_item(css)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slugify(novel.title) or 'novel'}.epub"
    epub.write_epub(str(out_path), book, {})
    return out_path


# --------- Main ---------
async def main():
    ap = argparse.ArgumentParser(description="Novelpia → EPUB (supports cookies.txt and virtual list TOC).")
    ap.add_argument("--url", required=True, help="Novel URL, e.g. https://global.novelpia.com/novel/1213")
    ap.add_argument("--cookies-json", help="Playwright/Chrome storageState JSON (optional)")
    ap.add_argument("--cookies-txt", help="Netscape cookies.txt (optional)")
    ap.add_argument("--out", default="output", help="Output folder (default: output/)")
    ap.add_argument("--max-chapters", type=int, default=0, help="Optional cap on chapters (0 = no cap)")
    args = ap.parse_args()

    from urllib.parse import urlparse
    if not await allowed_by_robots(urlparse(args.url).path or "/"):
        print("[blocked] robots.txt disallows this path.")
        return

    out_dir = Path(args.out)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=DEFAULT_UA,
            locale="en-US",
            viewport={"width": 1280, "height": 2100},
        )
        context.set_default_timeout(6000)
        context.set_default_navigation_timeout(15000)
        await ensure_login(context, args.cookies_json, args.cookies_txt)
        page: Page = await context.new_page()

        print(f"[nav] {args.url}")
        await page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)

            for label in [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]:
                    try:
                        btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                        if await btn.is_visible():
                            await btn.click(timeout=1500)
                            await page.wait_for_timeout(300)
                    except Exception:
                        pass
                    
        except PWTimeout:
            pass
        await debug_cookie_echo(page, "after goto")

        novel = await extract_meta(page, args.url)
        cover_bytes = await fetch_cover_bytes_from_page(page) 
        novel_slug = slugify(novel.title or "novelpia")
        book_dir = out_dir / novel_slug
        book_dir.mkdir(parents=True, exist_ok=True)

        # Discover chapters (virtual list aware)
        print("[stage] collecting chapters…")
        chapters = await collect_chapters(page, novel_url=args.url)
        print(f"[stage] collected {len(chapters)} entries from TOC")
        print(f"[ok] discovered {len(chapters)} potential entries")

        # If none or only non-viewer, try to seed a /viewer/ and walk Next
        only_non_viewer = chapters and all("/viewer/" not in c.url for c in chapters)
        if not chapters or only_non_viewer:
            print("[info] TOC incomplete; trying to seed /viewer/ and walk via Next…")
            start_url = None

            # Try a visible Start/Read button
            for label in [r"^Start reading$", r"^Read$", r"^Start$", r"^Continue$"]:
                try:
                    btn = page.get_by_text(re.compile(label, re.I)).first
                    if await btn.is_visible():
                        await btn.click(timeout=2500)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)

                            for label in [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]:
                                try:
                                    btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                                    if await btn.is_visible():
                                        await btn.click(timeout=1500)
                                        await page.wait_for_timeout(300)
                                except Exception:
                                    pass

                        except PWTimeout:
                            pass
                        start_url = page.url
                        break
                except Exception:
                    pass

            # Or scan DOM for a /viewer/ link
            if not start_url:
                try:
                    found = await page.evaluate("""
                    () => {
                      const urls = [];
                      document.querySelectorAll('a[href]').forEach(a => {
                        const h=a.getAttribute('href'); if (/\\/viewer\\//.test(h||'')) urls.push(h);
                      });
                      return urls[0] || null;
                    }
                    """)
                    if found:
                        start_url = found if found.startswith("http") else (BASE + found)
                except Exception:
                    pass

            if start_url and "/viewer/" in start_url:
                walked = await walk_next_chapters(page, start_url=start_url, max_steps=300)
                if walked:
                    chapters = walked
                    print(f"[ok] walked {len(chapters)} chapters via Next")

        # De-dup and optionally cap
        dedup: Dict[str, Chapter] = {}
        for c in chapters:
            if c.url not in dedup and ("/viewer/" in c.url):
                dedup[c.url] = c
        chapters = list(dedup.values())
        chapters.sort(key=lambda c: c.idx)

        if args.max_chapters and args.max_chapters > 0:
            chapters = chapters[:args.max_chapters]

        if not chapters:
            print("[warn] No /viewer/ chapters discovered. EPUB will include About page only.")
        else:
            print(f"[ok] using {len(chapters)} /viewer/ chapters")

        # Fetch content
        payloads: List[Dict[str, Any]] = []
        for ch in chapters:
            if "/viewer/" not in ch.url:
                continue
            try:
                print(f"[open] {ch.idx:03d} {ch.title} -> {ch.url}")
                await page.goto(ch.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)

                    for label in [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]:
                        try:
                            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
                            if await btn.is_visible():
                                await btn.click(timeout=1500)
                                await page.wait_for_timeout(300)
                        except Exception:
                            pass

                except PWTimeout:
                    pass

                # quick gate buttons if present
                for label in [r"^Read$", r"^Start$", r"^Continue$", r"^Start reading$"]:
                    try:
                        btn = page.get_by_text(re.compile(label, re.I)).first
                        if await btn.is_visible():
                            await btn.click(timeout=2000)
                            await page.wait_for_timeout(1200)
                            break
                    except Exception:
                        pass

                html = await page.content()
                data = extract_readable(html, list_title=ch.title, novel_title=novel.title)
                title = data["title"].strip() or ch.title
                payloads.append({"idx": ch.idx, "title": title, "html": data["html"]})
                await page.wait_for_timeout(THROTTLE_MS)
            except Exception as e:
                print(f"[skip] {ch.idx:03d} {ch.title}: {e}")
                await page.wait_for_timeout(THROTTLE_MS)

        # Save metadata + chapter list regardless
        (book_dir / "metadata.json").write_text(json.dumps(asdict(novel), ensure_ascii=False, indent=2), "utf-8")
        with (book_dir / "chapters.jsonl").open("w", encoding="utf-8") as f:
            for ch in chapters:
                f.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")

        out_path = await build_epub(novel, payloads, book_dir, cover_bytes=cover_bytes)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
