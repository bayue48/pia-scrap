import argparse
import asyncio
import json
import re
import httpx
import math
from dataclasses import dataclass, asdict
from html import escape as hesc
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from bs4 import BeautifulSoup
from slugify import slugify
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout
from ebooklib import epub

# --------- Config ---------
BASE = "https://global.novelpia.com"
ROBOTS_URL = f"{BASE}/robots.txt"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59"
NAV_TIMEOUT = 25_000
SCROLL_PAUSE_MS = 300
MAX_SCROLLS_PAGE = 50
LIST_SCROLL_PAUSE_MS = 280
THROTTLE_MS = 700
CONSENT_BUTTON_LABELS = [r"^I agree$", r"^Agree$", r"^Accept$", r"^Ok$", r"^Close$"]
NEXT_BUTTON_LABELS = [r"^Next$", r"^Next Episode$", r"^Next Chapter$", r"^다음$", r"^다음 화$", r"^▶$", r"^›$"]
START_BUTTON_LABELS = [r"^Start reading$", r"^Read$", r"^Start$", r"^Continue$"]

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
async def handle_consent_buttons(page: Page) -> None:
    for label in CONSENT_BUTTON_LABELS:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=1500)
                await page.wait_for_timeout(300)
                break
        except Exception:
            continue

async def safe_goto(page: Page, url: str, wait_for_network_idle: bool = True) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    if wait_for_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
    await handle_consent_buttons(page)

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
    """Navigate to a specific numeric page in the chapter list."""
    def js_has_target(n: int) -> str:
        return f"""
        () => !!Array.from(document.querySelectorAll('.pagination .page-btn:not(.arrow)'))
               .find(el => (el.textContent||'').trim() === '{n}')
        """

    try:
        cur_txt = await page.locator('.pagination .page-btn.current').first.text_content()
        if cur_txt and cur_txt.strip().isdigit() and int(cur_txt.strip()) == target:
            return True
    except Exception:
        pass

    try:
        btn = page.locator(f'.pagination .page-btn:not(.arrow):has-text("{target}")').first
        if await btn.count():
            await btn.click(timeout=1800)
            await page.wait_for_function(
                f"() => (document.querySelector('.pagination .page-btn.current')?.textContent||'').trim() === '{target}'",
                timeout=6000
            )
            return True
    except Exception:
        pass

    # step groups via ›
    for _ in range(40):
        try:
            if await page.evaluate(js_has_target(target)):
                btn = page.locator(f'.pagination .page-btn:not(.arrow):has-text("{target}")').first
                await btn.click(timeout=1800)
                await page.wait_for_function(
                    f"() => (document.querySelector('.pagination .page-btn.current')?.textContent||'').trim() === '{target}'",
                    timeout=6000
                )
                return True

            nxt = page.locator('.pagination .page-btn.arrow:has-text("›")').first
            if not await nxt.count():
                break
            await nxt.click(timeout=1500)
            await page.wait_for_timeout(300)
        except Exception:
            pass
    return False

def parse_netscape_cookies(txt_path: Path) -> List[Dict]:
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
            "name": name, "value": value, "domain": domain, "path": path or "/",
            "secure": secure.upper() == "TRUE", "httpOnly": False, "sameSite": "Lax",
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
    if cookies_json:
        p = Path(cookies_json)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "cookies" in data:
                    data = data["cookies"]
                await context.add_cookies(data)
                print(f"[auth] loaded {len(data)} cookies from JSON")
            except Exception as e:
                print(f"[warn] cookies JSON load failed: {e}")

    if cookies_txt:
        p = Path(cookies_txt)
        if p.exists():
            try:
                data = parse_netscape_cookies(p)
                await context.add_cookies(data)
                print(f"[auth] loaded {len(data)} cookies from cookies.txt")
            except Exception as e:
                print(f"[warn] cookies.txt load failed: {e}")

async def looks_logged_out(page: Page) -> bool:
    """Heuristic: if clicking TOC items yields no /viewer/ and a Sign In button is present."""
    try:
        # Login routes or obvious sign components
        if re.search(r"/auth|/login|/signin", page.url, re.I):
            return True
        # Top-right "Sign In" button visible on list page
        btn = page.get_by_text(re.compile(r"^\s*Sign In\s*$", re.I)).first
        if await btn.count():
            # treat it as a weak signal; still return True (works well in practice when cookies expire)
            return True
    except Exception:
        pass
    return False

async def maybe_reauth(page: Page, context, novel_url: str, cookies_json: Optional[str], cookies_txt: Optional[str], where: str) -> None:
    if await looks_logged_out(page):
        print(f"[auth] login UI detected ({where}); reauth and reload…")
        await ensure_login(context, cookies_json, cookies_txt)
        await safe_goto(page, novel_url)

async def fetch_cover_bytes_from_page(page: Page) -> Optional[bytes]:
    cover_url = None
    try:
        cover_url = await page.locator('meta[property="og:image"]').first.get_attribute("content")
    except Exception:
        pass
    if not cover_url:
        try:
            cover_url = await page.locator('meta[name="twitter:image"]').first.get_attribute("content")
        except Exception:
            pass

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

    if cover_url:
        cover_url = _abs_url(cover_url)

    if not cover_url:
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

    js = """
    () => {
      const q = (s) => document.querySelector(s);
      const qa = (s) => Array.from(document.querySelectorAll(s));
      const txt = (el) => (el && (el.textContent||'').trim()) || '';
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
      const tags = qa('a,span').map(e => txt(e)).filter(t => /^#/.test(t)).map(t => t.replace(/^#/, '').trim());
      let status = txt(q('.nv-stat-badge'));
      if (/comp|completed/i.test(status)) status = 'Completed';
      else if (/ongoing|up/i.test(status)) status = 'Ongoing';
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

# --------- TOC collection ---------
async def click_item_capture_viewer(page: Page, item_index: int, cur_page: int, novel_url: str) -> Optional[str]:
    """Click Nth item -> capture /viewer/ URL -> return to TOC page 'cur_page'."""
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
            await el.click(timeout=1800, force=True)

            # tiny bounded wait for SPA route
            try:
                await page.wait_for_function("() => location.href.includes('/viewer/')", timeout=2500)
            except Exception:
                pass

            new_url = page.url
            if "/viewer/" in new_url and new_url != before:
                # go straight back to the same TOC page
                await safe_goto(page, novel_url)
                await page.wait_for_selector(".ch-list-section", timeout=6000)
                await goto_page(page, cur_page)
                return new_url

            # if no route, try sniffing nested href quickly
            try:
                href = await el.locator("a[href*='/viewer/']").first.get_attribute("href")
                if href:
                    url = href if href.startswith("http") else (BASE + href)
                    await safe_goto(page, novel_url)
                    await page.wait_for_selector(".ch-list-section", timeout=6000)
                    await goto_page(page, cur_page)
                    return url
            except Exception:
                pass
        except Exception:
            continue
    return None

async def collect_chapters_from_section(
    page: Page,
    context,
    novel_url: str,
    cap: Optional[int],
    cookies_json: Optional[str],
    cookies_txt: Optional[str],
) -> List[Chapter]:
    chapters: List[Chapter] = []
    seen: Set[str] = set()

    try:
        await page.wait_for_selector(".ch-list-section", timeout=8000)
    except Exception:
        print("[info] .ch-list-section not found within 8s; falling back")
        return chapters

    total = await get_total_chapters(page)
    per_page = 20
    total_pages = math.ceil(total / per_page) if total else None
    print(f"[toc] total_chapters={total}  per_page={per_page}  total_pages={total_pages or 'unknown'}")

    cur = await get_current_page_num(page) or 1
    safety_pages = total_pages or 200

    items_tried = 0                        # cap is applied to TRIES (prevents flipping to next page)
    hard_cap = cap if (cap and cap > 0) else None

    for _ in range(safety_pages):
        # if we've already tried enough items, stop right away (no page 2)
        if hard_cap is not None and items_tried >= hard_cap:
            break

        cur = await get_current_page_num(page) or cur
        print(f"[toc] page {cur} …")

        wrappers = page.locator(".ch-list-section .list-item")
        count = await wrappers.count()
        print(f"[toc] items on page {cur}: {count}")

        # only try up to the remaining allowance on this page
        limit_on_page = count
        if hard_cap is not None:
            remaining = hard_cap - items_tried
            if remaining <= 0:
                break
            limit_on_page = min(limit_on_page, remaining)

        for i in range(limit_on_page):
            print(f"[toc]  trying item {i+1}/{count} on page {cur} …")
            await maybe_reauth(page, context, novel_url, cookies_json, cookies_txt, where="toc-item")

            wrappers = page.locator(".ch-list-section .list-item")
            num_txt, title_txt = "", ""
            try:
                num_txt = (await wrappers.nth(i).locator(".chapter-number").first.text_content() or "").strip()
            except Exception:
                pass
            try:
                title_txt = (await wrappers.nth(i).locator(".chapter-title").first.text_content() or "").strip()
            except Exception:
                pass
            pretty_title = (f"{num_txt} {title_txt}".strip() or f"Chapter {(len(chapters)+1)}").strip()

            url = await click_item_capture_viewer(page, i, cur_page=cur, novel_url=novel_url)
            items_tried += 1  # count a try whether or not it yielded a URL

            if not url:
                # one more quick sniff for nested href
                try:
                    href = await wrappers.nth(i).locator("a[href*='/viewer/']").first.get_attribute("href")
                    if href:
                        url = href if href.startswith("http") else (BASE + href)
                except Exception:
                    pass

            if url and "/viewer/" in url and url not in seen:
                print(f"[toc]    viewer: {url}")
                seen.add(url)
                chapters.append(Chapter(idx=len(chapters) + 1, title=pretty_title, url=url))
                if hard_cap is not None and len(chapters) >= hard_cap:
                    return chapters

        # stop if finished or reached last page
        if (total_pages and cur >= total_pages) or (hard_cap is not None and items_tried >= hard_cap):
            break

        # next numeric (cur + 1)
        target = cur + 1
        if await goto_page(page, target):
            continue

        # otherwise try one › then smallest visible number
        try:
            nxt = page.locator('.pagination .page-btn.arrow:has-text("›")').first
            if await nxt.count():
                await nxt.click(timeout=1500)
                await page.wait_for_timeout(250)
                nums = page.locator('.pagination .page-btn:not(.arrow)')
                ncnt = await nums.count()
                for j in range(ncnt):
                    t = (await nums.nth(j).text_content() or "").strip()
                    if t.isdigit():
                        if await goto_page(page, int(t)):
                            break
                else:
                    break
            else:
                break
        except Exception:
            break

    return chapters

async def collect_chapters(page: Page, context, novel_url: str, cap: Optional[int], cookies_json: Optional[str], cookies_txt: Optional[str]) -> List[Chapter]:
    primary = await collect_chapters_from_section(page, context, novel_url, cap, cookies_json, cookies_txt)
    if primary:
        return primary
    return []

# --------- Walk via Next (fallback) ---------
async def walk_next_chapters(page: Page, start_url: str, max_steps: int = 300) -> List[Chapter]:
    chapters: List[Chapter] = []
    seen = set()
    url = start_url
    idx = 1

    for _ in range(max_steps):
        if not url or url in seen:
            break
        seen.add(url)

        await safe_goto(page, url)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else f"Chapter {idx}"
        chapters.append(Chapter(idx=idx, title=title or f"Chapter {idx}", url=url))

        next_url = None
        for label in NEXT_BUTTON_LABELS:
            try:
                btn = page.get_by_text(re.compile(label, re.I)).first
                if await btn.is_visible(timeout=1000):
                    href = await btn.get_attribute("href")
                    if href:
                        next_url = href if href.startswith("http") else (BASE + href)
                    else:
                        await btn.click(timeout=2000)
                        await page.wait_for_load_state("networkidle", timeout=8000)
                        await handle_consent_buttons(page)
                        next_url = page.url
                    break
            except Exception:
                pass

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

# --------- Comment removal & content extraction ---------
def _rm_all(soup, selectors):
    for sel in selectors:
        for el in soup.select(sel):
            el.decompose()

def _strip_comment_blocks(node_or_soup):
    import re as _re
    root = node_or_soup
    _rm_all(root, [
        ".comment-all-wrapper",".comment-header",".comment-write-box",".comment-list-wrapper",
        ".cmtbox",".comment-txt",".comment-action-btn",".reply-write-input",".btn-reply-input",
        ".user-info",".user-nic",".input-date",".nv-comments",".comments",".comment","#comments","#comment",
        "[data-comments]","[data-comment]",
    ])
    def is_commentish(tag):
        if not getattr(tag, "attrs", None): return False
        cid = " ".join(tag.get("class", [])).lower()
        tid = (tag.get("id") or "").lower()
        return ("comment" in cid or "reply" in cid or "comment" in tid or "reply" in tid)
    for el in list(root.find_all(lambda t: t.name in ("div","section","ul","ol","aside","article") and is_commentish(t))):
        el.decompose()
    pat_no_comments = _re.compile(r"^\s*(there (are )?no comments|no comments)\s*$", _re.I)
    pat_timestamp = _re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*(am|pm)\s*$", _re.I)
    for el in list(root.find_all(["p","div","li"])):
        txt = (el.get_text(" ", strip=True) or "")
        if pat_no_comments.match(txt) or pat_timestamp.match(txt) or txt.strip() in {"HOT","NEWEST","ADD"}:
            parent = el.find_parent(["li","article","div","section"]) or el
            parent.decompose()

def extract_readable(html: str, list_title: str = "", novel_title: Optional[str] = None) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    _strip_comment_blocks(soup)
    containers = [
        "[id*='viewer']", ".viewer", ".view-contents", ".read-contents",
        "article", ".article", ".reader", ".chapter", ".prose",
        ".ql-editor", ".content", "[data-reader]", "[data-contents]"
    ]
    node = None
    for sel in containers:
        cand = soup.select_one(sel)
        if cand and len(cand.get_text(strip=True)) > 200:
            node = cand
            break
    if not node:
        paras = [p.get_text(" ", strip=True) for p in soup.select("p") if len(p.get_text(strip=True)) > 10]
        if not paras:
            raise RuntimeError("No readable content found (might be gated).")
        body_html = "".join(f"<p>{hesc(t)}</p>" for t in paras)
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        def is_good(t: str) -> bool:
            if not t: return False
            s = t.strip()
            if s.lower().startswith("novelpia -"): return False
            if novel_title and s.strip() == novel_title.strip(): return False
            return len(s) >= 4
        final_title = list_title or (page_title if is_good(page_title) else "Chapter")
        return {"title": final_title, "html": body_html}
    _strip_comment_blocks(node)
    header_title = ""
    for sel in [".chapter-title", ".ep-title", ".title", "header h1", "h1", "h2"]:
        h = node.select_one(sel)
        if h:
            header_title = h.get_text(" ", strip=True)
            if header_title:
                break
    def is_good(t: str) -> bool:
        if not t: return False
        s = t.strip()
        if s.lower().startswith("novelpia -"): return False
        if novel_title and s.strip() == novel_title.strip(): return False
        return len(s) >= 4
    final_title = header_title if is_good(header_title) else (list_title or header_title or "Chapter")
    body_html = "".join(str(x) for x in node.contents)
    return {"title": final_title, "html": body_html}

# --------- EPUB ---------
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
    ap.add_argument("--max-chapters", type=int, default=0, help="Stop after N TOC items (0 = no cap)")
    ap.add_argument("--no-headless", action="store_true", help="Run a visible browser window (debug)")
    args = ap.parse_args()

    from urllib.parse import urlparse
    # robots soft-check (fail open if error)
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": DEFAULT_UA}) as client:
            r = await client.get(ROBOTS_URL)
            r.raise_for_status()
            path = urlparse(args.url).path or "/"
            if "Disallow: /" in r.text and path.startswith("/"):
                print("[blocked] robots.txt disallows this path.")
    except Exception:
        pass

    out_dir = Path(args.out)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=not args.no_headless,
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
        await safe_goto(page, args.url)

        novel = await extract_meta(page, args.url)
        cover_bytes = await fetch_cover_bytes_from_page(page)
        novel_slug = slugify(novel.title or "novelpia")
        book_dir = out_dir / novel_slug
        book_dir.mkdir(parents=True, exist_ok=True)

        print("[stage] collecting chapters…")
        chapters = await collect_chapters(
            page, context, novel_url=args.url,
            cap=(args.max_chapters or None),
            cookies_json=args.cookies_json,
            cookies_txt=args.cookies_txt,
        )
        print(f"[stage] collected {len(chapters)} entries from TOC")
        print(f"[ok] discovered {len(chapters)} potential entries")

        only_non_viewer = chapters and all("/viewer/" not in c.url for c in chapters)
        if not chapters or only_non_viewer:
            print("[info] TOC incomplete; trying to seed /viewer/ and walk via Next…")
            start_url = None
            for label in START_BUTTON_LABELS:
                try:
                    btn = page.get_by_text(re.compile(label, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=2500)
                        await page.wait_for_load_state("networkidle", timeout=8000)
                        await handle_consent_buttons(page)
                        start_url = page.url
                        break
                except Exception:
                    pass
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
                lim = args.max_chapters if args.max_chapters and args.max_chapters > 0 else 300
                walked = await walk_next_chapters(page, start_url=start_url, max_steps=lim)
                if walked:
                    chapters = walked
                    print(f"[ok] walked {len(chapters)} chapters via Next")

        # de-dup and cap again (safety)
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
                await safe_goto(page, ch.url)

                html = await page.content()
                data = extract_readable(html, list_title=ch.title, novel_title=novel.title)
                title = data["title"].strip() or ch.title
                payloads.append({"idx": ch.idx, "title": title, "html": data["html"]})
                await page.wait_for_timeout(THROTTLE_MS)
            except Exception as e:
                print(f"[skip] {ch.idx:03d} {ch.title}: {e}")
                await page.wait_for_timeout(THROTTLE_MS)

        # Persist metadata & chapter list
        (book_dir / "metadata.json").write_text(json.dumps(asdict(novel), ensure_ascii=False, indent=2), "utf-8")
        with (book_dir / "chapters.jsonl").open("w", encoding="utf-8") as f:
            for ch in chapters:
                f.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")

        out_path = await build_epub(novel, payloads, book_dir, cover_bytes=cover_bytes)
        print(f"[success] EPUB created at: {out_path}")

        await context.close()
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
