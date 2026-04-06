import requests

from bs4 import BeautifulSoup
from src.helper import extract_t_token, normalize_url

# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------

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

def fetch_novel_and_episodes(client, novel_id, max_chapters=None):
    # Auth check
    try:
        res = client.me()
        if str(res.get("statusCode")) == "200":
            mem = (((res.get("result") or {}).get("login") or {}).get("mem_nick")) or "Unknown"
            print(f"[auth] Logged in as: {mem}")
    except Exception:
        pass

    print("[info] extracting metadata…")
    data_novel = client.novel(novel_id)

    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{novel_id}")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0

    rows = int(epi_cnt) if epi_cnt else 1000
    data_list = client.episode_list(novel_id, rows=rows)
    ep_list = data_list["result"].get("list", [])

    if max_chapters:
        ep_list = ep_list[:int(max_chapters)]

    return data_novel, ep_list, title

def fetch_episode_content(client, ep, debug_dump=False, out_dir=None, idx=None):
    epi_no = int(ep.get("episode_no"))
    epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"

    print(f"[info] ticket for episode {idx}; {epi_title} …")

    # --- ticket ---
    try:
        tdata = client.episode_ticket(epi_no)
    except requests.HTTPError as e:
        try:
            client.refresh()
            tdata = client.episode_ticket(epi_no)
        except Exception:
            print(f"[warn] ticket failed: {epi_title}")
            return None, epi_title

    token_t, direct_url = extract_t_token(tdata)
    if not token_t and not direct_url:
        return None, epi_title

    # --- content ---
    try:
        if token_t:
            cdata = client.episode_content(token_t)
        else:
            r = client.s.get(direct_url, timeout=client.timeout)
            r.raise_for_status()
            cdata = r.json()
    except requests.HTTPError:
        try:
            client.refresh()
            if token_t:
                cdata = client.episode_content(token_t)
            else:
                r = client.s.get(direct_url, timeout=client.timeout)
                r.raise_for_status()
                cdata = r.json()
        except Exception:
            return None, epi_title

    # --- extract html ---
    result_block = cdata.get("result", {})
    data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

    parts = []
    for k in sorted([k for k in data_block if str(k).startswith("epi_content")]):
        v = data_block.get(k)
        if isinstance(v, str):
            parts.append(v)

    html_text = "".join(parts).strip()

    if not html_text:
        html_text = (
            result_block.get("content")
            or result_block.get("html")
            or result_block.get("text")
            or cdata.get("content")
            or ""
        )

    return html_from_episode_text(html_text), epi_title