---
title: Gemini API Server
emoji: ⚡
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# ⚡ Gemini Local API & Enterprise Vibe Coding Platform

A state-of-the-art, high-performance, OpenAI and Google-compatible local API server powered by Google Gemini. Features a **Dual-Engine Architecture** (Zero-Auth Direct Web2API + Session Client), autonomous workspace coding agent (`agent.py`), reasoning depth control (`@think=N`), and native integration for **Kilo Code**, **Cline**, **Cursor**, **Continue.dev**, **Official Gemini CLI**, and **OpenAI Codex CLI**.

---

## 🌟 Top-Level Features

- **⚡ Dual-Engine Architecture (Zero-Downtime Guarantee)**:
  - **Engine A (Direct Web2API Protocol)**: Direct Google `StreamGenerate` protocol from `Sophomoresty/gemini-web2api`. Requires **ZERO AUTH** (anonymous mode) or cookie. Resilient against session expirations.
  - **Engine B (Gemini Session Client)**: Authenticated session for Google Gems, Deep Research, and Imagen 3 generation.
  - **Automatic Seamless Failover**: If cookies expire or are blank, chat completions automatically route through the direct Web2API engine without error!
- **🌐 Google Native API Endpoints (`/v1beta`)**:
  - `GET /v1beta/models`: Official Google Gemini model list.
  - `POST /v1beta/models/{model}:generateContent`: Direct Google Gemini generation.
  - `POST /v1beta/models/{model}:streamGenerateContent`: Server-Sent Events (SSE) in Google AI schema.
  - Full drop-in support for the official **Google Gemini CLI** (`gemini`).
- **⚡ OpenAI Responses API (`/v1/responses`)**:
  - Full event streaming for the **OpenAI Codex CLI** and modern developer tools.
- **🧠 Thinking Depth Control (`@think=N`)**:
  - Append `@think=0` for deepest reasoning and maximum output (~20k+ chars).
  - Supported on `gemini-3.5-flash-thinking`, `gemini-3.5-flash-thinking-lite`, `gemini-2.0-flash-thinking`, etc.
- **🤖 Built-in Autonomous Coding Agent (`agent.py`)**:
  - Automatically reads, writes, inspects files and runs terminal commands (`npm`, `python`, `git`) to solve end-to-end coding tasks with live execution tracing.
- **🛠️ Indexed Tool Calling & Streaming**:
  - Full OpenAI Function Calling schema with indexed streaming deltas for Cline, Kilo Code, Cursor, and Roo Code.
- **🛡️ Flexible Authentication (Zero-Auth by Default)**:
  - Supports optional `api_keys` in `config.json`. If empty, any client can connect without API keys. If populated, verifies `Bearer`, `x-api-key`, `x-goog-api-key`, or `?key=`.
- **🌐 HTTP Proxy Support**:
  - Configurable proxy (`proxy` in `config.json`) for users behind corporate firewalls or VPNs.
- **🔍 Vector Codebase Indexing (`/v1/embeddings`)**:
  - 384-dimensional vector embeddings for codebase search in Cursor and Continue.dev.
- **🎨 Glassmorphic Modern Web UI**:
  - Live interactive dashboard at `http://localhost:5353` with quick setup cards, real-time agent console, and model tester.

---

## 🚀 Quick Start

### 1. Start Server
Double-click **`start.bat`** (or run `.\start.bat`).  
The server starts on `http://localhost:5353` and launches the dashboard in your browser.

### 2. Stop Server
Double-click **`stop.bat`** (or run `.\stop.bat`) to cleanly terminate server and tunnel processes.

---

## 🔌 Connecting to Tools & IDEs

### ⚡ Kilo Code & Cline (VS Code)
1. Open Kilo Code or Cline in VS Code and click ⚙️ **Settings**.
2. Configure:
   - **Provider**: `OpenAI-Compatible`
   - **Base URL**: `http://localhost:5353/v1`
   - **API Key**: `gemini-local` (or any string)
   - **Model ID**: `gemini-3.6-flash` or `gemini-2.5-pro`
   - **Max Output Tokens**: `65536`

### 🖱️ Cursor IDE
1. Open Cursor Settings ➔ **Models**.
2. Enable **OpenAI API Key** and set **Override Base URL**: `http://localhost:5353/v1`.
3. Set API Key: `gemini-local`.
4. Model: `gemini-2.5-pro` or `gpt-4o`.

### 🌐 Official Gemini CLI
```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:5353
gemini
```

### ⚡ OpenAI Codex CLI
```bash
export OPENAI_BASE_URL=http://localhost:5353/v1
codex
```

### 🍒 Cherry Studio / ChatBox / NextChat
- **Base URL**: `http://localhost:5353/v1`
- **Model**: `gemini-3.5-flash-thinking@think=0` (Extended Thinking, ~20k+ chars)
- **API Key**: `any`

### ⏩ Continue.dev
Add to `~/.continue/config.json`:
```json
{
  "models": [
    {
      "title": "Gemini 3.6 Flash",
      "provider": "openai",
      "model": "gemini-3.6-flash",
      "apiBase": "http://localhost:5353/v1",
      "apiKey": "gemini-local"
    },
    {
      "title": "Gemini 3.5 Flash Thinking",
      "provider": "openai",
      "model": "gemini-3.5-flash-thinking@think=0",
      "apiBase": "http://localhost:5353/v1",
      "apiKey": "gemini-local"
    }
  ]
}
```

---

## 📋 Complete API Endpoints

| Endpoint | Method | Format | Description |
|---|---|---|---|
| `/health` | `GET` | JSON | Server health & dual-engine status |
| `/v1/models` | `GET` | OpenAI | OpenAI format model catalog |
| `/v1/chat/completions` | `POST` | OpenAI | Chat completions with tool calling & streaming |
| `/v1/responses` | `POST` | OpenAI | Responses API for OpenAI Codex CLI |
| `/v1beta/models` | `GET` | Google | Google AI Studio / Gemini CLI model list |
| `/v1beta/models/{model}:generateContent` | `POST` | Google | Google native generation |
| `/v1beta/models/{model}:streamGenerateContent` | `POST` | Google | Google native SSE streaming |
| `/v1/agent/task` | `POST` | SSE | Autonomous workspace coding agent |
| `/v1/agent/tools` | `GET` | JSON | List of workspace tools |
| `/v1/embeddings` | `POST` | OpenAI | Vector embeddings for code search |
| `/v1/images/generate` | `POST` | Multipart | Imagen 3 image generation |
| `/v1/videos/generate` | `POST` | Multipart | Video generation |
| `/v1/media/generate` | `POST` | Multipart | Music / Audio generation |
| `/v1/deep-research` | `POST` | JSON | Autonomous deep research synthesis |
| `/v1/gems` | `GET` | JSON | Custom Google Gems |
| `/v1/chats` | `GET/DELETE`| JSON | Cloud chat history management |

---

## ⚙️ Configuration (`config.json`)

```json
{
  "__Secure-1PSID": "YOUR_PSID_HERE",
  "__Secure-1PSIDTS": "YOUR_PSIDTS_HERE",
  "port": 5353,
  "cloudflare_tunnel": true,
  "model": "gemini-3.6-flash",
  "api_keys": [],
  "proxy": null,
  "temporary_chats": false
}
```

---

## 🏆 Model Catalog

| Model | Mode | Thinking Depth | Description |
|---|---|---|---|
| `gemini-3.7-flash` | Flash | Hybrid | Latest all-around model (Gemini 3.7 Flash) |
| `gemini-3.6-flash` | Flash | Fast | Default high-velocity model |
| `gemini-3.5-flash-thinking` | Thinking | Deepest (`@think=0`) | Longest output (~20k+ chars), deep chain-of-thought |
| `gemini-3.5-flash-thinking-lite`| Dynamic | Adaptive | Adaptive thinking depth |
| `gemini-3.1-pro` | Pro | Medium | Advanced coding and mathematics |
| `gemini-2.5-pro` | Pro | Deep | Flagship autonomous agent coding model |
| `gemini-2.5-flash` | Flash | Fast | Real-time vibe coding model |
| `gemini-latest` | Auto | Dynamic | Live auto-routing to Google's newest model |
| `gpt-4o` | Alias | - | Drop-in alias mapped to Gemini engine |
| `claude-3-7-sonnet` | Alias | Thinking | Drop-in alias mapped to Gemini thinking engine |

---

## 🤖 100+ Core Powers of the Autonomous Vibe Coding Agent

The built-in autonomous agent (`agent.py`) supports full-stack development across **10 core domains**:

### 1. 🌐 Live Web Browsing & Internet Research
- **Live DuckDuckGo / Web Scraping Engine**: Zero-rate-limit live web search (`web_search`) with clean markdown HTML extraction (`fetch_web_page`).
- **Real-Time REST API Testing**: `http_request` to verify local or remote endpoints, test webhooks, and inspect status codes.
- **Always Up-To-Date**: Researches React 19, Python 3.13, Vite 6, Tailwind v4, and Next.js 15 documentation directly from the web.

### 2. 🔍 Codebase Understanding & Context Engine
- **AST Parsing**: `ast_inspect` analyzes class definitions, methods, functions, arguments, return types, and docstrings across Python, JavaScript, and TypeScript.
- **Dynamic Dependency Graph**: `dependency_graph` traces import trees across modules.
- **Symbol Cross-Referencing**: `find_symbol_references` locates definitions and call sites of any symbol across all files.
- **Fast Text & Regex Search**: `grep_search` and `find_files` to quickly locate files and lines matching patterns.
- **Workspace Tree Architecture**: `get_workspace_structure` generates clean directory maps filtering `node_modules`, `.venv`, and `.git`.

### 3. 💻 Terminal, Shell & OS Execution
- **Async Process Management**: `run_command` executes powershell/bash commands with streaming capture and 90s timeout protection.
- **Port Collision Resolver**: `check_port_and_free` identifies occupied ports (e.g. 3000, 5173, 5353, 8000) and terminates blocking processes.
- **Package Management**: `install_package` auto-installs missing packages across `npm`, `pip`, `pnpm`, and `cargo`.

### 4. 🩺 Autonomous Debugging & Self-Healing
- **Traceback Extraction**: `diagnose_traceback` parses Python and Node/TypeScript error stack traces down to file, line, and exception type with fix actions.
- **Iterative Self-Repair Loop**: Agent runs code, detects failures, reads errors, modifies code, and re-verifies automatically.

### 5. 📝 Code Generation, Refactoring & Editing
- **Surgical Code Editing**: `edit_file` replaces exact unique code blocks.
- **Unified Diff Patching**: `apply_unified_diff` applies standard patches without rewriting whole files.
- **Full File Generation**: `write_file` writes complete production-ready code.
- **Secret Exposure Guard**: Detects OpenAI, Anthropic, Google API keys, GitHub tokens, and private RSA keys before writing to disk.
- **JSON-to-Types Generator**: `generate_types_from_json` converts raw JSON into TypeScript interfaces or Python Pydantic models.

### 6. 🗄️ Database & Backend Architecture
- **Realistic Seed Data Generation**: `generate_seed_data` produces structured mock JSON records for database seeding and API tests.
- **Architecture Decision Records**: `record_architecture_decision` formally documents decisions in `docs/adr/000X-title.md`.

### 7. 🧪 Testing & Quality Assurance
- **Unit Test Scaffolding**: `scaffold_test` auto-generates test suites for Pytest, Vitest, or Jest based on file AST and exports.
- **Automated Test Runs**: Executes test runners via `run_command` to verify functionality.

### 8. 🌐 API & Network Architecture
- **Environment Template Generator**: `generate_env_example` scans codebase for all referenced environment variables and outputs `.env.example`.
- **Live Endpoint Verification**: `http_request` tests `/health`, `/v1/models`, and app routes.

### 9. 🐳 DevOps, Cloud & Deployment
- **Multi-Stage Docker Setup**: `generate_docker_config` detects project type (Python, Node) and creates optimized multi-stage `Dockerfile` and `docker-compose.yml`.
- **CI/CD Pipeline Authoring**: `generate_ci_workflow` generates `.github/workflows/ci.yml` for linting and testing.

### 10. 🧠 Memory, Strategy & Workflow Intelligence
- **Git State Tracking**: `git_status` and `git_diff` monitor uncommitted modifications.
- **Safe Completion Loop**: `task_complete` provides a final verified summary and actionable next steps.
