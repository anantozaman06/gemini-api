"""
web2api_engine.py - High-performance Gemini Web direct protocol engine.
Based on Sophomoresty/gemini-web2api with full async httpx support,
extended thinking depth control (@think=N), tool calling, and multimodal support.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid
from typing import AsyncGenerator, Optional, Tuple

import httpx
from loguru import logger

__version__ = "1.2.0"

# ─── Model Registry & Depth Configurations ────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum:
# 1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE
MODELS = {
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (Google backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest reasoning output (~20k+ chars)",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive reasoning depth",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (advanced reasoning & coding)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection based on task complexity",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Ultra-lightweight high velocity model",
    },
    # Curated aliases for seamless IDE compatibility
    "gemini-latest": {
        "mode": 1, "think": 4,
        "desc": "Latest live Google Gemini model (Auto)",
    },
    "gemini-2.5-pro": {
        "mode": 3, "think": 4,
        "desc": "Gemini 2.5 Pro coding and autonomous agent model",
    },
    "gemini-2.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 2.5 Flash high-speed model",
    },
    "gemini-2.0-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 2.0 Flash agent model",
    },
    "gemini-2.0-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Gemini 2.0 Flash Thinking reasoning model",
    },
    "gemini-1.5-pro": {
        "mode": 3, "think": 4,
        "desc": "Gemini 1.5 Pro repository scale model",
    },
    "gemini-1.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 1.5 Flash balanced model",
    },
    "gemini-pro": {
        "mode": 3, "think": 4,
        "desc": "Standard Gemini Pro coding model",
    },
    "gemini-flash": {
        "mode": 1, "think": 4,
        "desc": "Standard Gemini Flash model",
    },
    "gpt-4o": {
        "mode": 3, "think": 4,
        "desc": "GPT-4o drop-in alias (Gemini engine)",
    },
    "gpt-4o-mini": {
        "mode": 1, "think": 4,
        "desc": "GPT-4o-mini drop-in alias (Gemini engine)",
    },
    "claude-3-7-sonnet": {
        "mode": 3, "think": 0,
        "desc": "Claude 3.7 Sonnet drop-in alias (Gemini thinking engine)",
    },
    "claude-3-5-sonnet": {
        "mode": 3, "think": 4,
        "desc": "Claude 3.5 Sonnet drop-in alias (Gemini engine)",
    },
    "o1": {
        "mode": 2, "think": 0,
        "desc": "OpenAI o1 drop-in alias (Gemini thinking engine)",
    },
    "o3-mini": {
        "mode": 2, "think": 0,
        "desc": "OpenAI o3-mini drop-in alias (Gemini thinking engine)",
    },
}

CURRENT_BL = "boq_assistant-bard-web-server_20260716.08_p0"
PROMPT_MAX_BYTES = 60000

# ─── BL Auto-Updater ─────────────────────────────────────────────────────────
def fetch_latest_bl(proxy: Optional[str] = None) -> Optional[str]:
    """Fetch the latest gemini_bl build label from gemini.google.com."""
    try:
        req = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        ctx = ssl.create_default_context()
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx)
            )
            resp = opener.open(req, timeout=12)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=12)
        html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'(boq_assistant-bard-web-server_\d+\.\d+_p\d+)', html)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug(f"BL auto-update fetch: {e}")
    return None

def update_bl_if_needed(proxy: Optional[str] = None) -> bool:
    global CURRENT_BL
    new_bl = fetch_latest_bl(proxy=proxy)
    if new_bl and new_bl != CURRENT_BL:
        logger.info(f"Gemini BL auto-updated: {CURRENT_BL} -> {new_bl}")
        CURRENT_BL = new_bl
        return True
    return False

# ─── SAPISID Hash ────────────────────────────────────────────────────────────
def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

# ─── Model & Depth Resolution ────────────────────────────────────────────────
def resolve_model_and_thinking(model_name: Optional[str]) -> Tuple[str, int, int]:
    """
    Parses model name and optional '@think=N' depth suffix.
    Returns: (resolved_name, mode_id, think_mode)
    0 = deepest thinking (~20k+ chars), 4 = standard/fast
    """
    if not model_name:
        model_name = "gemini-3.6-flash"
    clean_name = str(model_name).strip()
    think_override = None

    if "@think=" in clean_name:
        clean_name, think_str = clean_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            pass

    key = clean_name.lower()
    cfg = MODELS.get(key)
    if not cfg:
        # Default to fast flash
        cfg = MODELS["gemini-3.6-flash"]

    mode = cfg["mode"]
    think = think_override if think_override is not None else cfg["think"]
    return clean_name, mode, think

# ─── Text Cleaning & Tool Call Extraction ─────────────────────────────────────
def clean_gemini_text(text: str, strip: bool = True) -> str:
    """Remove internal code execution artifacts from Gemini Web responses."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip() if strip else text

def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate raw line-based protocol to extract final text."""
    if "BardErrorInfo" in raw:
        m = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
        if m:
            raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)

def parse_tool_calls(text: str) -> Tuple[str, list]:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    # 1. Check ```tool_call ... ```
    pattern1 = r'```tool_call\s*\n(.*?)\n```'
    for match in re.findall(pattern1, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            # Format: {"name": ..., "arguments": ...} or {"tool_calls": [...]}
            if "tool_calls" in data and isinstance(data["tool_calls"], list):
                for item in data["tool_calls"]:
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": item.get("name", "tool"),
                            "arguments": json.dumps(item.get("arguments", {}), ensure_ascii=False) if isinstance(item.get("arguments"), dict) else str(item.get("arguments", "{}"))
                        }
                    })
            elif "name" in data:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False) if isinstance(data.get("arguments"), dict) else str(data.get("arguments", "{}")),
                    }
                })
        except Exception:
            pass

    # 2. Check <tool_call> ... </tool_call>
    pattern2 = r'<tool_call>\s*(.*?)\s*</tool_call>'
    for match in re.findall(pattern2, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            if "name" in data:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False) if isinstance(data.get("arguments"), dict) else str(data.get("arguments", "{}")),
                    }
                })
        except Exception:
            pass

    clean = re.sub(pattern1, '', text, flags=re.DOTALL)
    clean = re.sub(pattern2, '', clean, flags=re.DOTALL).strip()
    return clean, tool_calls

# ─── Multi-Modal Data URL & Image Parsers ────────────────────────────────────
def decode_data_url(url: str):
    m = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", url, re.DOTALL)
    if not m:
        return None
    mime = m.group(1) or "image/png"
    is_base64 = bool(m.group(2))
    data = m.group(3)
    try:
        if is_base64:
            return base64.b64decode(data, validate=True), mime
        return urllib.parse.unquote_to_bytes(data), mime
    except (ValueError, TypeError, binascii.Error):
        return None

def image_from_url(url: str, mime: str = None):
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        return decode_data_url(url)
    return url, mime or "image/png"

def image_from_part(part: dict):
    ptype = part.get("type")
    if ptype == "image_url":
        iurl = part.get("image_url", {})
        if isinstance(iurl, dict):
            return image_from_url(iurl.get("url"), iurl.get("mime_type"))
        return image_from_url(iurl)
    if ptype in ("input_image", "image"):
        iurl = part.get("image_url") or part.get("url")
        if isinstance(iurl, dict):
            return image_from_url(iurl.get("url"), iurl.get("mime_type"))
        if iurl:
            return image_from_url(iurl, part.get("mime_type"))
        idata = part.get("data") or part.get("base64")
        if isinstance(idata, str):
            mime = part.get("mime_type") or part.get("media_type") or "image/png"
            if idata.startswith("data:"):
                return decode_data_url(idata)
            try:
                return base64.b64decode(idata, validate=True), mime
            except (ValueError, TypeError, binascii.Error):
                return None
    return None

def messages_to_prompt(messages: list, tools: list = None) -> Tuple[str, list]:
    """Convert OpenAI messages and tools to prompt string and images list."""
    parts = []
    images = []

    if tools:
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            tools_json = json.dumps(tool_defs, indent=2)
            if len(tools_json) > PROMPT_MAX_BYTES // 2:
                slim_defs = [{"name": t["name"], "description": t["description"]} for t in tool_defs]
                tools_json = json.dumps(slim_defs, indent=2)
            parts.append(
                "[System instruction]: You have access to tools. "
                "To call a tool, respond with:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                "Only use tool_call blocks when needed.\n\n"
                f"Available tools:\n{tools_json}"
            )

    for msg in messages:
        role = msg.get("role", "user") if isinstance(msg, dict) else getattr(msg, "role", "user")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")

        if isinstance(content, list):
            text_parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") in ("text", "input_text", "output_text"):
                        text_parts.append(c.get("text", ""))
                    else:
                        img = image_from_part(c)
                        if img:
                            images.append(img)
                            text_parts.append("[Image attached]")
                elif isinstance(c, str):
                    text_parts.append(c)
            content = " ".join(text_parts)

        if role in ("system", "developer"):
            parts.append(f"[System instruction]: {content}")
        elif role in ("assistant", "model"):
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
            if tool_calls:
                tc_strs = []
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                    name = fn.get("name", "") if isinstance(fn, dict) else getattr(fn, "name", "")
                    args = fn.get("arguments", "{}") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
                    tc_strs.append(f'```tool_call\n{{"name": "{name}", "arguments": {args}}}\n```')
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role in ("tool", "function"):
            fn_name = msg.get("name", "") if isinstance(msg, dict) else getattr(msg, "name", "")
            parts.append(f"[Tool result for {fn_name}]: {content}")
        else:
            parts.append(str(content) if content else "")

    return "\n\n".join(p for p in parts if p), images

def google_contents_to_prompt(req: dict) -> Tuple[str, list]:
    """Convert Google native API contents structure to prompt string and images."""
    parts = []
    images = []

    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_text = " ".join(
            part.get("text", "") for part in sys_inst.get("parts", []) if part.get("text")
        )
        if sys_text:
            parts.append(f"[System instruction]: {sys_text}")

    for content in req.get("contents", []):
        role = content.get("role", "user")
        text_parts = []
        for part in content.get("parts", []):
            if part.get("text"):
                text_parts.append(part["text"])
            elif part.get("inlineData"):
                data = part["inlineData"]
                try:
                    images.append((
                        base64.b64decode(data["data"], validate=True),
                        data.get("mimeType", "image/png"),
                    ))
                    text_parts.append("[Image attached]")
                except (KeyError, ValueError, TypeError, binascii.Error):
                    pass
        text = " ".join(text_parts)
        if role == "model":
            parts.append(f"[Assistant]: {text}")
        else:
            parts.append(text)

    return "\n\n".join(part for part in parts if part), images

# ─── Payload Builder & Protocol Execution ────────────────────────────────────
def build_stream_payload(prompt: str, model_id: int, think_mode: int, temporary_chats: bool = False) -> bytes:
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    if temporary_chats:
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    return urllib.parse.urlencode(params).encode()

def get_request_headers(cookie_str: str = "", sapisid: str = "") -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers

async def async_gemini_generate(
    prompt: str,
    model_id: int = 1,
    think_mode: int = 4,
    cookie_str: str = "",
    sapisid: str = "",
    proxy: Optional[str] = None,
    timeout_sec: int = 120,
    retry_attempts: int = 3,
) -> str:
    """Async execution of Gemini StreamGenerate endpoint returning clean text."""
    body = build_stream_payload(prompt, model_id, think_mode)
    headers = get_request_headers(cookie_str, sapisid)

    last_err = None
    for attempt in range(retry_attempts):
        reqid = int(time.time()) % 1000000
        url = (
            f"https://gemini.google.com/_/BardChatUi/data/"
            f"assistant.lamda.BardFrontendService/StreamGenerate"
            f"?bl={CURRENT_BL}&hl=en&_reqid={reqid}&rt=c"
        )
        try:
            transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
            async with httpx.AsyncClient(transport=transport, timeout=timeout_sec, verify=True) as client:
                resp = await client.post(url, content=body, headers=headers)
                if resp.status_code == 405 and update_bl_if_needed(proxy=proxy):
                    last_err = RuntimeError(f"HTTP 405 - Retrying with updated BL {CURRENT_BL}")
                    continue
                resp.raise_for_status()
                raw = resp.text
                return extract_response_text(raw)
        except Exception as e:
            last_err = e
            if attempt < retry_attempts - 1:
                logger.debug(f"Web2API attempt {attempt+1}/{retry_attempts} failed: {e}. Retrying in 1s...")
                await httpx.AsyncClient().sleep(1.0)
    raise last_err or RuntimeError("Gemini generate failed after retries")

async def async_gemini_stream(
    prompt: str,
    model_id: int = 1,
    think_mode: int = 4,
    cookie_str: str = "",
    sapisid: str = "",
    proxy: Optional[str] = None,
    timeout_sec: int = 120,
) -> AsyncGenerator[str, None]:
    """Async incremental streaming generator yielding text deltas."""
    body = build_stream_payload(prompt, model_id, think_mode)
    headers = get_request_headers(cookie_str, sapisid)
    reqid = int(time.time()) % 1000000
    url = (
        f"https://gemini.google.com/_/BardChatUi/data/"
        f"assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CURRENT_BL}&hl=en&_reqid={reqid}&rt=c"
    )

    prev_text = ""
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
    async with httpx.AsyncClient(transport=transport, timeout=timeout_sec, verify=True) as client:
        try:
            async with client.stream("POST", url, content=body, headers=headers) as resp:
                if resp.status_code == 405:
                    if update_bl_if_needed(proxy=proxy):
                        full = await async_gemini_generate(prompt, model_id, think_mode, cookie_str, sapisid, proxy, timeout_sec)
                        if full:
                            yield full
                        return
                resp.raise_for_status()
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf:
                        m = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                        if m:
                            raise RuntimeError(f"Gemini upstream error: BardErrorInfo [{m.group(1)}]")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if '"wrb.fr"' not in line or len(line) < 200:
                            continue
                        try:
                            arr = json.loads(line)
                            inner_str = arr[0][2]
                            if not inner_str or len(inner_str) < 50:
                                continue
                            inner2 = json.loads(inner_str)
                            if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                                for part in inner2[4]:
                                    if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                        for t in part[1]:
                                            if isinstance(t, str) and len(t) > len(prev_text):
                                                delta = t[len(prev_text):]
                                                delta = clean_gemini_text(delta, strip=False)
                                                if delta:
                                                    yield delta
                                                prev_text = t
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass
        except Exception as e:
            logger.debug(f"Streaming iter error: {e}")
            if not prev_text:
                full = await async_gemini_generate(prompt, model_id, think_mode, cookie_str, sapisid, proxy, timeout_sec)
                if full:
                    yield full
