import json
import os
import re
from pathlib import Path
from typing import Any

from urllib.parse import urlparse


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_cookie(cookie: dict) -> dict:
    cookie.setdefault("domain", ".google.com")
    cookie.setdefault("path", "/")
    same = cookie.get("sameSite")
    if same == "unspecified":
        cookie["sameSite"] = "Lax"
    elif same == "no_restriction":
        cookie["sameSite"] = "None"
    elif same == "strict":
        cookie["sameSite"] = "Strict"
    elif same == "lax":
        cookie["sameSite"] = "Lax"
    elif same not in {"Strict", "Lax", "None"}:
        cookie["sameSite"] = "Lax"
    return cookie


def _load_from_file(path: str) -> list[dict]:
    cookie_path = Path(path)
    if not cookie_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path}")

    encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
    text = None
    for enc in encodings:
        try:
            text = cookie_path.read_text(encoding=enc, errors="ignore")
            break
        except (UnicodeDecodeError, OSError):
            continue

    if text is None:
        raise ValueError("Could not read cookie file with supported encodings")

    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        cookies = json.loads(stripped)
        if isinstance(cookies, dict):
            return [_normalize_cookie(cookies)]
        return [_normalize_cookie(c) for c in cookies]

    cookies = []
    netscape_count = 0
    for line in stripped.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.strip().split("\t")
        if len(parts) >= 7:
            cookies.append(
                {
                    "name": parts[5],
                    "value": parts[6],
                    "domain": parts[0],
                    "path": parts[2],
                    "secure": _is_true(parts[3]),
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
            )
            netscape_count += 1
    if netscape_count:
        return [_normalize_cookie(c) for c in cookies]

    parsed = []
    for pair in re.split(r";\s*", stripped):
        if "=" in pair:
            name, value = pair.split("=", 1)
            parsed.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".google.com",
                    "path": "/",
                    "sameSite": "Lax",
                }
            )
    return [_normalize_cookie(c) for c in parsed]


def parse_cookies(
    cookie_str: str | None = None,
    cookie_file: str | None = None,
) -> list[dict]:
    cookies: list[dict] = []

    if not cookie_str and not cookie_file:
        cookie_str = os.getenv("GEMINI_COOKIES")
        cookie_file = os.getenv("GEMINI_COOKIES_FILE")

    if cookie_file:
        cookies.extend(_load_from_file(cookie_file))

    if cookie_str:
        s = cookie_str.strip()
        if s.startswith("[") or s.startswith("{"):
            data = json.loads(s)
            if isinstance(data, dict):
                cookies.append(_normalize_cookie(data))
            else:
                cookies.extend(_normalize_cookie(c) for c in data)
        else:
            for pair in re.split(r";\s*", s):
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    cookies.append(
                        {
                            "name": name.strip(),
                            "value": value.strip(),
                            "domain": ".google.com",
                            "path": "/",
                            "sameSite": "Lax",
                        }
                    )

    return [_normalize_cookie(c) for c in cookies if "name" in c and "value" in c]


def get_cookie_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def extract_psid(cookies: list[dict]) -> tuple[str, str]:
    names = {c["name"]: c["value"] for c in cookies}
    psid = names.get("__Secure-1PSID", "")
    psidts = names.get("__Secure-1PSIDTS", "")
    return psid, psidts


def load_psid_from_env() -> tuple[str, str]:
    cookie_str = os.getenv("GEMINI_COOKIES", "")
    if not cookie_str:
        return "", ""
    cookies = parse_cookies(cookie_str=cookie_str)
    return extract_psid(cookies)


def load_psid_from_file(path: str | None = None) -> tuple[str, str]:
    cookie_file = path or os.getenv("GEMINI_COOKIES_FILE", "")
    if not cookie_file:
        return "", ""
    cookies = parse_cookies(cookie_file=cookie_file)
    return extract_psid(cookies)


def validate_cookies(cookies: list[dict]) -> bool:
    names = {c["name"] for c in cookies}
    return "__Secure-1PSID" in names
