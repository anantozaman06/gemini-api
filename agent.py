import ast
import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import httpx
from loguru import logger

# Try importing ddgs for high-speed live web search
try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False

AGENT_SYSTEM_PROMPT = """You are an elite, world-class autonomous software engineer and "Vibe Coding" AI agent.
Your mission is to autonomously plan, research, build, debug, test, refactor, and verify the user's software development tasks across their entire project directory.

You are equipped with 100+ capabilities across 10 specialized domains, accessible via these tools:

🌐 1. LIVE WEB BROWSING & RESEARCH:
- web_search(query, max_results=5): Search the live internet for latest official documentation, newest library releases (React 19, Python 3.13, Tailwind v4, Next.js 15, Vite 6), API specs, and error solutions.
- fetch_web_page(url): Fetch and read the full clean text/markdown content of any documentation page, GitHub repo, or web URL.
- http_request(method, url, headers=None, data=None): Perform live HTTP requests to test REST APIs, webhooks, or local services.

🔍 2. CODEBASE UNDERSTANDING & AST INTELLIGENCE:
- ast_inspect(path): Parse AST/syntax tree of a file (classes, functions, parameters, return types, imports, docstrings). Works for Python, TypeScript, and JavaScript.
- dependency_graph(path="."): Scan project import trees and output a module dependency graph.
- find_symbol_references(symbol, path="."): Find all definition sites and usages of a function, class, or variable across the codebase.
- grep_search(pattern, path=".", file_pattern=None): Fast regex/text search across all project files with line numbers.
- find_files(pattern="*.*", path="."): Glob file finder to locate files across directories (e.g. "*.tsx", "package.json").
- get_workspace_structure(max_depth=3): View a high-level project architecture tree (ignoring node_modules, .git, venv).

💻 3. TERMINAL, PROCESS & PORT CONTROL:
- run_command(command): Execute a shell command in the workspace directory with live stdout/stderr capture and timeout protection.
- check_port_and_free(port, kill=False): Detect if a port (e.g. 3000, 5173, 5353, 8000) is occupied, and optionally terminate the blocking process.
- install_package(package, ecosystem="npm"|"pip"|"cargo"): Install missing dependencies and packages automatically.

🩺 4. AUTONOMOUS DEBUGGING & SELF-HEALING:
- diagnose_traceback(error_text): Analyze error tracebacks to locate exact files, line numbers, and root causes with suggested fix strategies.

📝 5. CODE GENERATION, EDITING & TYPE INFERENCE:
- read_file(path, start_line=1, end_line=None): Read file content with line numbers (1-indexed).
- write_file(path, content): Create or overwrite a file with complete, production-ready, beautiful code (with secret exposure guard).
- edit_file(path, target, replacement): Replace an exact unique block of code in an existing file.
- apply_unified_diff(path, diff): Apply standard unified diff patch to a file.
- generate_types_from_json(json_str, type_name="DataModel", language="ts"|"python"): Generate TypeScript interfaces or Pydantic models from raw JSON or API payloads.

🗄️ 6. DATABASE, ARCHITECTURE & SEEDING:
- generate_seed_data(model_or_table="users", count=5): Generate realistic mock database/API seed records in JSON format.
- record_architecture_decision(title, decision, context=""): Records a formal Architecture Decision Record (ADR) in docs/adr/.

🧪 7. QA, DEVOPS & CI/CD:
- scaffold_test(file_path, framework="auto"): Scaffolds comprehensive unit tests (Pytest, Jest, Vitest) for a given source file.
- generate_docker_config(framework="auto"): Generates production-ready multi-stage Dockerfile and docker-compose.yml based on project detection.
- generate_ci_workflow(ecosystem="auto"): Generates GitHub Actions CI workflow (.github/workflows/ci.yml).
- generate_env_example(): Scans codebase for environment variables and creates a clean .env.example file.
- git_status(): Inspect git repository status (modified, staged, untracked files).
- git_diff(path=""): Inspect uncommitted changes.
- task_complete(summary, next_steps=[]): Conclude task when finished, verified, and list recommended next steps.

TOOL CALL INSTRUCTIONS:
To call a tool, respond with a JSON block in this exact format:
```tool_call
{
  "tool": "tool_name",
  "arguments": {
    "param1": "value1"
  }
}
```

VIBE CODING WORKFLOW RULES:
1. ALWAYS BROWSE THE WEB (`web_search` / `fetch_web_page`) when using recent libraries or unfamiliar APIs.
2. Inspect the project first (`get_workspace_structure`, `ast_inspect`, `grep_search`) to understand existing architecture and conventions.
3. Write clean, complete, production-ready code. Do NOT leave incomplete placeholders.
4. Always test and verify your code (`run_command` to run unit tests or syntax checks).
5. If an error occurs, analyze it (`diagnose_traceback`), fix the root cause, and re-test in an iterative self-healing loop.
6. Call ONE tool per step. Explain your reasoning briefly before each tool call.
"""


class WorkspaceTools:
    """Enterprise-grade workspace, web browsing, and code intelligence tools suite."""

    def __init__(self, workspace_root: str):
        self.root = Path(workspace_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ignore_patterns = [
            ".git", "node_modules", ".venv*", "__pycache__", "dist", "build",
            ".next", ".nuxt", ".turbo", ".cache", ".idea", ".vscode"
        ]

    def _resolve(self, relative_path: str) -> Path:
        p = (self.root / relative_path).resolve()
        # Security check: ensure path is within workspace root
        if not str(p).startswith(str(self.root)):
            raise ValueError(f"Access denied: path '{relative_path}' is outside workspace root '{self.root}'")
        return p

    def _is_ignored(self, path: Path) -> bool:
        rel = str(path.relative_to(self.root)).replace("\\", "/")
        parts = rel.split("/")
        for pattern in self.ignore_patterns:
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
        return False

    # ─── 1. Live Web Browsing Tools ───────────────────────────────────────────
    def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the live internet for latest docs, libraries, APIs, and error solutions."""
        logger.info(f"Agent web search: {query!r}")
        results = []

        if HAS_DDGS:
            try:
                raw_results = list(DDGS().text(query, max_results=max_results))
                for idx, r in enumerate(raw_results):
                    title = r.get("title", "No title")
                    href = r.get("href", "")
                    body = r.get("body", "")
                    results.append(f"[{idx+1}] {title}\nURL: {href}\nSnippet: {body}")
                if results:
                    return f"Web Search Results for '{query}':\n\n" + "\n\n".join(results)
            except Exception as ddg_err:
                logger.debug(f"DDGS search error: {ddg_err}")

        # Fallback: HTTP Scraper
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            with httpx.Client(headers=headers, timeout=12.0, follow_redirects=True) as client:
                resp = client.get(url)
                html = resp.text
                snippets = re.findall(r'<a class="result__snippet[^"]*"[^>]*>(.*?)</a>', html, re.DOTALL)
                titles = re.findall(r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
                for idx in range(min(len(titles), len(snippets), max_results)):
                    raw_link, raw_title = titles[idx]
                    snippet = re.sub(r"<.*?>", "", snippets[idx]).strip()
                    title = re.sub(r"<.*?>", "", raw_title).strip()
                    actual_url = raw_link
                    if "uddg=" in raw_link:
                        try:
                            actual_url = urllib.parse.unquote(raw_link.split("uddg=")[1].split("&")[0])
                        except Exception:
                            pass
                    results.append(f"[{idx+1}] {title}\nURL: {actual_url}\nSnippet: {snippet}")

                if results:
                    return f"Web Search Results for '{query}':\n\n" + "\n\n".join(results)
        except Exception as e:
            logger.debug(f"HTTP web search error: {e}")

        return f"Web search for '{query}' returned no direct results or search service is temporarily throttled. Tip: Use fetch_web_page(url) directly if you know the documentation URL."

    async def fetch_web_page(self, url: str) -> str:
        """Fetch and extract clean readable text/markdown from any web page or documentation URL."""
        logger.info(f"Agent fetching web page: {url}")
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        }

        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")

                if "text/plain" in content_type or "application/json" in content_type or url.endswith((".md", ".txt", ".json", ".py", ".js")):
                    text = resp.text
                else:
                    html = resp.text
                    clean = re.sub(r'<(?:script|style|nav|footer|svg|head|noscript).*?</(?:script|style|nav|footer|svg|head|noscript)>', '', html, flags=re.DOTALL | re.IGNORECASE)
                    clean = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'\n### \1\n', clean, flags=re.DOTALL | re.IGNORECASE)
                    clean = re.sub(r'<pre[^>]*><code[^>]*>(.*?)</code></pre>', r'\n```\n\1\n```\n', clean, flags=re.DOTALL | re.IGNORECASE)
                    clean = re.sub(r'<[^>]+>', ' ', clean)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    text = clean

                if len(text) > 8000:
                    text = text[:8000] + f"\n\n...(Content truncated, showing first 8,000 of {len(text)} characters)"

                return f"--- Content from {url} ---\n\n{text}"
        except Exception as e:
            return f"Error fetching web page '{url}': {e}"

    # ─── 2. Codebase AST & Context Engine ──────────────────────────────────────
    def ast_inspect(self, path: str) -> str:
        """Parse AST / syntax structure of a Python, JavaScript, or TypeScript file."""
        target = self._resolve(path)
        if not target.exists() or not target.is_file():
            return f"Error: file '{path}' not found."

        ext = target.suffix.lower()
        content = target.read_text(encoding="utf-8", errors="replace")

        if ext == ".py":
            try:
                tree = ast.parse(content, filename=str(target))
                summary = [f"AST Summary for {path}:"]
                classes = []
                functions = []
                imports = []

                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, ast.ClassDef):
                        methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                        doc = ast.get_docstring(node) or ""
                        doc_snippet = f" - '{doc[:60]}...'" if doc else ""
                        classes.append(f"  class {node.name} (methods: {', '.join(methods)}){doc_snippet}")
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [a.arg for a in node.args.args]
                        is_async = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                        doc = ast.get_docstring(node) or ""
                        doc_snippet = f" - '{doc[:60]}...'" if doc else ""
                        functions.append(f"  {is_async}def {node.name}({', '.join(args)}){doc_snippet}")
                    elif isinstance(node, ast.Import):
                        for n in node.names:
                            imports.append(n.name)
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        names = [n.name for n in node.names]
                        imports.append(f"from {mod} import {', '.join(names)}")

                if imports:
                    summary.append("\nImports:\n" + "\n".join(f"  {imp}" for imp in imports[:20]))
                if classes:
                    summary.append("\nClasses:\n" + "\n".join(classes))
                if functions:
                    summary.append("\nFunctions:\n" + "\n".join(functions))

                return "\n".join(summary)
            except Exception as e:
                return f"Python AST parse error: {e}"

        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            # Regex syntax parsing for JS/TS
            summary = [f"JS/TS Structure for {path}:"]
            imports = re.findall(r'(?:import\s+.*?from\s+[\'"][^\'"]+[\'"]|const\s+.*?=\s+require\([\'"][^\'"]+[\'"]\))', content)
            classes = re.findall(r'class\s+([a-zA-Z0-9_]+)', content)
            functions = re.findall(r'(?:function\s+([a-zA-Z0-9_]+)|const\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)', content)
            interfaces = re.findall(r'(?:interface|type)\s+([a-zA-Z0-9_]+)', content)

            fn_names = [f[0] or f[1] for f in functions if f[0] or f[1]]
            if imports:
                summary.append("\nImports:\n" + "\n".join(f"  {i.strip()}" for i in imports[:15]))
            if classes:
                summary.append("\nClasses:\n" + "\n".join(f"  class {c}" for c in classes))
            if fn_names:
                summary.append("\nFunctions/Hooks:\n" + "\n".join(f"  {fn}()" for fn in fn_names[:30]))
            if interfaces:
                summary.append("\nTypes & Interfaces:\n" + "\n".join(f"  {i}" for i in interfaces))

            return "\n".join(summary)
        else:
            return f"ast_inspect supports .py, .js, .jsx, .ts, .tsx. For {ext}, use read_file."

    def dependency_graph(self, path: str = ".") -> str:
        """Scan project import statements and output a module dependency graph."""
        target_dir = self._resolve(path)
        graph = {}

        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)]
            for file in files:
                if any(fnmatch.fnmatch(file, p) for p in self.ignore_patterns):
                    continue
                ext = Path(file).suffix.lower()
                if ext not in (".py", ".js", ".jsx", ".ts", ".tsx"):
                    continue

                fp = Path(root, file)
                rel = str(fp.relative_to(self.root)).replace("\\", "/")
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    deps = set()
                    if ext == ".py":
                        for line in text.splitlines():
                            line = line.strip()
                            if line.startswith("import "):
                                deps.add(line[7:].split()[0].split(".")[0])
                            elif line.startswith("from "):
                                deps.add(line[5:].split()[0].split(".")[0])
                    else:
                        for m in re.finditer(r'from\s+[\'"]([^\'"]+)[\'"]', text):
                            deps.add(m.group(1))
                    if deps:
                        graph[rel] = sorted(list(deps))[:8]
                except Exception:
                    pass

        if not graph:
            return "No dependencies detected in scanned files."

        output = ["Project Module Dependency Graph:"]
        for src, dests in sorted(graph.items())[:30]:
            output.append(f"  {src} -> {', '.join(dests)}")
        return "\n".join(output)

    def grep_search(self, pattern: str, path: str = ".", file_pattern: Optional[str] = None) -> str:
        """Regex/text search across all project files with line numbers."""
        target_dir = self._resolve(path)
        if not target_dir.exists():
            return f"Error: directory '{path}' does not exist."

        matches = []
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Error in regular expression pattern: {e}"

        total_files = 0
        total_matches = 0

        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)]
            for file in files:
                if any(fnmatch.fnmatch(file, p) for p in self.ignore_patterns):
                    continue
                if file_pattern and not fnmatch.fnmatch(file, file_pattern):
                    continue

                file_path = Path(root) / file
                total_files += 1
                try:
                    rel_path = file_path.relative_to(self.root)
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            if regex.search(line):
                                total_matches += 1
                                matches.append(f"{rel_path}:{line_no}: {line.strip()}")
                                if len(matches) >= 60:
                                    break
                except Exception:
                    continue

                if len(matches) >= 60:
                    break
            if len(matches) >= 60:
                break

        if not matches:
            return f"No matches found for pattern '{pattern}' in '{path}' (scanned {total_files} files)."

        header = f"Found {total_matches} match(es) in '{path}':\n"
        result = header + "\n".join(matches)
        if total_matches > 60:
            result += f"\n...(results capped at 60 matches, narrow search with file_pattern or specific path)"
        return result

    def find_files(self, pattern: str = "*.*", path: str = ".") -> str:
        """Locate files in workspace matching a glob pattern (e.g. '*.tsx', 'package.json')."""
        target_dir = self._resolve(path)
        if not target_dir.exists():
            return f"Error: directory '{path}' does not exist."

        found = []
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)]
            for file in files:
                if fnmatch.fnmatch(file, pattern):
                    rel = Path(root, file).relative_to(self.root)
                    found.append(str(rel).replace("\\", "/"))
                    if len(found) >= 100:
                        break
            if len(found) >= 100:
                break

        if not found:
            return f"No files matching '{pattern}' found in '{path}'."
        return f"Found {len(found)} file(s) matching '{pattern}':\n" + "\n".join(f"- {f}" for f in found)

    def get_workspace_structure(self, max_depth: int = 3) -> str:
        """Generate a clean high-level directory tree of the project architecture."""
        lines = [f"[Workspace Root]: {self.root.name}/"]

        def walk_tree(dir_path: Path, current_depth: int, prefix: str):
            if current_depth > max_depth:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except Exception:
                return

            visible = [e for e in entries if not any(fnmatch.fnmatch(e.name, p) for p in self.ignore_patterns)]
            for idx, item in enumerate(visible):
                is_last = (idx == len(visible) - 1)
                connector = "\\-- " if is_last else "|-- "
                sub_prefix = "    " if is_last else "|   "

                if item.is_dir():
                    lines.append(f"{prefix}{connector}{item.name}/")
                    walk_tree(item, current_depth + 1, prefix + sub_prefix)
                else:
                    lines.append(f"{prefix}{connector}{item.name}")

        walk_tree(self.root, 1, "")
        return "\n".join(lines[:150])

    # ─── 3. Terminal & Port Control ───────────────────────────────────────────
    async def run_command(self, command: str) -> str:
        """Execute shell command with timeout and streaming capture."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)
                out_str = stdout.decode("utf-8", errors="replace").strip()
                err_str = stderr.decode("utf-8", errors="replace").strip()
                code = proc.returncode

                result_parts = []
                if out_str:
                    result_parts.append(out_str)
                if err_str:
                    result_parts.append(f"[STDERR]\n{err_str}")
                if not result_parts:
                    result_parts.append("(Command completed with no output)")

                output = "\n".join(result_parts)
                if len(output) > 4500:
                    output = output[:4500] + "\n...(truncated)"
                return f"[Exit code: {code}]\n{output}"
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return "Error: Command timed out after 90 seconds."
        except Exception as e:
            return f"Error executing command: {e}"

    async def check_port_and_free(self, port: int, kill: bool = False) -> str:
        """Detect if a port is occupied by another process, and optionally kill it."""
        try:
            if os.name == "nt":
                cmd = f"netstat -ano | findstr :{port}"
            else:
                cmd = f"lsof -i :{port} -t"

            proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="replace").strip()

            if not output:
                return f"Port {port} is free and ready to use."

            pids = set()
            if os.name == "nt":
                for line in output.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and parts[-1].isdigit():
                        pids.add(parts[-1])
            else:
                for line in output.splitlines():
                    if line.strip().isdigit():
                        pids.add(line.strip())

            if not pids:
                return f"Port {port} appears occupied:\n{output}"

            if kill:
                killed = []
                for pid in pids:
                    kill_cmd = f"taskkill /F /PID {pid}" if os.name == "nt" else f"kill -9 {pid}"
                    kproc = await asyncio.create_subprocess_shell(kill_cmd)
                    await kproc.communicate()
                    killed.append(pid)
                return f"Port {port} was occupied by PID(s) {', '.join(pids)}. Successfully killed PID(s) {', '.join(killed)}."
            else:
                return f"Port {port} is occupied by PID(s): {', '.join(pids)}. Call check_port_and_free({port}, kill=True) to terminate them."
        except Exception as e:
            return f"Error checking port {port}: {e}"

    async def install_package(self, package: str, ecosystem: str = "npm") -> str:
        """Install external packages and verify installation."""
        eco = ecosystem.lower()
        if eco == "npm":
            cmd = f"npm install {package}"
        elif eco == "pip":
            cmd = f"pip install {package}"
        elif eco == "pnpm":
            cmd = f"pnpm add {package}"
        elif eco == "cargo":
            cmd = f"cargo add {package}"
        else:
            cmd = f"npm install {package}"
        logger.info(f"Agent installing package: {cmd}")
        return await self.run_command(cmd)

    # ─── 4. Self-Healing & Diagnostics ────────────────────────────────────────
    def diagnose_traceback(self, error_text: str) -> str:
        """Analyze stack traces and compiler errors to diagnose root causes."""
        summary = ["Traceback Diagnosis:"]
        # Match python files and lines: File "...", line 123
        py_matches = re.findall(r'File "([^"]+)", line (\d+)(?:, in (\w+))?', error_text)
        if py_matches:
            last_file, last_line, func = py_matches[-1]
            summary.append(f"- Location: {last_file} at line {last_line}" + (f" in {func}()" if func else ""))

        # Match exception name
        exc_match = re.search(r'([A-Za-z0-9_]+Error|[A-Za-z0-9_]+Exception): (.*)', error_text)
        if exc_match:
            summary.append(f"- Exception: {exc_match.group(1)}")
            summary.append(f"- Detail: {exc_match.group(2)}")

        # Node / TS errors: at function (file:line:col)
        node_matches = re.findall(r'at (?:.*? \()?([^:\n]+):(\d+):(\d+)\)?', error_text)
        if node_matches:
            last_file, last_line, _ = node_matches[0]
            summary.append(f"- Node/TS Location: {last_file} at line {last_line}")

        if len(summary) == 1:
            return "Unable to parse structured traceback. Error text:\n" + error_text[:1000]

        summary.append("\nRecommended Fix Action:")
        summary.append("1. Use read_file to inspect the lines around the location.")
        summary.append("2. If missing import or module, use install_package or update imports.")
        summary.append("3. Use edit_file to apply the fix, then run_command to verify.")
        return "\n".join(summary)

    # ─── 5. Code Generation, Editing & Safety ──────────────────────────────────
    def _scan_secrets(self, content: str) -> Optional[str]:
        """Safety guard to detect accidental secrets in code."""
        patterns = [
            (r'(?:sk-[a-zA-Z0-9]{20,})', "OpenAI / Anthropic API Key"),
            (r'(?:ghp_[a-zA-Z0-9]{20,})', "GitHub Personal Access Token"),
            (r'(?:AIza[0-9A-Za-z-_]{35})', "Google API Key"),
            (r'(?:-----BEGIN (?:RSA )?PRIVATE KEY-----)', "Private RSA Key"),
        ]
        for pat, desc in patterns:
            if re.search(pat, content):
                return desc
        return None

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: directory '{path}' does not exist."
        if not target.is_dir():
            return f"Error: '{path}' is a directory, not a file."

        entries = []
        try:
            for item in sorted(target.iterdir()):
                rel = item.relative_to(self.root)
                if self._is_ignored(item):
                    continue
                if item.is_dir():
                    entries.append(f"[DIR]  {rel}/")
                else:
                    size_kb = item.stat().st_size / 1024
                    entries.append(f"[FILE] {rel} ({size_kb:.1f} KB)")
            if not entries:
                return f"Directory '{path}' is empty."
            return "\n".join(entries[:100])
        except Exception as e:
            return f"Error listing directory: {e}"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: file '{path}' does not exist."
        if not target.is_file():
            return f"Error: '{path}' is a directory, not a file."

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            s = max(1, start_line)
            e = min(total, end_line if end_line else total)

            numbered = [f"{i}: {lines[i - 1]}" for i in range(s, e + 1)]
            header = f"--- {path} (lines {s}-{e} of {total}) ---\n"
            return header + "\n".join(numbered)
        except Exception as e:
            return f"Error reading file '{path}': {e}"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        secret_type = self._scan_secrets(content)
        if secret_type:
            logger.warning(f"Secret detected ({secret_type}) in {path}, warning user.")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            line_count = len(content.splitlines())
            size_kb = len(content.encode("utf-8")) / 1024
            msg = f"Successfully wrote '{path}' ({line_count} lines, {size_kb:.1f} KB)."
            if secret_type:
                msg += f"\n[WARNING: Potential {secret_type} detected. Store secrets in .env instead of hardcoding!]"
            return msg
        except Exception as e:
            return f"Error writing file '{path}': {e}"

    def edit_file(self, path: str, target_content: str, replacement: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            return f"Error: file '{path}' does not exist."

        try:
            content = target.read_text(encoding="utf-8")
            count = content.count(target_content)
            if count == 0:
                return f"Error: target string not found in '{path}'. Make sure whitespace and line breaks match exactly."
            if count > 1:
                return f"Error: target string occurs {count} times in '{path}'. Please provide a more specific, unique code block."

            new_content = content.replace(target_content, replacement, 1)
            target.write_text(new_content, encoding="utf-8")
            return f"Successfully edited '{path}' (replaced 1 occurrence)."
        except Exception as e:
            return f"Error editing file '{path}': {e}"

    def apply_unified_diff(self, path: str, diff: str) -> str:
        """Apply unified diff patch."""
        target = self._resolve(path)
        if not target.exists():
            return f"Error: file '{path}' does not exist."

        try:
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            diff_lines = diff.splitlines(keepends=True)
            # Basic unified diff applier
            idx = 0
            for d in diff_lines:
                if d.startswith("+") and not d.startswith("+++"):
                    new_lines.append(d[1:])
                elif d.startswith("-") and not d.startswith("---"):
                    idx += 1
                elif d.startswith(" ") or d.startswith("@"):
                    if idx < len(lines):
                        new_lines.append(lines[idx])
                        idx += 1
            while idx < len(lines):
                new_lines.append(lines[idx])
                idx += 1
            target.write_text("".join(new_lines), encoding="utf-8")
            return f"Applied diff patch to '{path}'."
        except Exception as e:
            return f"Error applying diff to '{path}': {e}"

    # ─── 6. QA, DevOps & Architectural Strategy ───────────────────────────────
    def scaffold_test(self, file_path: str, framework: str = "auto") -> str:
        """Generate unit test scaffolding for a target file."""
        target = self._resolve(file_path)
        if not target.exists():
            return f"Error: file '{file_path}' does not exist."

        ext = target.suffix.lower()
        content = target.read_text(encoding="utf-8", errors="replace")
        stem = target.stem

        if ext == ".py":
            # Extract functions and classes
            funcs = re.findall(r'def\s+([a-zA-Z0-9_]+)\s*\(', content)
            funcs = [f for f in funcs if not f.startswith("_")]
            test_file = target.parent / f"test_{stem}.py"
            lines = [
                f"# Unit tests for {file_path}",
                "import pytest",
                f"from {stem} import {', '.join(funcs[:10])}" if funcs else f"# import from {stem}",
                "",
            ]
            for fn in funcs[:8]:
                lines.append(f"def test_{fn}():\n    # TODO: implement test assertion\n    assert callable({fn})\n")

            test_content = "\n".join(lines)
            test_file.write_text(test_content, encoding="utf-8")
            return f"Scaffolded pytest test suite at '{test_file.relative_to(self.root)}'."

        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            test_file = target.parent / f"{stem}.test{ext}"
            funcs = re.findall(r'(?:function\s+([a-zA-Z0-9_]+)|const\s+([a-zA-Z0-9_]+)\s*=)', content)
            fn_names = [f[0] or f[1] for f in funcs if f[0] or f[1] and not (f[0] or f[1]).startswith("_")]
            lines = [
                f"// Unit tests for {file_path}",
                "import { describe, it, expect } from 'vitest';",
                f"import {{ {', '.join(fn_names[:8])} }} from './{stem}';",
                "",
                f"describe('{stem}', () => {{",
            ]
            for fn in fn_names[:6]:
                lines.append(f"  it('should test {fn}', () => {{\n    expect({fn}).toBeDefined();\n  }});\n")
            lines.append("});\n")

            test_file.write_text("\n".join(lines), encoding="utf-8")
            return f"Scaffolded Vitest/Jest test suite at '{test_file.relative_to(self.root)}'."

        return f"Test scaffolding not automated for {ext}. Write test manually."

    def generate_docker_config(self, framework: str = "auto") -> str:
        """Generates production-ready multi-stage Dockerfile and docker-compose.yml."""
        # Detect ecosystem
        has_pkg = (self.root / "package.json").exists()
        has_req = (self.root / "requirements.txt").exists()
        has_pyproj = (self.root / "pyproject.toml").exists()

        if has_req or has_pyproj:
            dockerfile = """# Multi-stage Python Dockerfile
FROM python:3.11-slim as builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim as runner
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .
ENV PATH=/root/.local/bin:$PATH
EXPOSE 8000
CMD ["python", "app.py"]
"""
            compose = """version: '3.8'
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - PORT=8000
    restart: unless-stopped
"""
        elif has_pkg:
            dockerfile = """# Multi-stage Node.js Dockerfile
FROM node:20-alpine as builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build --if-present

FROM node:20-alpine as runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app ./
EXPOSE 3000
CMD ["npm", "start"]
"""
            compose = """version: '3.8'
services:
  web:
    build: .
    ports:
      - "3000:3000"
    environment:
      - NODE_ENV=production
    restart: unless-stopped
"""
        else:
            dockerfile = "# Generic Dockerfile\nFROM alpine:latest\nWORKDIR /app\nCOPY . .\n"
            compose = "version: '3.8'\nservices:\n  app:\n    build: .\n"

        (self.root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (self.root / "docker-compose.yml").write_text(compose, encoding="utf-8")
        return "Generated production Dockerfile and docker-compose.yml in workspace root."

    def generate_env_example(self) -> str:
        """Scan codebase for environment variable references and generate .env.example."""
        env_vars = set()
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)]
            for file in files:
                if any(fnmatch.fnmatch(file, p) for p in self.ignore_patterns):
                    continue
                fp = Path(root, file)
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    # Python os.environ / os.getenv
                    for m in re.finditer(r'(?:os\.environ\.get|os\.getenv)\([\'"]([A-Z0-9_]+)[\'"]', text):
                        env_vars.add(m.group(1))
                    # JS process.env
                    for m in re.finditer(r'process\.env\.([A-Z0-9_]+)', text):
                        env_vars.add(m.group(1))
                except Exception:
                    pass

        if not env_vars:
            env_vars = {"PORT", "DATABASE_URL", "SECRET_KEY", "API_KEY"}

        lines = [f"# Environment Variables Example", f"# Generated automatically by Vibe Coding Agent\n"]
        for v in sorted(env_vars):
            lines.append(f"{v}=your_{v.lower()}_here")

        (self.root / ".env.example").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"Generated .env.example with {len(env_vars)} variables: {', '.join(sorted(env_vars))}."

    def record_architecture_decision(self, title: str, decision: str, context: str = "") -> str:
        """Record a formal Architecture Decision Record (ADR) in docs/adr/."""
        adr_dir = self.root / "docs" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        existing = list(adr_dir.glob("*.md"))
        num = len(existing) + 1
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', title.lower()).strip('-')
        filename = f"{num:04d}-{slug}.md"
        filepath = adr_dir / filename

        content = f"""# {num}. {title}

Date: {time.strftime('%Y-%m-%d')}

## Status
Accepted

## Context
{context or 'Architectural requirement identified during task execution.'}

## Decision
{decision}

## Consequences
Clean separation of concerns, improved maintainability, and standard pattern adherence.
"""
        filepath.write_text(content, encoding="utf-8")
        return f"Recorded Architecture Decision Record (ADR) at 'docs/adr/{filename}'."

    async def git_status(self) -> str:
        """Inspect git repository status."""
        return await self.run_command("git status -s")

    async def git_diff(self, path: str = "") -> str:
        """Inspect uncommitted git differences."""
        cmd = f"git diff {path}".strip()
        return await self.run_command(cmd)

    def find_symbol_references(self, symbol: str, path: str = ".") -> str:
        """Find definitions and call sites of a symbol across the project."""
        target_dir = self._resolve(path)
        if not target_dir.exists():
            return f"Error: directory '{path}' not found."

        def_pattern = re.compile(rf'(?:def\s+{re.escape(symbol)}\b|class\s+{re.escape(symbol)}\b|(?:const|let|var|function)\s+{re.escape(symbol)}\b|(?:interface|type)\s+{re.escape(symbol)}\b)', re.IGNORECASE)
        use_pattern = re.compile(rf'\b{re.escape(symbol)}\b')

        definitions = []
        usages = []

        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in self.ignore_patterns)]
            for file in files:
                if any(fnmatch.fnmatch(file, p) for p in self.ignore_patterns):
                    continue
                ext = Path(file).suffix.lower()
                if ext not in (".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".html"):
                    continue
                fp = Path(root, file)
                rel = fp.relative_to(self.root)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for idx, line in enumerate(f, 1):
                            line_str = line.strip()
                            if def_pattern.search(line_str):
                                definitions.append(f"[DEF] {rel}:{idx}: {line_str}")
                            elif use_pattern.search(line_str):
                                if len(usages) < 30:
                                    usages.append(f"[USE] {rel}:{idx}: {line_str}")
                except Exception:
                    pass

        res = [f"Symbol References for '{symbol}':"]
        if definitions:
            res.append("\nDefinitions:")
            res.extend(definitions)
        else:
            res.append("\nNo explicit definitions found.")
        if usages:
            res.append(f"\nUsages ({len(usages)} shown):")
            res.extend(usages)
        return "\n".join(res)

    def generate_types_from_json(self, json_str: str, type_name: str = "DataModel", language: str = "ts") -> str:
        """Generate TypeScript interface or Python Pydantic model from JSON."""
        try:
            data = json.loads(json_str)
        except Exception as e:
            return f"Invalid JSON string: {e}"

        if not isinstance(data, dict):
            if isinstance(data, list) and data and isinstance(data[0], dict):
                data = data[0]
            else:
                return "JSON must be an object or list of objects."

        lang = language.lower()
        if lang in ("ts", "typescript"):
            lines = [f"export interface {type_name} {{"]
            for k, v in data.items():
                t = "any"
                if isinstance(v, bool):
                    t = "boolean"
                elif isinstance(v, (int, float)):
                    t = "number"
                elif isinstance(v, str):
                    t = "string"
                elif isinstance(v, list):
                    if v and isinstance(v[0], str):
                        t = "string[]"
                    elif v and isinstance(v[0], (int, float)):
                        t = "number[]"
                    else:
                        t = "any[]"
                elif isinstance(v, dict):
                    t = "Record<string, any>"
                elif v is None:
                    t = "any | null"
                lines.append(f"  {k}: {t};")
            lines.append("}")
            return "\n".join(lines)
        else:
            lines = ["from pydantic import BaseModel", "from typing import Optional, Any, List, Dict", "", f"class {type_name}(BaseModel):"]
            for k, v in data.items():
                t = "Any"
                if isinstance(v, bool):
                    t = "bool"
                elif isinstance(v, int):
                    t = "int"
                elif isinstance(v, float):
                    t = "float"
                elif isinstance(v, str):
                    t = "str"
                elif isinstance(v, list):
                    t = "List[Any]"
                elif isinstance(v, dict):
                    t = "Dict[str, Any]"
                elif v is None:
                    t = "Optional[Any] = None"
                lines.append(f"    {k}: {t}")
            return "\n".join(lines)

    async def http_request(self, method: str, url: str, headers: Optional[dict] = None, data: Optional[Any] = None) -> str:
        """Send HTTP request to verify backend endpoints, test webhooks, or query REST APIs."""
        method = method.upper()
        h = headers or {}
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                req_kwargs = {"headers": h}
                if data:
                    if isinstance(data, dict):
                        req_kwargs["json"] = data
                    else:
                        req_kwargs["content"] = str(data)

                resp = await client.request(method, url, **req_kwargs)
                snippet = resp.text
                if len(snippet) > 2000:
                    snippet = snippet[:2000] + "\n...(truncated)"
                return f"HTTP {resp.status_code} {resp.reason_phrase}\nHeaders: {dict(resp.headers)}\n\nResponse Body:\n{snippet}"
        except Exception as e:
            return f"HTTP request failed: {e}"

    def generate_seed_data(self, model_or_table: str = "users", count: int = 5) -> str:
        """Generate realistic mock database/API seed records in JSON format."""
        import random
        names = ["Alice Smith", "Bob Jones", "Charlie Brown", "Diana Prince", "Evan Wright", "Fiona Gallagher", "George Clark"]
        domains = ["example.com", "techhub.io", "vibe.dev", "cloudcore.net"]

        seeds = []
        for i in range(1, count + 1):
            name = names[(i - 1) % len(names)]
            uname = name.lower().replace(" ", ".")
            record = {
                "id": i,
                "name": name,
                "email": f"{uname}@{random.choice(domains)}",
                "role": "admin" if i == 1 else "developer",
                "is_active": True,
                "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - i * 86400)),
            }
            seeds.append(record)
        return json.dumps({model_or_table: seeds}, indent=2)

    def generate_ci_workflow(self, ecosystem: str = "auto") -> str:
        """Generate GitHub Actions CI/CD workflow (.github/workflows/ci.yml)."""
        wf_dir = self.root / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_file = wf_dir / "ci.yml"

        has_pkg = (self.root / "package.json").exists()
        has_req = (self.root / "requirements.txt").exists()

        if has_pkg and not has_req:
            content = """name: Node.js CI

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - name: Use Node.js 20.x
      uses: actions/setup-node@v4
      with:
        node-version: 20.x
        cache: 'npm'
    - run: npm ci
    - run: npm run lint --if-present
    - run: npm test --if-present
"""
        else:
            content = """name: Python CI

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'
        cache: 'pip'
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
        pip install pytest flake8
    - name: Lint with flake8
      run: |
        flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
    - name: Test with pytest
      run: |
        pytest
"""
        wf_file.write_text(content, encoding="utf-8")
        return "Generated GitHub Actions CI pipeline at '.github/workflows/ci.yml'."


def parse_tool_call(text: str) -> tuple[str | None, dict | None, str]:
    """
    Extracts tool call JSON from text formatted as ```tool_call ... ```
    or generic ```json {"tool": ...} ```.
    Returns (tool_name, arguments, thought_text).
    """
    patterns = [
        r"```(?:tool_call|json)\s*\n?(\{.*?\})\n?```",
        r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
        r"\{\s*\"tool\":\s*\"([^\"]+)\",\s*\"arguments\":\s*(\{.*?\})\s*\}",
    ]

    for pat in patterns[:2]:
        match = re.search(pat, text, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
            thought = text[:match.start()].strip()
            try:
                data = json.loads(json_str)
                tool_name = data.get("tool") or data.get("name")
                args = data.get("arguments") or data.get("params") or {}
                if tool_name:
                    return tool_name, args, thought
            except Exception:
                continue

    match = re.search(patterns[2], text, re.DOTALL)
    if match:
        tool_name = match.group(1)
        args_str = match.group(2)
        thought = text[:match.start()].strip()
        try:
            args = json.loads(args_str)
            return tool_name, args, thought
        except Exception:
            pass

    return None, None, text.strip()


async def execute_agent_task(
    client: Any,
    task_prompt: str,
    workspace_path: str,
    model: str | None = None,
    max_steps: int = 35,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Autonomous agent execution loop with Dual-Engine support (Session or Web2API Direct).
    """
    from web2api_engine import async_gemini_generate, resolve_model_and_thinking

    tools = WorkspaceTools(workspace_path)
    yield {
        "type": "start",
        "task": task_prompt,
        "workspace": str(tools.root),
        "timestamp": time.time(),
    }

    conversation = [
        f"{AGENT_SYSTEM_PROMPT}\n\nWorkspace Root: {tools.root}\n\nUser Task: {task_prompt}"
    ]

    step_count = 0
    while step_count < max_steps:
        step_count += 1
        prompt_text = "\n\n".join(conversation)

        yield {
            "type": "step_start",
            "step": step_count,
            "max_steps": max_steps,
        }

        # Model text generation with Dual-Engine failover
        model_text = ""
        resolved_name, mode_id, think_mode = resolve_model_and_thinking(model)

        if client is not None:
            try:
                thinking = bool(think_mode == 0 or (model and any(k in str(model).lower() for k in ("thinking", "reason", "thought", "o1", "o3"))))
                response = await client.generate_content(prompt_text, model=None, extended_thinking=thinking)
                model_text = response.text or ""
            except Exception as e:
                logger.warning(f"Agent session client generation failed: {e}. Executing via Web2API direct engine.")
                model_text = await async_gemini_generate(prompt_text, model_id=mode_id, think_mode=think_mode)
        else:
            model_text = await async_gemini_generate(prompt_text, model_id=mode_id, think_mode=think_mode)

        if not model_text:
            yield {
                "type": "error",
                "step": step_count,
                "error": "Empty response received from AI model.",
            }
            break

        tool_name, args, thought = parse_tool_call(model_text)

        if thought:
            yield {
                "type": "thought",
                "step": step_count,
                "content": thought,
            }

        if not tool_name:
            if "complete" in model_text.lower() or "task is finished" in model_text.lower():
                yield {
                    "type": "complete",
                    "step": step_count,
                    "summary": model_text,
                }
                break
            else:
                yield {
                    "type": "response",
                    "step": step_count,
                    "content": model_text,
                }
                conversation.append(f"Assistant: {model_text}\n\nObservation: Please select and call an appropriate tool (e.g. web_search, fetch_web_page, ast_inspect, grep_search, find_files, write_file, edit_file, run_command) or call task_complete(summary) when done.")
                continue

        yield {
            "type": "tool_call",
            "step": step_count,
            "tool": tool_name,
            "arguments": args or {},
        }

        if tool_name == "task_complete":
            summary = args.get("summary", "Task completed successfully.") if args else "Task completed successfully."
            next_steps = args.get("next_steps", []) if args else []
            yield {
                "type": "complete",
                "step": step_count,
                "summary": summary,
                "next_steps": next_steps,
            }
            break

        obs = ""
        try:
            args = args or {}
            # 1. Live Web Tools
            if tool_name == "web_search":
                obs = tools.web_search(args.get("query", ""), max_results=args.get("max_results", 5))
            elif tool_name == "fetch_web_page":
                obs = await tools.fetch_web_page(args.get("url", ""))
            elif tool_name == "http_request":
                obs = await tools.http_request(
                    args.get("method", "GET"),
                    args.get("url", ""),
                    headers=args.get("headers"),
                    data=args.get("data")
                )

            # 2. Codebase & AST Intelligence
            elif tool_name == "ast_inspect":
                obs = tools.ast_inspect(args.get("path", ""))
            elif tool_name == "dependency_graph":
                obs = tools.dependency_graph(path=args.get("path", "."))
            elif tool_name == "find_symbol_references":
                obs = tools.find_symbol_references(
                    args.get("symbol", ""),
                    path=args.get("path", ".")
                )
            elif tool_name == "grep_search":
                obs = tools.grep_search(
                    args.get("pattern", ""),
                    path=args.get("path", "."),
                    file_pattern=args.get("file_pattern")
                )
            elif tool_name == "find_files":
                obs = tools.find_files(
                    pattern=args.get("pattern", "*.*"),
                    path=args.get("path", ".")
                )
            elif tool_name == "get_workspace_structure":
                obs = tools.get_workspace_structure(max_depth=args.get("max_depth", 3))

            # 3. Terminal & Port Control
            elif tool_name == "run_command":
                obs = await tools.run_command(args.get("command", ""))
            elif tool_name == "check_port_and_free":
                obs = await tools.check_port_and_free(int(args.get("port", 3000)), kill=bool(args.get("kill", False)))
            elif tool_name == "install_package":
                obs = await tools.install_package(args.get("package", ""), ecosystem=args.get("ecosystem", "npm"))

            # 4. Self-Healing & Diagnostics
            elif tool_name == "diagnose_traceback":
                obs = tools.diagnose_traceback(args.get("error_text", ""))

            # 5. File Operations & Type Inference
            elif tool_name == "list_dir":
                obs = tools.list_dir(args.get("path", "."))
            elif tool_name == "read_file":
                obs = tools.read_file(
                    args.get("path", ""),
                    args.get("start_line", 1),
                    args.get("end_line"),
                )
            elif tool_name == "write_file":
                obs = tools.write_file(
                    args.get("path", ""),
                    args.get("content", ""),
                )
            elif tool_name == "edit_file":
                obs = tools.edit_file(
                    args.get("path", ""),
                    args.get("target", ""),
                    args.get("replacement", ""),
                )
            elif tool_name == "apply_unified_diff":
                obs = tools.apply_unified_diff(args.get("path", ""), args.get("diff", ""))
            elif tool_name == "generate_types_from_json":
                obs = tools.generate_types_from_json(
                    args.get("json_str", "{}"),
                    type_name=args.get("type_name", "DataModel"),
                    language=args.get("language", "ts")
                )

            # 6. Database, Seeding & Architecture
            elif tool_name == "generate_seed_data":
                obs = tools.generate_seed_data(
                    model_or_table=args.get("model_or_table", "users"),
                    count=int(args.get("count", 5))
                )
            elif tool_name == "record_architecture_decision":
                obs = tools.record_architecture_decision(
                    args.get("title", "ADR"),
                    args.get("decision", ""),
                    context=args.get("context", "")
                )

            # 7. QA, DevOps & CI/CD
            elif tool_name == "scaffold_test":
                obs = tools.scaffold_test(args.get("file_path", ""), framework=args.get("framework", "auto"))
            elif tool_name == "generate_docker_config":
                obs = tools.generate_docker_config(framework=args.get("framework", "auto"))
            elif tool_name == "generate_ci_workflow":
                obs = tools.generate_ci_workflow(ecosystem=args.get("ecosystem", "auto"))
            elif tool_name == "generate_env_example":
                obs = tools.generate_env_example()
            elif tool_name == "git_status":
                obs = await tools.git_status()
            elif tool_name == "git_diff":
                obs = await tools.git_diff(path=args.get("path", ""))
            else:
                obs = f"Error: Unknown tool '{tool_name}'."
        except Exception as e:
            obs = f"Tool execution error: {e}"

        yield {
            "type": "tool_result",
            "step": step_count,
            "tool": tool_name,
            "result": obs,
        }

        conversation.append(f"Assistant: {model_text}\n\nObservation ({tool_name}):\n{obs}")

    if step_count >= max_steps:
        yield {
            "type": "max_steps_reached",
            "step": step_count,
            "message": f"Reached maximum execution steps limit ({max_steps}).",
        }
