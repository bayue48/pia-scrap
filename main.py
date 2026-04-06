import argparse
import sys

from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.helper import load_config, save_config

# ----------------------------
# Main Function
# ----------------------------

def main():
    ap = argparse.ArgumentParser(description="Novelpia → EPUB packer (API)")
    ap.add_argument("novel_id", type=int, help="novel_no (e.g., 1072)")
    ap.add_argument("--user", "--email", "-u", "-e", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("--pass", "--password", "-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--max-chapters", "-max", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("--lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("--proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("--debug", "-v", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics")
    ap.add_argument("--throttle", type=float, default=2.0, help="Seconds delay between episode requests (default: 2.0)")
    ap.add_argument("--txt", "-txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    args = ap.parse_args()

    global HTTP_LOG
    HTTP_LOG = bool(args.debug)

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

    try:
        if args.txt:
            out_dir_final, title, count = build_txt(
                client, args.novel_id, args.out,
                max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
                language=args.lang, debug_dump=args.debug,
            )
            print(f"[success] Wrote TXT files under: {out_dir_final}  |  Title: {title}  |  Chapters: {count}")
        else:
            out_file, title, count = build_epub(
                client, args.novel_id, args.out,
                max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
                language=args.lang, debug_dump=args.debug
            )
            print(f"[success] Wrote EPUB: {out_file}  |  Title: {title}  |  Chapters: {count}")
    except Exception as e:
        print(f"[error] Failed to build novel: {e}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
