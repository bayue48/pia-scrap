import os
import json

from bs4 import BeautifulSoup
from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_episode_content, fetch_novel_and_episodes

# ----------------------------
# Main Build Function
# ----------------------------

def build_epub(client, novel_id, out_dir, max_chapters=None, language="en", debug_dump=False, update_mode=False):
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    # Update mode skip check 
    if update_mode:
        base = kebab(title)
        book_dir = os.path.join(out_dir, base)
        meta_path = os.path.join(book_dir, "metadata.json")
        
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    existing_chapters = meta.get("chapter", 0)
                    target_chapters = len(ep_list)
                        
                    if existing_chapters >= target_chapters and target_chapters > 0:
                        print(f"[info] '{title}' is already up to date ({existing_chapters} chapters). Skipping API fetch.")
                        return None, title, existing_chapters
            except Exception:
                pass

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    out_file, title, count = builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        update_mode=update_mode
    )

    # Metadata persistence 
    try:
        book_dir = os.path.dirname(out_file)
        nv = data_novel.get("result", {}).get("novel", {})
        tag_items = (data_novel.get("result", {}).get("tag_list") or nv.get("tag_list") or [])
        tags = []
        
        for t in tag_items:
            if isinstance(t, str):
                tags.append(t)
            elif isinstance(t, dict):
                val = t.get("tag_name") or t.get("name") or t.get("title")
                if isinstance(val, str):
                    tags.append(val)
        
        seen = set()
        uniq_tags = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                uniq_tags.append(t)

        meta = {
            "url": f"https://global.novelpia.com/novel/{novel_id}",
            "title": nv.get("novel_name") or title,
            "tags": uniq_tags,
            "chapter": len(ep_list),
            "description": nv.get("novel_story") or "",
        }
        
        meta_path = os.path.join(book_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    except Exception:
        pass

    return out_file, title, count

def build_txt(client, novel_id, out_dir, max_chapters=None, language="en", debug_dump=False):
    _, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

    total = 0

    for i, ep in enumerate(ep_list, 1):
        html_text, epi_title = fetch_episode_content(client, ep, idx=i)

        if not html_text:
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)

        total += 1

    return book_dir, title, total