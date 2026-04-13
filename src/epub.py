import html
import json
import os
import requests
import time

from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from tqdm import tqdm
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
        for attempt in range(1, 4):
            try:
                resp = client.s.get(url, timeout=client.timeout)
                if resp.status_code == 429:
                    wait = 2.0 * attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.content
            except Exception:
                if attempt < 3:
                    time.sleep(1.0)
                continue
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

        # Fetch episodes in parallel
        pbar = tqdm(total=len(episodes), desc="Fetching chapters", unit="chap")
        
        def update_pbar():
            pbar.update(1)

        fetched_results = client.fetch_episodes_parallel(episodes, progress_cb=update_pbar)
        pbar.close()

        for i, res in enumerate(fetched_results, 1):
            if not res or "error" in res:
                err = res.get("error") if res else "Unknown error"
                print(f"[warn] Failed to fetch chapter {i}: {err}")
                continue

            html_text = res["html"]
            epi_title = res["epi_title"]
            
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

        base = kebab(filename_hint or title)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)
        out_path = os.path.join(book_dir, f"{base}.epub")
        epub.write_epub(out_path, book, {})
        return out_path, title, len(episodes)
