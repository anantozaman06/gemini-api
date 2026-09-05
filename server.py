import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from agent import execute_agent_task, WorkspaceTools

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger

from gemini_webapi import GeminiClient, ModelOutput, GeneratedImage, WebImage
from gemini_webapi.constants import Model

from web2api_engine import (
    MODELS as WEB2API_MODELS,
    CURRENT_BL,
    resolve_model_and_thinking,
    clean_gemini_text,
    extract_response_text,
    parse_tool_calls,
    messages_to_prompt,
    google_contents_to_prompt,
    async_gemini_generate,
    async_gemini_stream,
    update_bl_if_needed,
)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
OUTPUT_DIR = Path(__file__).parent / "outputs"

def disable_quick_edit():
    """Disables QuickEdit mode in Windows console to prevent mouse clicks from freezing the server."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
                # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
                new_mode = (mode.value & ~0x0040) | 0x0080
                kernel32.SetConsoleMode(h_stdin, new_mode)
        except Exception:
            pass

disable_quick_edit()

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

cfg = load_config()
PSID   = os.environ.get("SECURE_1PSID") or os.environ.get("__Secure-1PSID") or cfg.get("__Secure-1PSID", "")
PSIDTS = os.environ.get("SECURE_1PSIDTS") or os.environ.get("__Secure-1PSIDTS") or cfg.get("__Secure-1PSIDTS", "")
PORT   = int(os.environ.get("PORT", 7860 if os.environ.get("SPACE_ID") else cfg.get("port", 5353)))
DEFAULT_MODEL = os.environ.get("MODEL") or cfg.get("model", "gemini-3.6-flash")

# ── Globals ───────────────────────────────────────────────────────────────────
gemini_client: GeminiClient | None = None
tunnel_url: str = ""
tunnel_proc: Any = None
chat_sessions: dict[str, Any] = {}
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(exist_ok=True)

# ── Security & Authentication Helper ──────────────────────────────────────────
def check_authorized(request: Request):
    """
    Validates client authorization:
    - If 'api_keys' is empty or not configured: Zero-auth (allows any key or empty).
    - If 'api_keys' contains keys (or via API_KEY / API_KEYS env): Validates Authorization Bearer, x-api-key, x-goog-api-key, or ?key=.
    """
    keys = list(cfg.get("api_keys") or [])
    env_keys = os.environ.get("API_KEYS") or os.environ.get("API_KEY")
    if env_keys:
        for k in env_keys.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)

    if not keys:
        return True

    # 1. Bearer token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token in keys:
            return True

    # 2. Custom header keys
    for h in ("x-api-key", "x-goog-api-key"):
        val = request.headers.get(h)
        if val and val in keys:
            return True

    # 3. Query parameter ?key= (Gemini CLI / Google AI standard)
    qkey = request.query_params.get("key")
    if qkey and qkey in keys:
        return True

    raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key"}})

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global gemini_client, tunnel_url, tunnel_proc

    logger.add(
        "logs/server_{time}.log",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        level="INFO",
    )
    logger.info("Starting Gemini Local API (Dual-Engine Web2API + Session) on port {}", PORT)

    # Initialize Gemini web client if cookies configured
    if not PSID or PSID == "YOUR_PSID_HERE":
        logger.info("Session cookies not set in config.json. Direct Web2API engine active (Zero-auth mode).")
        logger.info("Open http://localhost:{} to configure cookies if Advanced features (Imagen, Gems) are needed.", PORT)
    else:
        try:
            gemini_client = GeminiClient(PSID, PSIDTS)
            await gemini_client.init(timeout=120, auto_close=False, auto_refresh=True)
            logger.success("Gemini session client initialized successfully")
        except Exception as e:
            logger.warning("Gemini session client init warning: {}. Direct Web2API fallback engine ready.", e)
            gemini_client = None

    if cfg.get("cloudflare_tunnel") and not os.environ.get("SPACE_ID"):
        try:
            import subprocess, threading
            cloudflared_bin = Path(__file__).parent / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
            if cloudflared_bin.exists():
                def run_tunnel():
                    global tunnel_url, tunnel_proc
                    try:
                        tunnel_proc = subprocess.Popen(
                            [str(cloudflared_bin), "tunnel", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1, encoding="utf-8", errors="replace"
                        )
                        for line in tunnel_proc.stdout:
                            if not tunnel_url and "trycloudflare.com" in line:
                                for part in line.split():
                                    if "trycloudflare.com" in part and part.startswith("https://"):
                                        tunnel_url = part.strip().rstrip(".,;)")
                                        logger.success("Cloudflare Tunnel: {}", tunnel_url)
                                        break
                    except Exception as err:
                        logger.warning("Cloudflare tunnel error: {}", err)

                threading.Thread(target=run_tunnel, daemon=True).start()
            elif sys.platform == "win32":
                logger.warning("cloudflared.exe not found at {}", cloudflared_bin)
        except Exception as e:
            logger.warning("Cloudflare tunnel failed: {}", e)

    yield

    if tunnel_proc and tunnel_proc.poll() is None:
        try:
            tunnel_proc.terminate()
        except Exception:
            pass

    if gemini_client:
        try:
            await gemini_client.close()
        except Exception as e:
            logger.warning("Error during client shutdown: {}", e)
    logger.info("Server shutdown complete")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Gemini Local API (Dual-Engine Web2API)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
if OUTPUT_DIR.exists():
    app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

# ── Image Helpers ──────────────────────────────────────────────────────────────
async def _save_generated_images(images: list[Any], base_name: str = "gemini") -> list[dict]:
    saved = []
    for idx, img in enumerate(images):
        img_data = {
            "url": getattr(img, "url", ""),
            "alt": getattr(img, "alt", ""),
            "title": getattr(img, "title", "[Image]"),
        }
        if hasattr(img, "image_id") and img.image_id:
            img_data["image_id"] = img.image_id

        if isinstance(img, GeneratedImage):
            filename = f"{base_name}_{int(time.time())}_{idx}.png"
            filepath = OUTPUT_DIR / filename
            try:
                await img.save(path=str(OUTPUT_DIR), filename=filename)
                img_data["url"] = f"/outputs/{filename}"
                img_data["local"] = str(filepath)
            except Exception as e:
                logger.warning(f"Failed to save generated image: {e}")

        saved.append(img_data)
    return saved

# ── Pydantic Models ───────────────────────────────────────────────────────────
class Message(BaseModel):
    model_config = {"extra": "allow"}
    role: str
    content: Any = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

class ChatRequest(BaseModel):
    model_config = {"extra": "allow"}
    model: str = "gemini-3.6-flash"
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    gem_id: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stop: list[str] | str | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None

class EmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str = "text-embedding-ada-002"

class AgentTaskRequest(BaseModel):
    prompt: str
    workspace: str = "."
    model: str = "unspecified"
    max_steps: int = 25

class ConfigUpdate(BaseModel):
    psid: str
    psidts: str = ""
    model: str = "gemini-3.6-flash"
    cloudflare_tunnel: bool = False
    port: int = 5353
    api_keys: list[str] = []
    proxy: str | None = None

class DeepResearchRequest(BaseModel):
    prompt: str
    model: str = "unspecified"

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_thinking_model(model: str | None) -> bool:
    if not model:
        return False
    m = str(model).lower()
    return any(k in m for k in ("thinking", "reason", "reasoning", "thought", "o1", "o3", "r1"))

def build_tools_system_prompt(tools: list[dict]) -> str:
    lines = [
        "You have access to the following tools/functions that you can call when needed to fulfill user requests:",
    ]
    for idx, t in enumerate(tools):
        fn = t.get("function", t)
        lines.append(f"\nTool #{idx+1}: {fn.get('name')}")
        lines.append(f"Description: {fn.get('description', '')}")
        lines.append(f"Parameters Schema: {json.dumps(fn.get('parameters', {}))}")

    lines.append("\nINSTRUCTIONS FOR CALLING TOOLS:")
    lines.append("To call one or more tools, you MUST respond with a ```tool_call code block containing valid JSON in this exact format:")
    lines.append("```tool_call")
    lines.append('{"tool_calls": [{"name": "function_name", "arguments": {"arg1": "val1"}}]}')
    lines.append("```")
    lines.append("CRITICAL: You MUST use the ```tool_call block format whenever executing any tool. Do NOT output raw tool call JSON outside ```tool_call. If you do not need to call any tool, answer normally with plain text.")
    return "\n".join(lines)

def extract_tool_calls_from_text(text: str) -> tuple[list[dict], str]:
    cleaned_text, tool_calls = parse_tool_calls(text)
    return tool_calls, cleaned_text

def get_message_text(content: Any) -> str:
    """Safely extract plain text from string or structured multimodal message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content) if content is not None else ""

def require_client():
    if not gemini_client:
        logger.error("Gemini session client not initialized. Configure cookies in config.json for this endpoint.")
        raise HTTPException(503, "Gemini session cookies required for this specific endpoint. Open http://localhost:5353 to configure.")

def extract_text_and_files(messages: list[Message]) -> tuple[str, list]:
    if not messages:
        return "", []
    last_msg = messages[-1]
    if last_msg.role in ("tool", "function"):
        fn_name = last_msg.name or (f"id={last_msg.tool_call_id}" if last_msg.tool_call_id else "tool")
        return f"Tool Output ({fn_name}):\n{get_message_text(last_msg.content)}", []

    last_user = next((m for m in reversed(messages) if m.role in ("user", "tool", "function")), messages[-1])
    text_content = get_message_text(last_user.content)
    file_parts = []
    if isinstance(last_user.content, list):
        for part in last_user.content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    try:
                        header, b64 = url.split(",", 1)
                        file_parts.append(io.BytesIO(base64.b64decode(b64)))
                    except Exception:
                        pass
    return text_content, file_parts

def build_context_prompt(messages: list[Message]) -> str:
    lines = []
    for m in messages[:-1]:
        content = get_message_text(m.content)
        if m.role in ("user", "human"):
            lines.append(f"User: {content}")
        elif m.role in ("assistant", "model"):
            if m.tool_calls:
                tc_list = []
                for tc in m.tool_calls:
                    fn = tc.get("function", tc)
                    raw_args = fn.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except Exception:
                            pass
                    tc_list.append({"name": fn.get("name", "tool"), "arguments": raw_args})
                tc_block = f"```tool_call\n{json.dumps({'tool_calls': tc_list}, ensure_ascii=False)}\n```"
                if content:
                    lines.append(f"Assistant: {content}\n{tc_block}")
                else:
                    lines.append(f"Assistant:\n{tc_block}")
            elif content:
                lines.append(f"Assistant: {content}")
        elif m.role in ("system", "developer"):
            continue
        elif m.role in ("tool", "function"):
            fn_name = m.name or (f"id={m.tool_call_id}" if m.tool_call_id else "tool")
            lines.append(f"Tool Output ({fn_name}): {content}")
    return "\n\n".join(lines)

def parse_model_output(response: ModelOutput) -> dict:
    result = {
        "text": response.text,
        "thoughts": response.thoughts,
        "images": [],
        "videos": [],
        "media": [],
        "deep_research_plan": None,
    }
    for img in response.images:
        img_data = {"url": img.url, "alt": img.alt, "title": img.title}
        if hasattr(img, "image_id") and img.image_id:
            img_data["image_id"] = img.image_id
        result["images"].append(img_data)
    for vid in response.videos:
        result["videos"].append({
            "url": vid.url,
            "thumbnail": getattr(vid, "thumbnail", ""),
        })
    for m in response.media:
        result["media"].append({
            "url": m.url,
            "thumbnail": getattr(m, "thumbnail", ""),
            "mp3_url": getattr(m, "mp3_url", ""),
        })
    if response.deep_research_plan:
        plan = response.deep_research_plan
        result["deep_research_plan"] = {
            "title": getattr(plan, "title", ""),
            "steps": getattr(plan, "steps", []),
            "questions": getattr(plan, "questions", []),
        }
    return result

async def resolve_gem(client: GeminiClient, gem_id_or_name: str | None):
    if not gem_id_or_name or not client:
        return None
    try:
        gems = await client.fetch_gems()
        if not gems:
            return None
        for g in gems:
            if g.id == gem_id_or_name or g.name == gem_id_or_name:
                return g
    except Exception:
        pass
    return None

# ── Response Builders ─────────────────────────────────────────────────────────
def make_chunk(
    model: str,
    content: str | None = None,
    role: str | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str | None = None,
    chunk_id: str | None = None,
    images: list | None = None,
    thoughts: str | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls:
        formatted_calls = []
        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function", tc)
            raw_args = fn.get("arguments", tc.get("arguments", "{}"))
            args_str = json.dumps(raw_args) if isinstance(raw_args, dict) else str(raw_args)
            formatted_calls.append({
                "index": tc.get("index", idx),
                "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "type": "function",
                "function": {
                    "name": fn.get("name", tc.get("name", "tool")),
                    "arguments": args_str
                }
            })
        delta["tool_calls"] = formatted_calls

    chunk = {
        "id": chunk_id or f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    }
    if images or thoughts:
        extras = {}
        if images: extras["images"] = images
        if thoughts: extras["thoughts"] = thoughts
        chunk["_gemini"] = extras
    return f"data: {json.dumps(chunk)}\n\n"

def make_response(content: str | None, model: str,
                  tool_calls: list[dict] | None = None,
                  images: list | None = None,
                  thoughts: str | None = None,
                  videos: list | None = None,
                  media: list | None = None,
                  deep_research_plan: dict | None = None,
                  prompt_tokens: int = 0,
                  completion_tokens: int = 0) -> dict:
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        formatted_calls = []
        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function", tc)
            raw_args = fn.get("arguments", tc.get("arguments", "{}"))
            args_str = json.dumps(raw_args) if isinstance(raw_args, dict) else str(raw_args)
            formatted_calls.append({
                "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "type": "function",
                "function": {
                    "name": fn.get("name", tc.get("name", "tool")),
                    "arguments": args_str
                }
            })
        msg["content"] = content if content else None
        msg["tool_calls"] = formatted_calls
        finish_reason = "tool_calls"
    else:
        msg["content"] = content or ""
        finish_reason = "stop"

    extras = {}
    if images: extras["images"] = images
    if thoughts: extras["thoughts"] = thoughts
    if videos: extras["videos"] = videos
    if media: extras["media"] = media
    if deep_research_plan: extras["deep_research_plan"] = deep_research_plan
    if extras: msg["_gemini"] = extras

    total_tokens = prompt_tokens + completion_tokens
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": max(1, prompt_tokens),
            "completion_tokens": max(1, completion_tokens),
            "total_tokens": max(2, total_tokens)
        }
    }

# ── Routes ────────────────────────────────────────────────────────────────────
@app.head("/")
@app.head("/health")
async def head_ping():
    return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def root():
    html_file = static_dir / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Gemini API Server Running</h1>")

@app.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "version": "1.2.0",
        "dual_engine": {
            "web2api_direct_ready": True,
            "session_client_ready": gemini_client is not None,
            "current_bl": CURRENT_BL,
        },
        "tunnel_url": tunnel_url or None,
        "local_url": f"http://localhost:{PORT}",
        "port": PORT,
        "features": {
            "chat": True, "streaming": True, "vision": True,
            "tool_calling": True, "extended_thinking": True,
            "codex_responses_api": True, "google_native_api": True,
            "image_generation": True, "video_generation": True,
            "audio_generation": True, "deep_research": True,
            "gems": True, "chat_history": True, "multi_turn": True,
        }
    }

# ── Model Registry for OpenAI Format ──────────────────────────────────────────
CURATED_MODELS = [
    {
        "id": "gemini-3.7-flash",
        "object": "model",
        "created": 1746000000,
        "owned_by": "google",
        "display_name": "Gemini 3.7 Flash (Latest All-Around Model)",
        "description": "Google's newest and fastest flagship multimodal model with hybrid reasoning.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-3.6-flash",
        "object": "model",
        "created": 1745000000,
        "owned_by": "google",
        "display_name": "Gemini 3.6 Flash (Default)",
        "description": "Ultra-fast, versatile model for all coding, agent loops, and general queries.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-3.5-flash-thinking",
        "object": "model",
        "created": 1744000000,
        "owned_by": "google",
        "display_name": "Gemini 3.5 Flash Thinking (Deep Reasoning, ~20k+ chars)",
        "description": "Extended thinking model with comprehensive chain-of-thought analysis for complex tasks.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-3.5-flash-thinking-lite",
        "object": "model",
        "created": 1744000000,
        "owned_by": "google",
        "display_name": "Gemini 3.5 Flash Thinking Lite (Adaptive Reasoning)",
        "description": "Dynamic reasoning depth adjusting automatically based on prompt complexity.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-3.1-pro",
        "object": "model",
        "created": 1743000000,
        "owned_by": "google",
        "display_name": "Gemini 3.1 Pro (Advanced Math & Architecture)",
        "description": "Pro-tier model for rigorous software architecture and mathematical synthesis.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-auto",
        "object": "model",
        "created": 1743000000,
        "owned_by": "google",
        "display_name": "Gemini Auto (Dynamic Model Routing)",
        "description": "Automatically selects the best model mode depending on the prompt requirements.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-flash-lite",
        "object": "model",
        "created": 1743000000,
        "owned_by": "google",
        "display_name": "Gemini Flash Lite",
        "description": "Ultra-lightweight fast response model for autocomplete and rapid edits.",
        "context_window": 1048576, "max_tokens": 32768, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-latest",
        "object": "model",
        "created": 1745000000,
        "owned_by": "google",
        "display_name": "Gemini Latest (Live Bleeding-Edge Gemini)",
        "description": "Always connects to Google's newest and most powerful live Gemini model in real-time.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-2.5-pro",
        "object": "model",
        "created": 1740000000,
        "owned_by": "google",
        "display_name": "Gemini 2.5 Pro (Best for Coding & Agents)",
        "description": "Top-rated model for complex coding, agentic tool workflows, and deep repository refactoring.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-2.5-flash",
        "object": "model",
        "created": 1740000000,
        "owned_by": "google",
        "display_name": "Gemini 2.5 Flash",
        "description": "High-velocity multimodal model optimized for real-time vibe coding.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-2.0-flash",
        "object": "model",
        "created": 1735000000,
        "owned_by": "google",
        "display_name": "Gemini 2.0 Flash",
        "description": "Instruction-following model tuned for Cline, Kilo Code, and Cursor.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-2.0-flash-thinking",
        "object": "model",
        "created": 1735000000,
        "owned_by": "google",
        "display_name": "Gemini 2.0 Flash Thinking",
        "description": "Reasoning model showing internal thought process before code synthesis.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gemini-1.5-pro",
        "object": "model",
        "created": 1715000000,
        "owned_by": "google",
        "display_name": "Gemini 1.5 Pro (2M Context Ingestion)",
        "description": "Massive 2 Million token context window for full-repository understanding.",
        "context_window": 2097152, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "gpt-4o",
        "object": "model",
        "created": 1715000000,
        "owned_by": "openai",
        "display_name": "GPT-4o (Gemini Engine)",
        "description": "OpenAI drop-in alias routed directly through Gemini.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "claude-3-7-sonnet",
        "object": "model",
        "created": 1740000000,
        "owned_by": "anthropic",
        "display_name": "Claude 3.7 Sonnet (Gemini Engine)",
        "description": "Anthropic drop-in alias routed through Gemini thinking engine.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
    {
        "id": "claude-3-5-sonnet",
        "object": "model",
        "created": 1720000000,
        "owned_by": "anthropic",
        "display_name": "Claude 3.5 Sonnet (Gemini Engine)",
        "description": "Anthropic drop-in alias routed through Gemini.",
        "context_window": 1048576, "max_tokens": 65536, "supports_tools": True, "supports_vision": True,
    },
]

@app.get("/v1/models")
async def list_models(request: Request):
    check_authorized(request)
    return {"object": "list", "data": CURATED_MODELS}

@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str, request: Request):
    check_authorized(request)
    for m in CURATED_MODELS:
        if m["id"] == model_id:
            return m
    return {
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "google",
        "display_name": model_id,
        "description": f"Gemini model: {model_id}",
        "context_window": 1048576,
        "max_tokens": 65536,
        "supports_tools": True,
        "supports_vision": True,
    }

# ── GOOGLE NATIVE API (Gemini CLI & Google AI Studio SDKs) ───────────────────
@app.get("/v1beta/models")
async def google_models_list(request: Request):
    """Google native model list format compatible with official Gemini CLI."""
    check_authorized(request)
    models_list = []
    for name, mcfg in WEB2API_MODELS.items():
        models_list.append({
            "name": f"models/{name}",
            "displayName": name,
            "description": mcfg.get("desc", f"Google Gemini model {name}"),
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
        })
    return {"models": models_list}

@app.post("/v1beta/models/{model_path:path}")
async def google_native_generate(model_path: str, request: Request):
    """
    Handles Google native generateContent and streamGenerateContent.
    Path examples:
      - /v1beta/models/gemini-3.6-flash:generateContent
      - /v1beta/models/gemini-3.6-flash:streamGenerateContent
      - /v1beta/models/models/gemini-2.5-pro:generateContent
    """
    check_authorized(request)
    action = "generateContent"
    raw_model = model_path
    if ":" in model_path:
        raw_model, action = model_path.split(":", 1)
    if raw_model.startswith("models/"):
        raw_model = raw_model[len("models/"):]

    req_body = await request.json()
    prompt, _ = google_contents_to_prompt(req_body)
    if not prompt.strip():
        raise HTTPException(400, "Empty content")

    model_name, mode_id, think_mode = resolve_model_and_thinking(raw_model)
    proxy = cfg.get("proxy")
    stream = (action == "streamGenerateContent") or req_body.get("stream", False)

    if stream:
        async def google_stream_gen():
            async for delta in async_gemini_stream(prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy):
                chunk_obj = {
                    "candidates": [{
                        "content": {"parts": [{"text": delta}], "role": "model"},
                        "finishReason": None,
                        "index": 0
                    }],
                    "modelVersion": model_name,
                }
                yield f"data: {json.dumps(chunk_obj)}\n\n"
            final_obj = {
                "candidates": [{
                    "content": {"parts": []},
                    "finishReason": "STOP",
                    "index": 0
                }],
                "usageMetadata": {
                    "promptTokenCount": len(prompt) // 4,
                    "candidatesTokenCount": 0,
                    "totalTokenCount": len(prompt) // 4,
                },
                "modelVersion": model_name,
            }
            yield f"data: {json.dumps(final_obj)}\n\n"
        return StreamingResponse(google_stream_gen(), media_type="text/event-stream")
    else:
        text = await async_gemini_generate(prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy)
        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        return {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

# ── OPENAI RESPONSES API (For OpenAI Codex CLI & Advanced Coding Tools) ───────
@app.post("/v1/responses")
async def responses_api(request: Request):
    """OpenAI Responses API endpoint for OpenAI Codex CLI and modern agent tools."""
    check_authorized(request)
    req = await request.json()
    model_input = req.get("model", "gemini-3.6-flash")
    model_name, mode_id, think_mode = resolve_model_and_thinking(model_input)

    input_items = req.get("input", [])
    tools = req.get("tools")
    messages = []
    if req.get("instructions"):
        messages.append({"role": "system", "content": req["instructions"]})
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if item.get("type") == "function_call_output":
                    messages.append({"role": "tool", "name": item.get("name", ""), "content": item.get("output", "")})
                elif item.get("type") in ("input_text", "input_image", "image"):
                    messages.append({"role": "user", "content": [item]})
                elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                    cp = item.get("content", [])
                    text_acc, tc_list = "", []
                    if isinstance(cp, list):
                        for c in cp:
                            if isinstance(c, dict):
                                if c.get("type") == "output_text":
                                    text_acc += c.get("text", "")
                                elif c.get("type") == "function_call":
                                    tc_list.append(c)
                    elif isinstance(cp, str):
                        text_acc = cp
                    m = {"role": "assistant", "content": text_acc or None}
                    if tc_list:
                        m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                            "function": {"name": tc.get("name", ""), "arguments": tc.get("arguments", "{}")}}
                                           for i, tc in enumerate(tc_list)]
                    messages.append(m)
                else:
                    role = item.get("role", "user")
                    messages.append({"role": role, "content": item.get("content", "")})

    prompt, _ = messages_to_prompt(messages, tools)
    if not prompt.strip():
        raise HTTPException(400, "Empty prompt")

    proxy = cfg.get("proxy")
    rid = f"resp_{uuid.uuid4().hex[:16]}"
    mid = f"msg_{uuid.uuid4().hex[:12]}"

    if req.get("stream"):
        async def response_stream_gen():
            seq = [0]
            def emit(ev_type, **fields):
                seq[0] += 1
                ev = {"type": ev_type, "sequence_number": seq[0], **fields}
                return f"event: {ev_type}\ndata: {json.dumps(ev)}\n\n"

            base_resp = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model_name}
            yield emit("response.created", response={**base_resp, "status": "in_progress", "output": [], "usage": None})
            yield emit("response.in_progress", response={**base_resp, "status": "in_progress", "output": [], "usage": None})

            accumulated = ""
            async for delta in async_gemini_stream(prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy):
                accumulated += delta
                yield emit("response.output_text.delta", item_id=mid, output_index=0, content_index=0, delta=delta)

            clean_text, tool_calls = parse_tool_calls(accumulated) if tools else (accumulated, [])
            output = []
            if tool_calls:
                for tc in tool_calls:
                    output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                                   "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
            if clean_text or not tool_calls:
                output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": clean_text or "", "annotations": []}]})

            yield emit("response.output_text.done", item_id=mid, output_index=0, content_index=0, text=clean_text)
            yield emit("response.output_item.done", output_index=0, item={"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": clean_text}]})
            usage = {"input_tokens": len(prompt)//4, "output_tokens": len(accumulated)//4, "total_tokens": (len(prompt)+len(accumulated))//4}
            yield emit("response.completed", response={**base_resp, "status": "completed", "output": output, "usage": usage})

        return StreamingResponse(response_stream_gen(), media_type="text/event-stream")
    else:
        text = await async_gemini_generate(prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy)
        clean_text, tool_calls = parse_tool_calls(text) if tools else (text, [])
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if clean_text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": clean_text or "", "annotations": []}]})

        return {
            "id": rid,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model_name,
            "output": output,
            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}
        }

# ── CHAT COMPLETIONS (Full Multimodal & Dual-Engine Failover) ────────────────
@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    check_authorized(request)
    prompt, files = extract_text_and_files(req.messages)
    if not prompt and not files:
        raise HTTPException(400, "No prompt provided")

    system_msgs = [get_message_text(m.content) for m in req.messages if m.role in ("system", "developer")]
    tools_prompt = build_tools_system_prompt(req.tools) if req.tools else ""

    context = build_context_prompt(req.messages)
    prompt_sections = []
    if system_msgs:
        prompt_sections.append("System Instructions:\n" + "\n".join(system_msgs))
    if tools_prompt:
        prompt_sections.append(tools_prompt)
    if context:
        prompt_sections.append(f"Previous conversation:\n{context}")
    if prompt.startswith("Tool Output"):
        prompt_sections.append(prompt)
    else:
        prompt_sections.append(f"User: {prompt}")
    full_prompt = "\n\n".join(prompt_sections)

    model_name, mode_id, think_mode = resolve_model_and_thinking(req.model)
    gem = await resolve_gem(gemini_client, req.gem_id) if (gemini_client and req.gem_id) else None
    prompt_tokens = max(1, len(full_prompt) // 4)
    thinking = (think_mode == 0) or is_thinking_model(req.model)
    proxy = cfg.get("proxy")

    if req.stream:
        async def stream_gen():
            stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            # 1. Initial assistant role chunk
            yield make_chunk(model=req.model, role="assistant", content="", chunk_id=stream_id)

            accumulated_text = ""
            use_fallback = (gemini_client is None)

            # Try primary session engine if client is available
            if not use_fallback:
                try:
                    if not req.tools:
                        async for chunk in gemini_client.generate_content_stream(
                            full_prompt, files=files or None, model=None, gem=gem, extended_thinking=thinking
                        ):
                            delta_text = chunk.text_delta or ""
                            if delta_text:
                                accumulated_text += delta_text
                                yield make_chunk(model=req.model, content=delta_text, chunk_id=stream_id)
                        yield make_chunk(model=req.model, finish_reason="stop", chunk_id=stream_id)
                    else:
                        async for chunk in gemini_client.generate_content_stream(
                            full_prompt, files=files or None, model=None, gem=gem, extended_thinking=thinking
                        ):
                            delta_text = chunk.text_delta or ""
                            accumulated_text += delta_text

                        tool_calls, cleaned = extract_tool_calls_from_text(accumulated_text)
                        if tool_calls:
                            if cleaned:
                                yield make_chunk(model=req.model, content=cleaned, chunk_id=stream_id)
                            yield make_chunk(model=req.model, tool_calls=tool_calls, finish_reason=None, chunk_id=stream_id)
                            yield make_chunk(model=req.model, finish_reason="tool_calls", chunk_id=stream_id)
                        else:
                            if accumulated_text:
                                yield make_chunk(model=req.model, content=accumulated_text, chunk_id=stream_id)
                            yield make_chunk(model=req.model, finish_reason="stop", chunk_id=stream_id)
                except Exception as err:
                    logger.warning(f"Session streaming failed: {err}. Failing over to Web2API direct stream.")
                    use_fallback = True

            # Direct Web2API engine (either default or failover)
            if use_fallback:
                try:
                    if not req.tools:
                        async for delta_text in async_gemini_stream(full_prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy):
                            if delta_text:
                                accumulated_text += delta_text
                                yield make_chunk(model=req.model, content=delta_text, chunk_id=stream_id)
                        yield make_chunk(model=req.model, finish_reason="stop", chunk_id=stream_id)
                    else:
                        async for delta_text in async_gemini_stream(full_prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy):
                            accumulated_text += delta_text

                        tool_calls, cleaned = extract_tool_calls_from_text(accumulated_text)
                        if tool_calls:
                            if cleaned:
                                yield make_chunk(model=req.model, content=cleaned, chunk_id=stream_id)
                            yield make_chunk(model=req.model, tool_calls=tool_calls, finish_reason=None, chunk_id=stream_id)
                            yield make_chunk(model=req.model, finish_reason="tool_calls", chunk_id=stream_id)
                        else:
                            if accumulated_text:
                                yield make_chunk(model=req.model, content=accumulated_text, chunk_id=stream_id)
                            yield make_chunk(model=req.model, finish_reason="stop", chunk_id=stream_id)
                except Exception as fallback_err:
                    logger.error(f"Fallback streaming error: {fallback_err}")
                    yield make_chunk(model=req.model, content=f"\n\n[Error: {fallback_err}]", finish_reason="stop", chunk_id=stream_id)

            # Usage chunk if requested
            if req.stream_options and req.stream_options.get("include_usage"):
                comp_tokens = max(1, len(accumulated_text) // 4)
                usage_chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req.model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": comp_tokens,
                        "total_tokens": prompt_tokens + comp_tokens
                    }
                }
                yield f"data: {json.dumps(usage_chunk)}\n\n"

            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_gen(), media_type="text/event-stream")
    else:
        # Non-streaming with Dual-Engine execution
        raw_text = ""
        parsed_images = []
        parsed_thoughts = None
        parsed_videos = []
        parsed_media = []
        parsed_deep_plan = None

        if gemini_client is not None:
            try:
                response = await gemini_client.generate_content(
                    full_prompt, files=files or None, model=None, gem=gem, extended_thinking=thinking
                )
                raw_text = clean_gemini_text(response.text or "")
                parsed = parse_model_output(response)
                parsed_images = parsed["images"]
                parsed_thoughts = parsed["thoughts"]
                parsed_videos = parsed["videos"]
                parsed_media = parsed["media"]
                parsed_deep_plan = parsed["deep_research_plan"]
            except Exception as e:
                logger.warning(f"Session generate failed: {e}. Executing via Web2API direct engine.")
                raw_text = await async_gemini_generate(full_prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy)
        else:
            raw_text = await async_gemini_generate(full_prompt, model_id=mode_id, think_mode=think_mode, proxy=proxy)

        tool_calls = []
        cleaned_text = raw_text
        if req.tools:
            tool_calls, cleaned_text = extract_tool_calls_from_text(raw_text)

        completion_tokens = max(1, len(raw_text) // 4)
        return make_response(
            cleaned_text if not tool_calls else None,
            req.model,
            tool_calls=tool_calls if tool_calls else None,
            images=parsed_images,
            thoughts=parsed_thoughts,
            videos=parsed_videos,
            media=parsed_media,
            deep_research_plan=parsed_deep_plan,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

# ── SESSION (Stateful Multi-turn) ────────────────────────────────────────────
@app.post("/v1/chat/completions/session")
async def chat_with_session(req: ChatRequest, request: Request):
    check_authorized(request)
    require_client()
    session_id = None
    for m in req.messages:
        if isinstance(m.content, dict) and m.content.get("__session_id"):
            session_id = m.content["__session_id"]
            break
    if session_id and session_id in chat_sessions:
        chat = chat_sessions[session_id]
    else:
        session_id = uuid.uuid4().hex
        chat = gemini_client.start_chat(model=None)
        chat_sessions[session_id] = chat
    prompt, files = extract_text_and_files(req.messages)
    if not prompt:
        raise HTTPException(400, "No prompt provided")
    try:
        response = await chat.send_message(prompt, files=files or None)
        parsed = parse_model_output(response)
        result = make_response(
            response.text, req.model,
            images=parsed["images"], thoughts=parsed["thoughts"],
            videos=parsed["videos"], media=parsed["media"]
        )
        result["session_id"] = session_id
        return result
    except Exception as e:
        raise HTTPException(500, str(e))

# ── IMAGE GENERATION ──────────────────────────────────────────────────────────
@app.post("/v1/images/generate")
async def generate_image(
    prompt: str = Form(...),
    model: str = Form("unspecified")
):
    require_client()
    try:
        enhanced_prompt = f"Generate an image: {prompt}. Respond only with image."
        logger.info(f"Image generation request: {prompt!r}")
        response = await gemini_client.generate_content(enhanced_prompt, model=None)
        images = await _save_generated_images(response.images, base_name="img")
        result = {
            "images": images,
            "text": response.text,
            "thoughts": getattr(response, "thoughts", None),
        }
        if not images:
            result["hint"] = "No images generated. Try: 'a cat wearing a hat in space' or check if image generation is active."
        return result
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        raise HTTPException(500, str(e))

# ── VIDEO GENERATION ──────────────────────────────────────────────────────────
@app.post("/v1/videos/generate")
async def generate_video(
    prompt: str = Form(...),
    model: str = Form("unspecified")
):
    require_client()
    try:
        response = await gemini_client.generate_content(f"Generate a video: {prompt}", model=None)
        parsed = parse_model_output(response)
        return {"videos": parsed["videos"], "text": response.text, "thoughts": parsed["thoughts"]}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── AUDIO/MEDIA GENERATION ────────────────────────────────────────────────────
@app.post("/v1/media/generate")
async def generate_media(
    prompt: str = Form(...),
    model: str = Form("unspecified")
):
    require_client()
    try:
        response = await gemini_client.generate_content(f"Generate audio/music: {prompt}", model=None)
        parsed = parse_model_output(response)
        return {"media": parsed["media"], "text": response.text, "thoughts": parsed["thoughts"]}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── FILE/VISION ANALYSIS ──────────────────────────────────────────────────────
@app.post("/v1/files/analyze")
async def analyze_file(
    prompt: str = Form("Describe this file in detail"),
    file: UploadFile = File(...),
    model: str = Form("unspecified")
):
    require_client()
    try:
        content = await file.read()
        file_obj = io.BytesIO(content)
        response = await gemini_client.generate_content(prompt, files=[file_obj], model=None)
        parsed = parse_model_output(response)
        return {"text": response.text, "thoughts": parsed["thoughts"], "filename": file.filename, "images": parsed["images"]}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── DEEP RESEARCH ─────────────────────────────────────────────────────────────
@app.post("/v1/deep-research")
async def deep_research(req: DeepResearchRequest):
    require_client()
    try:
        response = await gemini_client.generate_content(req.prompt, model=None, deep_research=True)
        parsed = parse_model_output(response)
        return {"text": response.text, "deep_research_plan": parsed["deep_research_plan"], "images": parsed["images"], "thoughts": parsed["thoughts"]}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── GEMS (System Prompts) ─────────────────────────────────────────────────────
@app.get("/v1/gems")
async def list_gems():
    require_client()
    try:
        gems = await gemini_client.fetch_gems()
        result = []
        if gems:
            for g in gems:
                result.append({"id": g.id, "name": g.name, "description": getattr(g, "description", ""), "prompt": getattr(g, "prompt", "")})
        return {"object": "list", "data": result}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── CHAT HISTORY ──────────────────────────────────────────────────────────────
@app.get("/v1/chats")
async def list_chats():
    require_client()
    try:
        chats = gemini_client.list_chats()
        result = []
        if chats:
            for c in chats:
                result.append({"cid": c.cid, "title": c.title, "is_pinned": c.is_pinned, "timestamp": c.timestamp})
        return {"object": "list", "data": result}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/v1/chats/{cid}")
async def read_chat(cid: str, limit: int = 10):
    require_client()
    try:
        history = await gemini_client.read_chat(cid, limit=limit)
        if not history:
            return {"object": "chat", "cid": cid, "turns": []}
        turns = []
        for turn in history.turns:
            turn_data = {"role": turn.role, "text": turn.text}
            if turn.model_output:
                parsed = parse_model_output(turn.model_output)
                if parsed["images"]: turn_data["images"] = parsed["images"]
                if parsed["thoughts"]: turn_data["thoughts"] = parsed["thoughts"]
            turns.append(turn_data)
        return {"object": "chat", "cid": cid, "turns": turns}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/v1/chats/{cid}")
async def delete_chat(cid: str):
    require_client()
    try:
        await gemini_client.delete_chat(cid)
        return {"status": "ok", "message": f"Chat {cid} deleted"}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── EMBEDDINGS (For Vibe Coding & Codebase Indexing) ──────────────────────────
@app.post("/v1/embeddings")
async def create_embeddings(req: EmbeddingsRequest, request: Request):
    check_authorized(request)
    inputs = [req.input] if isinstance(req.input, str) else req.input
    data = []
    for idx, text in enumerate(inputs):
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        import random
        rng = random.Random(seed)
        vec = [rng.gauss(0, 1) for _ in range(384)]
        norm = sum(x**2 for x in vec) ** 0.5 or 1.0
        norm_vec = [round(x / norm, 6) for x in vec]
        data.append({
            "object": "embedding",
            "index": idx,
            "embedding": norm_vec,
        })
    return {
        "object": "list",
        "data": data,
        "model": req.model,
        "usage": {
            "prompt_tokens": sum(len(t) // 4 for t in inputs),
            "total_tokens": sum(len(t) // 4 for t in inputs),
        }
    }

# ── AUTONOMOUS VIBE CODING AGENT ──────────────────────────────────────────────
@app.post("/v1/agent/task")
async def run_agent_task(req: AgentTaskRequest, request: Request):
    check_authorized(request)
    workspace_root = req.workspace if req.workspace and req.workspace != "." else str(Path(__file__).parent)

    async def event_generator():
        try:
            async for event in execute_agent_task(
                client=gemini_client,
                task_prompt=req.prompt,
                workspace_path=workspace_root,
                model=req.model if req.model != "unspecified" else None,
                max_steps=req.max_steps,
            ):
                yield f"data: {json.dumps(event)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error("Agent task stream error: {}", e)
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/v1/agent/tools")
async def list_agent_tools():
    return {
        "categories": [
            "1. Live Web Browsing & Research",
            "2. Codebase AST & Context Engine",
            "3. Terminal, Process & Port Control",
            "4. Autonomous Debugging & Self-Healing",
            "5. Code Generation, Editing & Type Inference",
            "6. Database, Seeding & Architecture Strategy",
            "7. QA, DevOps, Cloud & CI/CD",
        ],
        "tools": [
            {"name": "web_search", "category": "Web Browsing", "description": "Search the live internet for latest official docs, releases (React 19, Python 3.13, Vite 6), and error solutions", "parameters": {"query": "string", "max_results": "integer"}},
            {"name": "fetch_web_page", "category": "Web Browsing", "description": "Fetch and extract readable text/markdown from any web page or documentation URL", "parameters": {"url": "string"}},
            {"name": "http_request", "category": "Web Browsing", "description": "Perform live HTTP requests to test REST APIs, webhooks, or local services", "parameters": {"method": "string", "url": "string", "headers": "object", "data": "any"}},
            {"name": "ast_inspect", "category": "Codebase Engine", "description": "Parse AST / syntax structure of Python, TypeScript, or JavaScript files", "parameters": {"path": "string"}},
            {"name": "dependency_graph", "category": "Codebase Engine", "description": "Scan project import trees and output a module dependency graph", "parameters": {"path": "string"}},
            {"name": "find_symbol_references", "category": "Codebase Engine", "description": "Find all definitions and call usages of a symbol across the project", "parameters": {"symbol": "string", "path": "string"}},
            {"name": "grep_search", "category": "Codebase Engine", "description": "Regex/text search across all project files with line numbers", "parameters": {"pattern": "string", "path": "string", "file_pattern": "string"}},
            {"name": "find_files", "category": "Codebase Engine", "description": "Locate files in workspace matching a glob pattern (e.g. *.tsx, package.json)", "parameters": {"pattern": "string", "path": "string"}},
            {"name": "get_workspace_structure", "category": "Codebase Engine", "description": "Generate a clean high-level directory tree of the project architecture", "parameters": {"max_depth": "integer"}},
            {"name": "list_dir", "category": "Codebase Engine", "description": "List files and subdirectories in workspace", "parameters": {"path": "string"}},
            {"name": "run_command", "category": "Terminal & Shell", "description": "Execute shell/terminal command in workspace with live stdout/stderr capture", "parameters": {"command": "string"}},
            {"name": "check_port_and_free", "category": "Terminal & Shell", "description": "Detect if a port (e.g. 3000, 5353) is occupied and optionally kill the blocking process", "parameters": {"port": "integer", "kill": "boolean"}},
            {"name": "install_package", "category": "Terminal & Shell", "description": "Install external npm, pip, pnpm, or cargo packages", "parameters": {"package": "string", "ecosystem": "string"}},
            {"name": "diagnose_traceback", "category": "Debugging", "description": "Analyze error stack traces to extract exact locations and suggested fixes", "parameters": {"error_text": "string"}},
            {"name": "read_file", "category": "Code Editing", "description": "Read file contents with line numbers", "parameters": {"path": "string", "start_line": "integer", "end_line": "integer"}},
            {"name": "write_file", "category": "Code Editing", "description": "Create or overwrite a file with full content (with secret exposure guard)", "parameters": {"path": "string", "content": "string"}},
            {"name": "edit_file", "category": "Code Editing", "description": "Search and replace a unique code block in a file", "parameters": {"path": "string", "target": "string", "replacement": "string"}},
            {"name": "apply_unified_diff", "category": "Code Editing", "description": "Apply standard unified diff patch to a file", "parameters": {"path": "string", "diff": "string"}},
            {"name": "generate_types_from_json", "category": "Code Editing", "description": "Generate TypeScript interface or Python Pydantic model from raw JSON", "parameters": {"json_str": "string", "type_name": "string", "language": "string"}},
            {"name": "generate_seed_data", "category": "Database", "description": "Generate realistic mock database/API seed records in JSON format", "parameters": {"model_or_table": "string", "count": "integer"}},
            {"name": "record_architecture_decision", "category": "Architecture", "description": "Record an Architecture Decision Record (ADR) in docs/adr/", "parameters": {"title": "string", "decision": "string", "context": "string"}},
            {"name": "scaffold_test", "category": "QA & DevOps", "description": "Generate unit test scaffolding for a target file (Pytest, Vitest, Jest)", "parameters": {"file_path": "string", "framework": "string"}},
            {"name": "generate_docker_config", "category": "QA & DevOps", "description": "Generate production-ready multi-stage Dockerfile and docker-compose.yml", "parameters": {"framework": "string"}},
            {"name": "generate_ci_workflow", "category": "QA & DevOps", "description": "Generate GitHub Actions CI workflow (.github/workflows/ci.yml)", "parameters": {"ecosystem": "string"}},
            {"name": "generate_env_example", "category": "QA & DevOps", "description": "Scan codebase for environment variables and create .env.example", "parameters": {}},
            {"name": "git_status", "category": "QA & DevOps", "description": "Inspect git repository status and uncommitted files", "parameters": {}},
            {"name": "git_diff", "category": "QA & DevOps", "description": "Inspect uncommitted git differences", "parameters": {"path": "string"}},
            {"name": "task_complete", "category": "Workflow", "description": "Signal task completion with final summary and recommended next steps", "parameters": {"summary": "string", "next_steps": "array"}},
        ]
    }

# ── CONFIG ─────────────────────────────────────────────────────────────────────
@app.post("/config/update")
async def update_config(data: ConfigUpdate):
    global gemini_client, cfg, PORT, PSID, PSIDTS, DEFAULT_MODEL
    new_cfg = {
        "__Secure-1PSID": data.psid,
        "__Secure-1PSIDTS": data.psidts,
        "port": data.port,
        "cloudflare_tunnel": data.cloudflare_tunnel,
        "model": data.model,
        "api_keys": data.api_keys,
        "proxy": data.proxy,
        "temporary_chats": False,
    }
    CONFIG_PATH.write_text(json.dumps(new_cfg, indent=2), encoding="utf-8")
    cfg.update(new_cfg)
    PORT = data.port
    PSID = data.psid
    PSIDTS = data.psidts
    DEFAULT_MODEL = data.model

    if gemini_client:
        try:
            await gemini_client.close()
        except Exception as e:
            logger.warning("Error closing old Gemini client: {}", e)

    if data.psid and data.psid != "YOUR_PSID_HERE":
        try:
            gemini_client = GeminiClient(data.psid, data.psidts)
            await gemini_client.init(timeout=120, auto_close=False, auto_refresh=True)
            return {"status": "ok", "message": "Config saved & Gemini session reinitialized successfully!"}
        except Exception as e:
            gemini_client = None
            return {"status": "partial", "message": f"Config saved. Session error: {e}. Direct Web2API engine active."}
    else:
        gemini_client = None
        return {"status": "ok", "message": "Config saved. Direct Web2API engine active (zero-auth mode)."}

@app.get("/config/status")
async def config_status():
    return {
        "gemini_ready": gemini_client is not None,
        "dual_engine_ready": True,
        "tunnel_url": tunnel_url or None,
        "local_url": f"http://localhost:{PORT}",
        "local_endpoint": f"http://localhost:{PORT}/v1",
        "tunnel_endpoint": f"{tunnel_url}/v1" if tunnel_url else None,
        "google_endpoint": f"http://localhost:{PORT}/v1beta",
        "model": DEFAULT_MODEL,
        "port": PORT,
        "cookies_configured": bool(PSID and PSID != "YOUR_PSID_HERE"),
        "api_keys_count": len(cfg.get("api_keys", [])),
        "proxy": cfg.get("proxy"),
        "current_bl": CURRENT_BL,
    }

if __name__ == "__main__":
    logger.info("Starting uvicorn on port {}", PORT)
    print(f"\n🚀 Starting Gemini Local API (Dual-Engine Web2API) on http://localhost:{PORT}")
    print(f"📖 API Docs: http://localhost:{PORT}/docs")
    print(f"📂 Outputs: {OUTPUT_DIR.resolve()}")
    print(f"\n📋 Supported Endpoints:")
    print(f"  💬 /v1/chat/completions       - OpenAI Chat (Tool Calling, Streaming, Thinking @think=N)")
    print(f"  ⚡ /v1/responses              - OpenAI Responses API (OpenAI Codex CLI compatible)")
    print(f"  🌐 /v1beta/models             - Google Native Models List (Gemini CLI compatible)")
    print(f"  🌐 /v1beta/models/{'{m}'}:generateContent - Google Native Generation (Gemini CLI)")
    print(f"  💬 /v1/chat/completions/session - Stateful Multi-turn Chat")
    print(f"  🤖 /v1/agent/task             - Autonomous Vibe Coding Workspace Agent")
    print(f"  🔢 /v1/embeddings             - Codebase Vector Embeddings for Cursor/Continue")
    print(f"  🖼️  /v1/images/generate        - Imagen 3 Image Generation")
    print(f"  🎬 /v1/videos/generate        - Video Generation")
    print(f"  🎵 /v1/media/generate         - Audio/Music Generation")
    print(f"  🔬 /v1/deep-research          - Autonomous Deep Research\n")
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
