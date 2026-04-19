import json
import os
import random
import time
import uuid
import requests
import concurrent.futures
import re as _re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from src import const
from src.helper import j, mask_kv, attach_auth_cookies, merge_login_at
from src.helper import extract_t_token
from src.novel import html_from_episode_text

# ----------------------------
# API Client
# ----------------------------

@dataclass
class Tokens:
    login_at: Optional[str] = None
    tkey: Optional[str] = None
    userkey: Optional[str] = None

class NovelpiaClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: int = 30, throttle: float = 1.5,
                 userkey: Optional[str] = None, tkey: Optional[str] = None):
        self.s = requests.Session()
        self.s.headers.update(const.SESSION_HEADERS.copy())
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        # delay seconds between episode-related API calls to reduce 429/500 rate limits
        self.throttle = max(0.0, float(throttle or 1.5))
        try:
            if not userkey:
                userkey = uuid.uuid4().hex
            self.s.cookies.set("USERKEY", userkey, domain=".novelpia.com", path="/")
            self.tokens.userkey = userkey
            if tkey:
                self.s.cookies.set("TKEY", tkey, domain=".novelpia.com", path="/")
                self.tokens.tkey = tkey
        except Exception as e:
            print(f"Error setting cookies: {e}")

    def login(self):
        url = f"{const.API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s, "POST", url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        data = r.json()
        self.tokens.login_at = data["result"]["LOGINAT"]
        # Capture cookies after successful login
        try:
            for c in self.s.cookies:
                if c.name == "TKEY":
                    self.tokens.tkey = c.value
                elif c.name == "USERKEY":
                    self.tokens.userkey = c.value
        except Exception:
            pass

    def refresh(self) -> Optional[str]:
        url = f"{const.API_BASE}/v1/login/refresh"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        self.tokens.login_at = r.json()["result"]["LOGINAT"]
        # Persist refreshed token to config
        try:
            cfg: Dict[str, Any] = {}
            if os.path.exists(const.CONFIG_PATH):
                try:
                    with open(const.CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception as e:
                    print(f"Error loading config: {e}")
                    cfg = {}
            cfg["login_at"] = self.tokens.login_at
            with open(const.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                pass
        except Exception as e:
            print(f"Error saving config: {e}")
            pass
        return self.tokens.login_at

    def _on_rate_limit(self):
        """Increase throttle when 429 occurs."""
        old = self.throttle
        self.throttle = min(15.0, self.throttle + 1.5)
        if const.HTTP_LOG:
            print(f"[api] Increased throttle from {old}s to {self.throttle}s due to rate limit.")

    def me(self) -> Dict:
        url = f"{const.API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def novel(self, novel_id: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id},
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def episode_list(self, novel_id: int, rows: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/list"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        # Throttle before hitting ticket endpoint to avoid rate limits
        if self.throttle:
            time.sleep(self.throttle + random.uniform(1.0, 1.5))
        r = request_with_retries(
            self.s, "GET", url,
            headers=headers, params=params,
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit, max_retries=4,
        )
        r.raise_for_status()
        return r.json()

    def episode_content(self, token_t: str) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/content"
        # Throttle content fetch too, to be safe
        if self.throttle:
            time.sleep(self.throttle + random.uniform(1.0, 1.5))
        r = request_with_retries(
            self.s, "GET", url,
            params={"_t": token_t},
            timeout=self.timeout, max_retries=3,
            allow_refresh=True, refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def fetch_episode(self, ep: Dict, idx: int = 0) -> Dict:
        """Fetch ticket and content for a single episode."""
        episode_no = ep.get("episode_no")
        if episode_no is None:
            return {
                "error": "missing episode_no",
                "epi_no": None,
                "epi_title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                "idx": idx,
            }
        epi_no = int(episode_no)
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        
        # 1) Ticket
        try:
            tdata = self.episode_ticket(epi_no)
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        token_t, direct_url = extract_t_token(tdata)
        if not token_t and not direct_url:
            return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 2) Content
        try:
            if token_t:
                cdata = self.episode_content(token_t)
            else:
                assert direct_url is not None, "direct_url unavailable"
                r = self.s.get(direct_url, timeout=self.timeout)
                r.raise_for_status()
                cdata = r.json()
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 3) Extract HTML
        result_block = cdata.get("result", {})
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        try:
            def _key(k: str):
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
            html_text = (
                result_block.get("content")
                or result_block.get("html")
                or result_block.get("text")
                or cdata.get("content")
                or ""
            )

        return {
            "html": html_from_episode_text(html_text),
            "epi_title": epi_title,
            "epi_no": epi_no,
            "idx": idx,
        }

    def fetch_episodes_parallel(self, ep_list: List[Dict[str, Any]], max_workers: int = 3, progress_cb=None) -> List[Dict[str, Any]]:
        """Fetch multiple episodes in parallel."""
        results: List[Dict[str, Any]] = [{} for _ in range(len(ep_list))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.fetch_episode, ep, i+1): i 
                for i, ep in enumerate(ep_list)
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    results[idx] = res
                except Exception as e:
                    results[idx] = {"error": str(e), "idx": idx+1}
                if progress_cb:
                    progress_cb()
        return results

def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None,
                          login_fn=None, on_rate_limit=None):
    """Generic request wrapper: retries on 5xx, 429, and network issues.
    If allow_refresh is True and the response indicates an expired token, invoke
    refresh_fn() followed by login_fn() if needed, then retry.
    """
    attempt = 0
    last_exc = None
    did_refresh = False
    did_login = False
    while attempt < max_retries:
        attempt += 1
        try:
            # Inject Cookie header (except for login endpoint) using session cookies
            try:
                if "/v1/member/login" not in url:
                    attach_auth_cookies(session, headers)
            except Exception as e:
                print(f"Error occurred while attaching auth cookies: {e}")
                pass

            if const.HTTP_LOG:
                print(f"[api]   -> {method} {url} (attempt {attempt}/{max_retries})")
                try:
                    eff_headers = {}
                    try:
                        eff_headers.update(getattr(session, "headers", {}) or {})
                    except Exception as e:
                        print(f"Error occurred while fetching session headers: {e}")
                        pass
                    if headers:
                        eff_headers.update(headers)
                except Exception as e:
                    print(f"[api]   req-headers: <unavailable> ({e})")
                if params:
                    print(f"[api]   params:  {j(mask_kv(params))}")
                if json is not None:
                    print(f"[api]   json:    {j(mask_kv(json))}")

            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)

            if const.HTTP_LOG and r.status_code != 200:
                print(f"[api]   <- {r.status_code} {r.reason} from {r.url}")
                print(f"[api]   <- Response content: {r.text}")
            
            # Handle rate limiting (429)
            if r.status_code == 429:
                if on_rate_limit:
                    on_rate_limit()
                wait = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                if const.HTTP_LOG:
                    print(f"[api] !! Rate limit (429) hit. Waiting {wait:.1f}s...")
                time.sleep(wait)
                continue

            # Handle too many requests or server errors (5xx)
            if r.status_code >= 500:
                if on_rate_limit:
                    on_rate_limit()
                wait = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                if const.HTTP_LOG:
                    print(f"[api] !! Server error ({r.status_code}). Retrying after backoff...")
                time.sleep(wait)
                continue

            # Handle auth refresh-and-retry for all endpoints except login/refresh
            if allow_refresh and (refresh_fn or login_fn) and not did_login:
                trigger_refresh = False
                if r.status_code in (401, 403):
                    trigger_refresh = True
                else:
                    msg = ""
                    try:
                        body = r.json()
                        msg = (body.get("errmsg") or body.get("message") or "").lower()
                    except Exception:
                        pass
                    if "token" in msg and "expire" in msg:
                        trigger_refresh = True

                if trigger_refresh:
                    try:
                        success = False
                        # Try refresh first
                        if refresh_fn and not did_refresh:
                            if const.HTTP_LOG: print("[api] Session expired, trying refresh...")
                            try:
                                refresh_fn()
                                did_refresh = True
                                success = True
                            except Exception:
                                if const.HTTP_LOG: print("[api] Refresh failed.")
                        
                        # Try full login if refresh failed or not available
                        if not success and login_fn and not did_login:
                            if const.HTTP_LOG: print("[api] Refresh failed or unavailable, trying full re-login...")
                            try:
                                login_fn()
                                did_login = True
                                success = True
                            except Exception as e:
                                if const.HTTP_LOG: print(f"[api] Re-login failed: {e}")

                        if success:
                            # Retry original request once
                            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
                    except Exception as e:
                        if const.HTTP_LOG: print(f"[api] Auth recovery failed: {e}")

            if r.json and r.status_code >= 500 and attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            return r
        except requests.RequestException as e:
            if const.HTTP_LOG:
                print(f"[api] !! {method} {url} failed on attempt {attempt}: {e}")
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return r