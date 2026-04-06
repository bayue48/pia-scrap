import argparse
import sys
import time
import requests

from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.helper import load_config, save_config, parse_range

# ----------------------------
# Main Function
# ----------------------------

def main():
    ap = argparse.ArgumentParser(description="Novelpia → EPUB packer (API)")
    ap.add_argument("novel_ids", help="Novel ID (e.g. 1072) or Range (e.g. 1000-1050)")
    ap.add_argument("--user", "--email", "-u", "-e", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("--pass", "--password", "-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--max-chapters", "-max", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("--lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("--proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("--debug", "-v", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics")
    ap.add_argument("--throttle", type=float, default=2.0, help="Seconds delay between episode requests (default: 2.0)")
    ap.add_argument("--txt", "-txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    ap.add_argument("--update", action="store_true", help="Only download new chapters and update existing EPUB via local cache")
    args = ap.parse_args()

    global HTTP_LOG
    HTTP_LOG = bool(args.debug)

    target_ids = parse_range(args.novel_ids)
    print(f"[info] Queue size: {len(target_ids)} novels")

    cfg = load_config()
    cfg_login_at = (cfg.get("login_at") or "").strip() or None
    cfg_userkey = (cfg.get("userkey") or "").strip() or None
    cfg_tkey = (cfg.get("tkey") or "").strip() or None

    # Priority: CLI credentials > stored tokens > error
    if args.email and args.password:
        client = NovelpiaClient(email=args.email, password=args.password, proxy=args.proxy,
                                throttle=args.throttle, userkey=cfg_userkey, tkey=cfg_tkey)
        client.login()
        # Persist/refresh tokens after successful login
        userkey_val = None
        tkey_val = None
        try:
            for c in client.s.cookies:
                if c.name == "USERKEY":
                    userkey_val = c.value
                elif c.name == "TKEY":
                    tkey_val = c.value
        except Exception as e:
            print(f"Error occurred while fetching cookies: {e}")
            pass
        save_config({
            "login_at": client.tokens.login_at,
            "userkey": userkey_val or cfg_userkey or "",
            "tkey": tkey_val or client.tokens.tkey or cfg_tkey or "",
        })
    elif cfg_login_at and cfg_userkey:
        client = NovelpiaClient(email=None, password=None, proxy=args.proxy,
                                throttle=args.throttle, userkey=cfg_userkey, tkey=cfg_tkey)
        client.tokens.login_at = cfg_login_at
    else:
        print("[error] No credentials or stored tokens found. Provide --user and --pass to login once.")
        sys.exit(2)

    # Processing loop
    success_count = 0
    fail_count = 0
    skipped_count = 0

    for idx, novel_id in enumerate(target_ids):
        print(f"\n--- Processing ID {novel_id} ({idx+1}/{len(target_ids)}) ---")
        try:
            if args.txt:
                out_dir_final, title, count = build_txt(
                    client, novel_id, args.out,
                    max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
                    language=args.lang, debug_dump=args.debug,
                )
                print(f"[success] Wrote TXT files under: {out_dir_final}  |  Title: {title}  |  Chapters: {count}")
                success_count += 1
            else:
                out_file, title, count = build_epub(
                    client, novel_id, args.out,
                    max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
                    language=args.lang, debug_dump=args.debug,
                    update_mode=args.update
                )
                
                if out_file is None:
                    skipped_count += 1
                else:
                    print(f"[success] Wrote EPUB: {out_file}  |  Title: {title}  |  Chapters: {count}")
                    success_count += 1

        except Exception as e:
            err_str = str(e)
            if "NoneType" in err_str or "KeyError" in err_str:
                print(f"[-] Novel {novel_id} likely does not exist or has no data. Skipping.")
            elif isinstance(e, requests.HTTPError) and e.response.status_code == 404:
                print(f"[-] Novel {novel_id} returned 404. Skipping.")
            else:
                print(f"[error] Failed processing {novel_id}: {e}")
            
            fail_count += 1
            time.sleep(1.0)

    print(f"\n[done] Finished range. Success: {success_count}, Skipped (Up to date): {skipped_count}, Failed/No Data: {fail_count}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)