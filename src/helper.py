
import base64
import json
import os
import re

from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse
import requests
from src.const import BASE_URL, CONFIG_PATH, IMG_BASE_HTTPS

# ----------------------------
# Helpers
# ----------------------------

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip()) or "book"

def normalize_url(u: str) -> str:
    if not u:
        return u
    if u.startswith("//"):
        return IMG_BASE_HTTPS + u
    if u.startswith("/"):
        return urljoin(BASE_URL, u)
    return u

def media_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"

def looks_like_jwt(token: Optional[str]) -> bool:
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    for p in parts:
        try:
            base64.urlsafe_b64decode(p + "===")
        except Exception:
            return False
    return True

def kebab(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "book"

# ----------------------------
# Config management
# ----------------------------

def load_config() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        print(f"Error occurred while loading config: {e}")
        return {}
    return {}

def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error occurred while saving config: {e}")
        pass

# ----------------------------
# Auth token management & header merging
# ----------------------------

def merge_login_at(headers: dict, login_at: Optional[str]) -> dict:
    h = dict(headers or {})
    if login_at:
        h["login-at"] = login_at
    return h

def _mask_value(v: Any) -> Any:
    try:
        if isinstance(v, dict):
            return {k: _mask_value(v2) for k, v2 in v.items()}
        if isinstance(v, list):
            return [_mask_value(x) for x in v]
        if isinstance(v, str):
            low = v.lower()
            # Mask long tokens / JWT-like
            if low.count(".") == 2 and all(len(p) > 5 for p in v.split(".")):
                parts = v.split(".")
                return parts[0][:6] + "..." + parts[-1][-6:]
            if len(v) > 64:
                return v[:32] + "…(trunc)"
            return v
        return v
    except Exception:
        return "<masked>"

def mask_kv(d: Optional[dict]) -> Optional[dict]:
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        kl = str(k).lower()
        if any(x in kl for x in (
            "pass", "passwd", "password", "authorization", "token",
            "login-at", "login_at", "_t", "cookie", "set-cookie"
        )):
            out[k] = "***"
        else:
            out[k] = _mask_value(v)
    return out

def j(x: Any) -> str:
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:
        return str(x)
    
def attach_auth_cookies(session, headers=None):
        ck = getattr(session, "cookies", None)
        if ck is None:
            return headers

        uval = None
        tval = None

        try:
            for c in ck:
                if c.name == "USERKEY":
                    uval = c.value
                elif c.name == "TKEY":
                    tval = c.value
        except Exception as e:
            print(f"Error occurred while fetching cookies: {e}")

        cookie_parts = []
        if uval:
            cookie_parts.append(f"USERKEY={uval}")
        if tval:
            cookie_parts.append(f"TKEY={tval}")

        cookie_parts.append("last_login=basic")

        if cookie_parts:
            headers = dict(headers or {})
            headers.setdefault("Cookie", "; ".join(cookie_parts))

        return headers

# ----------------------------
# Token extraction (STRICT)
# ----------------------------

def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)

def extract_t_token(tdata: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, direct_content_url_or_none).
    Prefer JWT-like tokens, but accept any non-empty string if present.
    If using URL, accept any _t value on the official content endpoint.
    """
    res = tdata.get("result", {}) if isinstance(tdata, dict) else {}
    fallback_token: Optional[str] = None

    # 1) common keys at result
    for k in ("_t", "t", "token"):
        v = res.get(k)
        if isinstance(v, str) and v:
            if looks_like_jwt(v):
                return v, None
            fallback_token = fallback_token or v

    # 2) nested dicts under result
    if isinstance(res, dict):
        for _, v in res.items():
            if isinstance(v, dict):
                for k in ("_t", "t", "token"):
                    vv = v.get(k)
                    if isinstance(vv, str) and vv:
                        if looks_like_jwt(vv):
                            return vv, None
                        fallback_token = fallback_token or vv

    # 3) URL that is the official content endpoint with any _t
    for s in iter_strings(tdata):
        if isinstance(s, str) and (s.startswith("http://") or s.startswith("https://")):
            try:
                p = urlparse(s)
                if p.netloc.endswith("api-global.novelpia.com") and p.path.endswith("/v1/novel/episode/content"):
                    q = parse_qs(p.query)
                    cand = (q.get("_t") or [None])[0]
                    if isinstance(cand, str) and cand:
                        if looks_like_jwt(cand):
                            return cand, s
                        # fallback
                        fallback_token = fallback_token or cand
            except Exception as e:
                print(f"Error occurred while parsing URL: {e}")
                pass
    if fallback_token:
        return fallback_token, None
    return None, None