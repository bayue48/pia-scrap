import argparse
import random
import uuid
import base64
import html
import json
import os
import re
import sys
import time
import requests
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse, parse_qs
from requests.exceptions import RequestException
from ebooklib import epub
from bs4 import BeautifulSoup

# ----------------------------
# Constants & Helpers
# ----------------------------

BASE_URL = "https://global.novelpia.com"
API_BASE = "https://api-global.novelpia.com"
IMG_BASE_HTTPS = "https:"
HTTP_LOG = False 
CONFIG_PATH = os.path.join(os.path.dirname(__file__), ".api.json")

SESSION_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "x-requested-with": "XMLHttpRequest",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-fetch-dest": "empty",
    "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip()) or "book"

def normalize_url(u: str) -> str:
    if not u:
        return u
    if u.startswith("//"):
        return IMG_BASE_HTTPS + u
    if u.startswith("/"):
        return urljoin(BASE_URL, u)
    return u

def media_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"

def looks_like_jwt(token: Optional[str]) -> bool:
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    for p in parts:
        try:
            base64.urlsafe_b64decode(p + "===")
        except Exception:
            return False
    return True

def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")

    # normalize images
    for img in soup.find_all("img"):
        if img.get("data-src") and not img.get("src"):
            img["src"] = img["data-src"]
        if "style" in img.attrs:
            del img["style"]
        if img.get("src"):
            img["src"] = normalize_url(img["src"])

    # Ensure document wrapper
    if not soup.find("html"):
        html_tag = soup.new_tag("html")
        head = soup.new_tag("head")
        meta = soup.new_tag("meta", charset="utf-8")
        head.append(meta)
        body = soup.new_tag("body")
        for el in list(soup.children):
            body.append(el.extract())
        html_tag.append(head)
        html_tag.append(body)
        soup.append(html_tag)

    return str(soup)

# ----------------------------
# Resilient request layer
# ----------------------------

def merge_login_at(headers: dict, login_at: Optional[str]) -> dict:
    h = dict(headers or {})
    if login_at:
        h["login-at"] = login_at
    return h

def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None):
    """Generic request wrapper: retries on 5xx and network issues.
    If allow_refresh and body says token expired, call refresh_fn() once and retry.
    """
    def _mask_value(v: Any) -> Any:
        try:
            if isinstance(v, dict):
                return {k: _mask_value(v2) for k, v2 in v.items()}
            if isinstance(v, list):
                return [_mask_value(x) for x in v]
            if isinstance(v, str):
                low = v.lower()
                # Mask long tokens / JWT-like
                if low.count(".") == 2 and all(len(p) > 5 for p in v.split(".")):
                    parts = v.split(".")
                    return parts[0][:6] + "..." + parts[-1][-6:]
                if len(v) > 64:
                    return v[:32] + "…(trunc)"
                return v
            return v
        except Exception:
            return "<masked>"

    def _mask_kv(d: Optional[dict]) -> Optional[dict]:
        if not isinstance(d, dict):
            return d
        out = {}
        for k, v in d.items():
            kl = str(k).lower()
            if any(x in kl for x in ("pass", "passwd", "password", "authorization", "token", "login-at", "login_at", "_t")):
                out[k] = "***"
            else:
                out[k] = _mask_value(v)
        return out

    def _j(x: Any) -> str:
        try:
            return json.dumps(x, ensure_ascii=False)
        except Exception:
            return str(x)

    attempt = 0
    last_exc = None
    while attempt < max_retries:
        attempt += 1
        try:
            if HTTP_LOG:
                print(f"[api] -> {method} {url} (attempt {attempt}/{max_retries})")
                if headers:
                    print(f"[api]    headers: {_j(_mask_kv(headers))}")
                if params:
                    print(f"[api]    params:  {_j(_mask_kv(params))}")
                if json is not None:
                    print(f"[api]    json:    {_j(_mask_kv(json))}")
                if data is not None and not json:
                    # data may be bytes/str; keep short
                    d = data if isinstance(data, (str, bytes)) else _j(_mask_kv(data))
                    if isinstance(d, bytes):
                        d = d[:128] + b"..." if len(d) > 128 else d
                        try:
                            d = d.decode("utf-8", "ignore")
                        except Exception:
                            d = "<bytes>"
                    print(f"[api]    data:    {d if isinstance(d, str) else str(d)}")

            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
            if allow_refresh:
                try:
                    body = r.json()
                    if body.get("errmsg") == "The token has expired." and refresh_fn:
                        refresh_fn()
                        r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
                except Exception:
                    pass
            if HTTP_LOG:
                body_preview = None
                try:
                    t = r.text
                    body_preview = (t[:500] + "…") if len(t) > 500 else t
                except Exception:
                    body_preview = "<non-text body>"
                print(f"[api] <- {r.status_code} {r.reason} from {r.url}")
                if body_preview is not None:
                    print(f"[api]    body: {body_preview}")
            if r.status_code >= 500 and attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            return r
        except RequestException as e:
            if HTTP_LOG:
                print(f"[api] !! {method} {url} failed on attempt {attempt}: {e}")
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return r

# ----------------------------
# API Client
# ----------------------------

@dataclass
class Tokens:
    login_at: Optional[str] = None

class NovelpiaClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: int = 30, throttle: float = 2.0,
                 userkey: Optional[str] = None):
        self.s = requests.Session()
        self.s.headers.update(SESSION_HEADERS.copy())
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        # delay seconds between episode-related API calls to reduce 429/500 rate limits
        self.throttle = max(0.0, float(throttle or 0.0))
        try:
            if not userkey:
                userkey = uuid.uuid4().hex
            self.s.cookies.set("USERKEY", userkey, domain=".novelpia.com", path="/")
        except Exception:
            pass

    def login(self):
        url = f"{API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s, "POST", url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        data = r.json()
        self.tokens.login_at = data["result"]["LOGINAT"]

    def refresh(self):
        url = f"{API_BASE}/v1/login/refresh"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        self.tokens.login_at = r.json()["result"]["LOGINAT"]

    def me(self) -> Dict:
        url = f"{API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, allow_refresh=True, refresh_fn=self.refresh,
        )
        r.raise_for_status()
        return r.json()

    def novel(self, novel_id: int) -> Dict:
        url = f"{API_BASE}/v1/novel"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id},
            timeout=self.timeout, allow_refresh=True, refresh_fn=self.refresh,
        )
        r.raise_for_status()
        return r.json()

    def episode_list(self, novel_id: int, rows: int) -> Dict:
        url = f"{API_BASE}/v1/novel/episode/list"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            timeout=self.timeout, allow_refresh=True, refresh_fn=self.refresh,
        )
        r.raise_for_status()
        return r.json()

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        # Throttle before hitting ticket endpoint to avoid rate limits
        if self.throttle:
            time.sleep(self.throttle + random.uniform(0.05, 0.25))
        r = request_with_retries(
            self.s, "GET", url,
            headers=headers, params=params,
            timeout=self.timeout, allow_refresh=True, refresh_fn=self.refresh, max_retries=4,
        )
        r.raise_for_status()
        return r.json()

    def episode_content(self, token_t: str) -> Dict:
        url = f"{API_BASE}/v1/novel/episode/content"
        # Throttle content fetch too, to be safe
        if self.throttle:
            time.sleep(self.throttle + random.uniform(0.05, 0.25))
        r = request_with_retries(
            self.s, "GET", url,
            params={"_t": token_t},
            timeout=self.timeout, max_retries=3,
        )
        r.raise_for_status()
        return r.json()

# ----------------------------
# Token extraction (STRICT)
# ----------------------------

def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)

def extract_t_token(tdata: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, direct_content_url_or_none).
    Prefer JWT-like tokens, but accept any non-empty string if present.
    If using URL, accept any _t value on the official content endpoint.
    """
    res = tdata.get("result", {}) if isinstance(tdata, dict) else {}
    fallback_token: Optional[str] = None

    # 1) common keys at result
    for k in ("_t", "t", "token"):
        v = res.get(k)
        if isinstance(v, str) and v:
            if looks_like_jwt(v):
                return v, None
            fallback_token = fallback_token or v

    # 2) nested dicts under result
    if isinstance(res, dict):
        for _, v in res.items():
            if isinstance(v, dict):
                for k in ("_t", "t", "token"):
                    vv = v.get(k)
                    if isinstance(vv, str) and vv:
                        if looks_like_jwt(vv):
                            return vv, None
                        fallback_token = fallback_token or vv

    # 3) URL that is the official content endpoint with any _t
    for s in iter_strings(tdata):
        if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
            try:
                p = urlparse(s)
                if p.netloc.endswith("api-global.novelpia.com") and p.path.endswith("/v1/novel/episode/content"):
                    q = parse_qs(p.query)
                    cand = (q.get("_t") or [None])[0]
                    if isinstance(cand, str) and cand:
                        if looks_like_jwt(cand):
                            return cand, s
                        # fallback
                        fallback_token = fallback_token or cand
            except Exception:
                pass
    if fallback_token:
        return fallback_token, None
    return None, None

# ----------------------------
# EPUB Builder
# ----------------------------

class EpubBuilder:
    def __init__(self, out_dir: str, debug_dump: bool = False):
        self.out_dir = out_dir
        self.debug_dump = debug_dump
        ensure_dir(out_dir)

    def _fetch_bytes(self, client: NovelpiaClient, url: str) -> Optional[bytes]:
        try:
            resp = client.s.get(url, timeout=client.timeout)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    def build(self, client: NovelpiaClient, novel: Dict, episodes: List[Dict],
              filename_hint: Optional[str] = None, language: str = "en",
              author_fallback: str = "Unknown", css_text: Optional[str] = None,
              novel_id: Optional[int] = None) -> str:
        nv = novel["result"]["novel"]
        title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
        writers = novel["result"].get("writer_list") or []
        author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else author_fallback)
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        # Cover
        cover_url = normalize_url(nv.get("novel_full_img") or nv.get("novel_img") or "")
        cover_bytes = self._fetch_bytes(client, cover_url) if cover_url else None
        has_cover = False
        if cover_bytes:
            book.set_cover("cover.jpg", cover_bytes)
            has_cover = True

        # CSS
        default_css = css_text or (
            """
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial; line-height: 1.6; }
            h1, h2, h3 { page-break-after: avoid; }
            img { max-width: 100%; height: auto; }
            .epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
            """
        )
        style = epub.EpubItem(uid="style", file_name="style/main.css",
                              media_type="text/css", content=default_css.encode("utf-8"))
        book.add_item(style)

        spine: List = ["nav"]
        toc: List = []
        image_cache: Dict[str, str] = {}
        img_index = 1

        def add_images_and_rewrite(html_str: str) -> Tuple[str, List[epub.EpubItem]]:
            nonlocal img_index
            soup = BeautifulSoup(html_str, "html.parser")
            added_items: List[epub.EpubItem] = []

            for img in soup.find_all("img"):
                src = img.get("src")
                if not src:
                    continue
                src = normalize_url(src)
                if src in image_cache:
                    img["src"] = image_cache[src]
                    continue

                path = urlparse(src).path
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    ext = ".jpg"

                img_bytes = self._fetch_bytes(client, src)
                if not img_bytes:
                    # leave external
                    continue

                fname = f"images/img_{img_index:05d}{ext}"
                image_cache[src] = fname
                img_index += 1

                item = epub.EpubItem(uid=f"img{img_index}", file_name=fname,
                                     media_type=media_type_from_ext(ext), content=img_bytes)
                added_items.append(item)
                img["src"] = fname

            return str(soup), added_items

        for i, ep in enumerate(episodes, 1):
            epi_no = int(ep["episode_no"])
            epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
            print(f"[info] ticket for episode {epi_title} …")
            try:
                tdata = client.episode_ticket(epi_no)
            except requests.HTTPError as e:
                print(f"[warn] episode_ticket {epi_title} failed after retries: {e} — skipping")
                if self.debug_dump:
                    with open(os.path.join(self.out_dir, f"ticket_{epi_no}.json"), "w", encoding="utf-8") as f:
                        f.write(getattr(e, "response", None) and getattr(e.response, "text", "") or "")
                continue

            token_t, direct_url = extract_t_token(tdata)
            if not token_t and not direct_url:
                print(f"[warn] no _t token for episode {epi_no} (title: {epi_title}) — skipping")
                if self.debug_dump:
                    with open(os.path.join(self.out_dir, f"ticket_{epi_no}.json"), "w", encoding="utf-8") as f:
                        json.dump(tdata, f, ensure_ascii=False, indent=2)
                continue

            # Fetch content
            try:
                if token_t:
                    cdata = client.episode_content(token_t)
                else:
                    r = client.s.get(direct_url, timeout=client.timeout)
                    r.raise_for_status()
                    cdata = r.json()
            except requests.HTTPError as e:
                print(f"[warn] content fetch failed for {epi_no}: {e}")
                if self.debug_dump:
                    with open(os.path.join(self.out_dir, f"content_{epi_no}.json"), "w", encoding="utf-8") as f:
                        f.write(getattr(e, "response", None) and getattr(e.response, "text", "") or "")
                continue

            # Prefer result.data.epi_content* fields and concatenate
            result_block = (cdata.get("result", {}) or {})
            data_block = (result_block.get("data") or {}) if isinstance(result_block, dict) else {}
            parts = []
            try:
                # natural sort by numeric suffix, epi_content, epi_content2, epi_content3...
                def _key(k: str):
                    import re as _re
                    m = _re.search(r"(\d+)$", k)
                    return (0 if k == "epi_content" else 1, int(m.group(1)) if m else 0)
                for k in sorted([kk for kk in data_block.keys() if str(kk).startswith("epi_content")], key=_key):
                    v = data_block.get(k)
                    if isinstance(v, str) and v:
                        parts.append(v)
            except Exception:
                pass
            html_text = "".join(parts).strip()
            if not html_text:
                # fallbacks
                html_text = (
                    result_block.get("content")
                    or result_block.get("html")
                    or result_block.get("text")
                    or cdata.get("content")
                    or ""
                )

            html_text = html_from_episode_text(html_text)
            html_text, new_imgs = add_images_and_rewrite(html_text)

            chapter = epub.EpubHtml(
                title=epi_title,
                file_name=f"chap_{i:04d}.xhtml",
                lang=language,
                content=(
                    f"<html xmlns=\"http://www.w3.org/1999/xhtml\">"
                    f"<head><title>{html.escape(epi_title)}</title>"
                    f"<link rel=\"stylesheet\" href=\"style/main.css\"/></head>"
                    f"<body><h2 class=\"epi-title\">{html.escape(epi_title)}</h2>{html_text}</body></html>"
                ),
            )

            book.add_item(chapter)
            spine.append(chapter)
            toc.append(chapter)

            for item in new_imgs:
                book.add_item(item)

        # About / metadata page
        src_url = f"{BASE_URL}/novel/{novel_id}" if novel_id else ""
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append("<p><img src='cover.jpg' alt='Cover' style='width:230px;max-width:90%;height:auto;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>")
        meta_parts.append(f"<p><strong>Author:</strong> {html.escape(author)}</p>")
        meta_parts.append(f"<p><strong>Chapters:</strong> {len(episodes)}</p>")
        meta_parts.append(f"<p><strong>Status:</strong> {html.escape(status)}</p>")
        if src_url:
            meta_parts.append(f"<p><strong>Source:</strong> <a href='{src_url}'>{src_url}</a></p>")
        if description:
            meta_parts.append(f"<p>{html.escape(description)}</p>")
        meta_html = (
            "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>"
             + "".join(meta_parts) + "</body></html>"
        )
        about = epub.EpubHtml(title="About", file_name="about.xhtml", lang=language, content=meta_html)
        book.add_item(about)
        spine.insert(1, about)
        toc.insert(0, about)

        # TOC, NCX, Nav
        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Spine & CSS
        book.spine = spine

        # kebab-case, lowercase filename
        import re as _re
        def _kebab(s: str) -> str:
            s = (s or "").lower()
            s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
            return s or "book"
        base = _kebab(filename_hint or title)
        out_path = os.path.join(self.out_dir, f"{base}.epub")
        epub.write_epub(out_path, book, {})
        return out_path

# ----------------------------
# Orchestration
# ----------------------------

def fetch_and_build(client: NovelpiaClient, novel_id: int, out_dir: str,
                    max_chapters: Optional[int], language: str,
                    debug_dump: bool = False):
    # Confirm token
    _ = client.me()

    # Novel data
    data_novel = client.novel(novel_id)
    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{novel_id}")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0

    # Episode list
    rows = int(epi_cnt) if epi_cnt else 1000
    data_list = client.episode_list(novel_id, rows=rows)
    ep_list = data_list["result"].get("list", [])

    # Optional cap on number of chapters (ascending order)
    if max_chapters is not None and max_chapters > 0:
        ep_list = ep_list[: int(max_chapters)]

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)
    out_file = builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
    )
    return out_file, title, len(ep_list)

# ----------------------------
# Main
# ----------------------------

def main():
    ap = argparse.ArgumentParser(description="Novelpia → EPUB packer (API)")
    ap.add_argument("novel_id", type=int, help="novel_no (e.g., 1072)")
    ap.add_argument("--email", help="Novelpia email (optional if config has tokens)")
    ap.add_argument("--password", help="Novelpia password (optional if config has tokens)")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--max-chapters", "-max", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("--lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("--proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("--debug", "-v", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics")
    ap.add_argument("--throttle", type=float, default=2.0, help="Seconds delay between episode requests (default: 2.0)")
    args = ap.parse_args()

    global HTTP_LOG
    HTTP_LOG = bool(args.debug)

    # Config helpers
    def load_config() -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception:
            return {}
        return {}

    def save_config(cfg: Dict[str, Any]) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    cfg = load_config()
    cfg_login_at = (cfg.get("login_at") or "").strip() or None
    cfg_userkey = (cfg.get("userkey") or "").strip() or None

    # Prefer tokens from config if present
    if cfg_login_at and cfg_userkey:
        client = NovelpiaClient(email=None, password=None, proxy=args.proxy, throttle=args.throttle, userkey=cfg_userkey)
        client.tokens.login_at = cfg_login_at
    else:
        # Need credentials for first run
        if not args.email or not args.password:
            print("[error] No stored tokens found. Please run once with --email and --password to login.")
            sys.exit(2)
        client = NovelpiaClient(email=args.email, password=args.password, proxy=args.proxy, throttle=args.throttle, userkey=cfg_userkey)
        client.login()
        # Persist tokens for next run
        userkey_val = None
        try:
            for c in client.s.cookies:
                if c.name == "USERKEY":
                    userkey_val = c.value
                    break
        except Exception:
            pass
        save_config({"login_at": client.tokens.login_at, "userkey": userkey_val or cfg_userkey or ""})

    try:
        out_file, title, count = fetch_and_build(
            client, args.novel_id, args.out,
            max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
            language=args.lang, debug_dump=args.debug
        )
    except requests.HTTPError as e:
        print(f"[error] HTTP error: {e} | body: {getattr(e, 'response', None) and getattr(e.response, 'text', '')}")
        sys.exit(1)

    print(f"[success] Wrote EPUB: {out_file}  |  Title: {title}  |  Chapters: {count}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
