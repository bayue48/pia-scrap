import os

from bs4 import BeautifulSoup
from tqdm import tqdm
from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_novel_and_episodes

# ----------------------------
# Main Build Function
# ----------------------------

def build_epub(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None, language="en", debug_dump=False):
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    return builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
    )

def build_txt(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None, language="en", debug_dump=False):
    _, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

    total = 0
    pbar = tqdm(total=len(ep_list), desc="Exporting TXT", unit="chap")

    def update_pbar():
        pbar.update(1)

    fetched_results = client.fetch_episodes_parallel(ep_list, progress_cb=update_pbar)
    pbar.close()

    for i, res in enumerate(fetched_results, 1):
        if not res or "error" in res:
            err = res.get("error") if res else "Unknown error"
            print(f"[warn] Failed to fetch chapter {i}: {err}")
            continue

        html_text = res["html"]
        epi_title = res["epi_title"]

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)

        total += 1

    return book_dir, title, total