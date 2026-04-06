import html
import json
import os
import requests

from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from src.api import NovelpiaClient
from src.const import BASE_URL
from src.helper import ensure_dir, extract_t_token, kebab, media_type_from_ext, normalize_url
from src.novel import html_from_episode_text

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
              novel_id: Optional[int] = None, update_mode: bool = False) -> Tuple[str, str, int]:
        nv = novel["result"]["novel"]
        title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
        writers = novel["result"].get("writer_list") or []
        author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else author_fallback)
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()

        base = kebab(filename_hint or title)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)

        # Setup cache directory
        cache_dir = os.path.join(book_dir, ".raw_cache")
        if update_mode:
            ensure_dir(cache_dir)

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
            
            cdata = None
            cache_file = os.path.join(cache_dir, f"{epi_no}.json") if update_mode else None

            # Local cache check
            if update_mode and cache_file and os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                    print(f"[info] loaded cached episode {i}; {epi_title}")
                except Exception:
                    pass

            # API fetch
            if not cdata:
                print(f"[info] ticket for episode {i}; {epi_title} …")
                try:
                    tdata = client.episode_ticket(epi_no)
                except requests.HTTPError as e:
                    should_retry = False
                    try:
                        resp = getattr(e, "response", None)
                        msg = None
                        if resp is not None:
                            try:
                                body = resp.json()
                                msg = isinstance(body, dict) and body.get("errmsg")
                            except Exception:
                                msg = resp.text if hasattr(resp, "text") else None
                        if isinstance(msg, str) and "The token has expired." in msg:
                            try:
                                client.refresh()
                                should_retry = True
                            except Exception:
                                pass
                    except Exception:
                        pass
                    if should_retry:
                        try:
                            tdata = client.episode_ticket(epi_no)
                            print(f"[info] retry ticket for episode {i}; {epi_title} …")
                        except requests.HTTPError as e2:
                            print(f"[warn] episode_ticket {i}; {epi_title} failed after refresh: {e2} — skipping")
                            continue
                    else:
                        print(f"[warn] episode_ticket {i}; {epi_title} failed after retries: {e} — skipping")
                        continue

                token_t, direct_url = extract_t_token(tdata)
                if not token_t and not direct_url:
                    print(f"[warn] no _t token for episode {i}; {epi_title} (eps_no: {epi_no}) — skipping")
                    continue

                try:
                    if token_t:
                        cdata = client.episode_content(token_t)
                    else:
                        r = client.s.get(direct_url, timeout=client.timeout)
                        r.raise_for_status()
                        cdata = r.json()
                except requests.HTTPError as e:
                    should_retry = False
                    try:
                        resp = getattr(e, "response", None)
                        msg = None
                        if resp is not None:
                            try:
                                body = resp.json()
                                msg = isinstance(body, dict) and body.get("errmsg")
                            except Exception:
                                msg = resp.text if hasattr(resp, "text") else None
                        if isinstance(msg, str) and "The token has expired." in msg:
                            try:
                                client.refresh()
                                should_retry = True
                            except Exception:
                                pass
                    except Exception:
                        pass
                    if should_retry:
                        try:
                            if token_t:
                                cdata = client.episode_content(token_t)
                                print(f"[info] retry ticket for episode {i}; {epi_title} …")
                            else:
                                r = client.s.get(direct_url, timeout=client.timeout)
                                r.raise_for_status()
                                cdata = r.json()
                        except requests.HTTPError as e2:
                            print(f"[warn] content fetch failed after refresh for episode {i}; {epi_title} (eps_no: {epi_no}): {e2}")
                            continue
                    else:
                        print(f"[warn] content fetch failed for episode {i}; {epi_title} (eps_no: {epi_no}: {e}")
                        continue
                
                if update_mode and cdata and cache_file:
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(cdata, f, ensure_ascii=False)
                    except Exception:
                        pass

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

        out_path = os.path.join(book_dir, f"{base}.epub")
        epub.write_epub(out_path, book, {})
        
        return out_path, title, len(episodes)