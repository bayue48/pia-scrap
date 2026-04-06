
import os

from bs4 import BeautifulSoup

from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_episode_content, fetch_novel_and_episodes

# ----------------------------
# Main Build Function
# ----------------------------

def build_epub(client, novel_id, out_dir, max_chapters=None, language="en", debug_dump=False):
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, max_chapters)

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    return builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
    )

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