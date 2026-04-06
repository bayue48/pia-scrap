import json
import os
import random
import time
import uuid
import requests

from dataclasses import dataclass
from typing import Any, Dict, Optional
from src.const import API_BASE, CONFIG_PATH, HTTP_LOG, SESSION_HEADERS
from src.helper import _j, _mask_kv, attach_auth_cookies, merge_login_at

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
                 proxy: Optional[str] = None, timeout: int = 30, throttle: float = 2.0,
                 userkey: Optional[str] = None, tkey: Optional[str] = None):
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
            self.tokens.userkey = userkey
            if tkey:
                self.s.cookies.set("TKEY", tkey, domain=".novelpia.com", path="/")
                self.tokens.tkey = tkey
        except Exception as e:
            print(f"Error setting cookies: {e}")

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
        url = f"{API_BASE}/v1/login/refresh"
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
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception as e:
                    print(f"Error loading config: {e}")
                    cfg = {}
            cfg["login_at"] = self.tokens.login_at
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                pass
        except Exception as e:
            print(f"Error saving config: {e}")
            pass
        return self.tokens.login_at

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
            allow_refresh=True, refresh_fn=self.refresh,
        )
        r.raise_for_status()
        return r.json()

def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None):
    """Generic request wrapper: retries on 5xx and network issues.
    If allow_refresh is True and the response indicates an expired token, invoke
    refresh_fn() once and then retry the original request exactly once.
    """
    attempt = 0
    last_exc = None
    did_refresh = False
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

            if HTTP_LOG:
                print(f"[api] -> {method} {url} (attempt {attempt}/{max_retries})")
                # Effective headers (session defaults + per-call overrides)
                try:
                    eff_headers = {}
                    try:
                        eff_headers.update(getattr(session, "headers", {}) or {})
                    except Exception as e:
                        print(f"Error occurred while fetching session headers: {e}")
                        pass
                    if headers:
                        eff_headers.update(headers)
                    print(f"[api]    req-headers: {_j(_mask_kv(eff_headers))}")
                except Exception as e:
                    print(f"[api]    req-headers: <unavailable> ({e})")
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
            # Handle auth refresh-and-retry for all endpoints except login/refresh
            if allow_refresh and refresh_fn and not did_refresh:
                try:
                    trigger_refresh = False
                    # Status-based hint
                    if getattr(r, "status_code", None) in (401, 403):
                        trigger_refresh = True
                    else:
                        # Body-message based hint (robust to minor variations)
                        msg = None
                        try:
                            body = r.json()
                            if isinstance(body, dict):
                                msg = body.get("errmsg") or body.get("message")
                        except Exception as e:
                            print(f"Error occurred while parsing response body: {e}")
                            try:
                                msg = r.text
                            except Exception as e:
                                print(f"Error occurred while fetching response text: {e}")
                                msg = None
                        if isinstance(msg, str):
                            s = msg.lower()
                            if ("token" in s and "expire" in s) or ("the token has expired" in s):
                                trigger_refresh = True
                    if trigger_refresh:
                        try:
                            # Perform refresh; allow refresh_fn to return the new token
                            new_login_at = None
                            try:
                                new_login_at = refresh_fn()
                            except TypeError:
                                # Backward compatibility if refresh_fn returns nothing
                                refresh_fn()
                                new_login_at = None
                            did_refresh = True
                            # If we got a new token, update headers for retry
                            if new_login_at:
                                if headers is None:
                                    headers = {"login-at": new_login_at}
                                else:
                                    headers = dict(headers)
                                    headers["login-at"] = new_login_at
                            # Re-inject Cookie header from updated session cookies before retry
                            try:
                              attach_auth_cookies(session, headers)
                            except Exception:
                                print(f"Error occurred while preparing cookies for retry: {e}")
                                pass
                            # Retry original request once after successful refresh (with possibly updated headers)
                            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
                        except Exception as e:
                            print(f"Error occurred during refresh logic: {e}")
                            pass
                except Exception as e:
                    print(f"Error occurred during refresh logic: {e}")
                    pass
            if HTTP_LOG:
                body_preview = None
                try:
                    t = r.text
                    body_preview = (t[:500] + "…") if len(t) > 500 else t
                except Exception:
                    body_preview = "<non-text body>"
                print(f"[api] <- {r.status_code} {r.reason} from {r.url}")
                try:
                    print(f"[api]    resp-headers: {_j(_mask_kv(dict(r.headers))) }")
                except Exception:
                    print("[api]    resp-headers: <unavailable>")
                if body_preview is not None:
                    print(f"[api]    body: {body_preview}")
            if r.status_code >= 500 and attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            return r
        except requests.RequestException as e:
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