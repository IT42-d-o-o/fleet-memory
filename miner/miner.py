"""
Transcript miner — extracts facts/decisions/ideas from AI session transcripts
and project documentation (Markdown) and writes them to fleet memory (mem0).

Sources: Claude Code, Codex, Antigravity, Cursor, OpenClaw, Markdown docs, git logs,
         GitHub issues/commits, GitLab issues/commits, Gitea issues/commits.

Resumable: checkpoint.json tracks processed files by path+hash.
Run: python miner.py [--dry-run] [--since YYYY-MM-DD] [--limit N]
                     [--skip-subagents] [--workers N] [--model NAME]
                     [--markdown] [--markdown-roots PATH [PATH ...]]
                     [--git] [--git-roots PATH [PATH ...]]
                     [--github] [--github-orgs ORG ...] [--github-url URL]
                     [--gitlab] [--gitlab-groups GROUP ...] [--gitlab-url URL]
                     [--gitea] [--gitea-orgs ORG ...]
Note: --markdown and --git require --markdown-roots/--git-roots or PROJECTS_ROOT env var.
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from _llm import create_llm_client, push_tokens
from _forge import GitHubForgeClient, GitLabForgeClient, GiteaForgeClient, get_gitea_credentials, GIT_SKIP_SUBJECTS

# Force UTF-8 line-buffered stdout on Windows (fixes PowerShell console lag)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

CLAUDE_SESSIONS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
ANTIGRAVITY_BRAIN_ROOT = Path.home() / ".gemini" / "antigravity" / "brain"
CURSOR_PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
OPENCLAW_SESSIONS_ROOT = Path.home() / ".openclaw" / "agents"
CHECKPOINT_FILE = Path(__file__).parent / "checkpoint.json"
LOG_FILE = Path(__file__).parent / "miner.log"

FLEET_MEMORY_URL = os.getenv("FLEET_MEMORY_URL", "http://127.0.0.1:8800/mcp")
DEFAULT_MODEL = "qwen3:8b"

# Markdown mining — project repos (set PROJECTS_ROOT env var or pass --markdown-roots)
MARKDOWN_ROOTS_DEFAULT = None  # must be provided at runtime
# Root-level files to include from each repo
MARKDOWN_ROOT_FILES = {"CLAUDE.md", "AGENTS.md", "METHODOLOGY.md", "IDENTITY.md"}
# Subdirectory globs (relative to repo root, one level deep)
MARKDOWN_DOCS_GLOB = "docs/*.md"
# File names to always skip
MARKDOWN_SKIP_NAMES = {"README.md", "readme.md"}
# Directory names to skip during repo discovery
MARKDOWN_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv", "venv"}

# Git commit mining — set PROJECTS_ROOT env var or pass --git-roots
GIT_ROOTS_DEFAULT = None  # must be provided at runtime

# Gitea issue mining
GITEA_URL = os.getenv("GITEA_URL", "http://127.0.0.1:3000")
GITEA_ORGS_DEFAULT: list[str] = []
GITEA_SKIP_REPOS: set[str] = set()

# ~3000 words per chunk, rough estimate: 4 chars/word
CHUNK_CHARS = 12_000

EXTRACTION_SYSTEM_PROMPT = """You are an information extractor. Given a conversation transcript, extract atomic pieces of information that would be valuable to recall in FUTURE sessions with this user. Think: "would a future AI assistant benefit from knowing this?"

Categories:
- FACT: concrete facts about the user's setup, projects, infrastructure, tools, services, IPs, paths
- DECISION: architectural or implementation choices made and WHY (e.g. "chose X over Y because Z")
- PROJECT: completed milestones, stable goals, permanent blockers — NOT in-progress steps
- LESSON: what failed/succeeded and why — non-obvious, reusable insight applicable beyond this session
- TOOL: endpoints, file paths, Vault paths, env vars, service addresses
- USER: user preferences, working style, constraints, expertise

Output ONLY a JSON array of objects. Each object:
  "category": one of fact/decision/project/lesson/tool/user
  "content": atomic fact as a clear complete sentence (max 80 words)

STRICT SKIP LIST — output nothing for:
- In-progress coding steps, file edits, command execution narration ("I will now...", "Running...", "Patching...")
- TODO items, task lists, P1/P2/P3 priority items, backlog entries
- Git commit messages, commit hashes, or git log/status output
- File contents echoed by an agent (tool results, file reads)
- Errors that were resolved within the same session
- Small talk, confirmations, acknowledgements
- Anything already obvious from the code or standard tools
- Re-statements of existing documentation or memory files the agent read aloud
- Facts about Claude Code itself: its settings schema, plugin system, marketplace config, hooks syntax, skill framework — extract only facts about the USER's infrastructure, projects, and decisions
- Specific timestamps, scheduled event times, current file line counts, transient file paths used once
- Generic software documentation masquerading as facts (e.g. "flag X enables feature Y in tool Z" when Z is a standard tool, not user-built)

Keep content specific: include names, IPs, paths, version numbers when present.
NEVER include plaintext passwords or secrets — Vault path references only.
If nothing in this transcript meets the bar, return: []
Output valid JSON only, no markdown fences, no explanation text.

Example output:
[
  {"category": "tool", "content": "ComfyUI runs at 127.0.0.1:8188, started via: docker compose -f C:/AI/services/docker-compose/compose.comfyui.yml up -d"},
  {"category": "decision", "content": "Chose qwen3-coder:30b over qwen3:8b for transcript mining because extraction quality matters more than speed for institutional knowledge backfill"},
  {"category": "lesson", "content": "mem0 infer=True dedup does not catch near-duplicates from repeated file reads across sessions; pre-filter tool result content before sending to extraction LLM"}
]"""


MARKDOWN_SYSTEM_PROMPT = """You are an information extractor. Given a documentation file from a software project, extract atomic pieces of information that would be valuable for a future AI assistant to know about this project's infrastructure, architecture, and decisions.

Categories:
- FACT: concrete facts about setup, infrastructure, tools, services, IPs, paths, versions
- DECISION: architectural or implementation choices and WHY
- PROJECT: project goals, active status, permanent blockers, ownership
- LESSON: what failed/succeeded — non-obvious reusable insight applicable beyond this project
- TOOL: endpoints, file paths, Vault paths, env vars, service addresses, credentials references
- USER: user preferences, working constraints, process requirements

Output ONLY a JSON array of objects. Each object:
  "category": one of fact/decision/project/lesson/tool/user
  "content": atomic fact as a clear complete sentence (max 80 words)

STRICT SKIP LIST — output nothing for:
- Generic software documentation (standard tool behavior, not project-specific)
- Placeholder / example values (e.g. "example.com", "your-api-key", "TODO")
- Step-by-step procedures — extract the OUTCOME or DECISION, not the steps themselves
- Redundant facts obvious from names or standard conventions
- Anything already obvious from standard tooling (e.g. "Docker containers run in isolation")
- NEVER include plaintext passwords or secrets — Vault path references only

Keep content specific: include names, IPs, ports, versions, CT numbers when present.
If nothing meets the bar, return: []
Output valid JSON only, no markdown fences, no explanation text."""


GIT_SYSTEM_PROMPT = """You are an information extractor. Given a block of git commit messages from a software project, extract atomic pieces of information that would be valuable for a future AI assistant to know about this project's decisions, architecture, and lessons learned.

Categories:
- FACT: concrete facts about infrastructure, services, IPs, paths, versions established by this work
- DECISION: architectural or implementation choices made and WHY (chose X over Y because Z)
- PROJECT: milestones completed, features shipped, integrations done
- LESSON: what failed/succeeded — non-obvious reusable insight (bugs fixed with root cause, migration pitfalls)
- TOOL: endpoints, file paths, Vault paths, env vars, service addresses confirmed by this work

Output ONLY a JSON array of objects. Each object:
  "category": one of fact/decision/project/lesson/tool/user
  "content": atomic fact as a clear complete sentence (max 80 words)

STRICT SKIP LIST — output nothing for:
- Commits that only say "fix typo", "update README", "WIP", "chore: bump version" with no meaningful body
- Co-Authored-By lines — ignore completely
- Merge commits
- Generic refactoring with no architectural significance
- Anything that is already obvious from code naming conventions
- NEVER include plaintext passwords or secrets — Vault path references only

Prioritize commits with multi-line bodies — those contain the most valuable context.
If nothing meets the bar, return: []
Output valid JSON only, no markdown fences, no explanation text."""


GITEA_SYSTEM_PROMPT = """You are an information extractor. Given Gitea/GitHub issue discussions from a software project, extract atomic pieces of information valuable for a future AI assistant to know about this project's bugs, decisions, and lessons.

Categories:
- FACT: concrete facts about infrastructure, services, IPs, paths, versions confirmed in these issues
- DECISION: architectural or implementation choices made in issue discussions and WHY
- PROJECT: features shipped, bugs fixed, integrations completed (closed issues with resolution)
- LESSON: root causes of bugs, failed approaches, workarounds — non-obvious, reusable
- TOOL: endpoints, file paths, Vault paths, env vars, service addresses mentioned in issues
- USER: user preferences or constraints mentioned in issue discussions

Output ONLY a JSON array of objects. Each object:
  "category": one of fact/decision/project/lesson/tool/user
  "content": atomic fact as a clear complete sentence (max 80 words)

STRICT SKIP LIST — output nothing for:
- Generic feature requests with no implementation detail or resolution
- Questions with no resolved answers in the thread
- Dependency update issues (Dependabot, Renovate, auto-generated)
- "Done", "Fixed", "Merged" one-liners with zero context
- NEVER include plaintext passwords or secrets — Vault path references only

Prioritize closed issues with discussion bodies — those contain resolved, durable knowledge.
If nothing meets the bar, return: []
Output valid JSON only, no markdown fences, no explanation text."""


_log_lock = threading.Lock()
_mem_write_sem = threading.Semaphore(2)  # max 2 concurrent fleet-memory writes


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8-sig"))
    return {"processed": {}, "stats": {"files": 0, "facts": 0, "errors": 0}}


def save_checkpoint(cp: dict):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, ensure_ascii=False), encoding="utf-8")


def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    h.update(str(path).encode())
    h.update(str(path.stat().st_size).encode())
    h.update(str(path.stat().st_mtime).encode())
    return h.hexdigest()[:16]


def parse_claude_transcript(path: Path) -> str:
    """Extract human-readable conversation text from Claude .jsonl."""
    parts = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = obj.get("type")
            if msg_type == "user":
                msg = obj.get("message", {})
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        parts.append(f"USER: {content.strip()}")
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "").strip()
                                if t:
                                    parts.append(f"USER: {t}")
            elif msg_type == "assistant":
                msg = obj.get("message", {})
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        parts.append(f"CLAUDE: {content.strip()[:500]}")
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "").strip()
                                if t:
                                    parts.append(f"CLAUDE: {t[:500]}")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
    return "\n".join(parts)


def parse_codex_transcript(path: Path) -> str:
    """Extract human-readable conversation text from Codex .jsonl."""
    parts = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "response_item":
                payload = obj.get("payload", {})
                if payload.get("type") == "message":
                    role = payload.get("role", "")
                    content = payload.get("content", [])
                    label = "USER" if role == "user" else "CODEX"
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text", "").strip()
                            if not text:
                                text = block.get("content", "").strip()
                            if not text or len(text) <= 5:
                                continue
                            # Skip tool call echoes and tool result content (file reads, git output, etc.)
                            if text.startswith("[external_agent_tool_result") or text.startswith("[external_agent_tool_call"):
                                continue
                            max_len = 2000 if role == "user" else 500
                            parts.append(f"{label}: {text[:max_len]}")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
    return "\n".join(parts)


def parse_antigravity_transcript(path: Path) -> str:
    """Extract conversation text from Antigravity (Google) .jsonl transcript."""
    parts = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            source = obj.get("source", "")
            typ = obj.get("type", "")
            content = obj.get("content", "")
            thinking = obj.get("thinking", "")
            if source == "USER_EXPLICIT" and typ == "USER_INPUT":
                match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                if match:
                    text = match.group(1).strip()
                    if text:
                        parts.append(f"USER: {text}")
            elif source == "MODEL" and typ == "PLANNER_RESPONSE":
                if thinking:
                    parts.append(f"THINKING: {thinking.strip()[:300]}")
                if content and not obj.get("tool_calls"):
                    parts.append(f"ANTIGRAVITY: {content.strip()[:500]}")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
    return "\n\n".join(parts)


def parse_cursor_transcript(path: Path) -> str:
    """Extract conversation text from Cursor agent-transcript .jsonl."""
    parts = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role", "")
            msg = obj.get("message", {})
            content_blocks = msg.get("content", [])
            if not isinstance(content_blocks, list):
                continue
            label = "USER" if role == "user" else "CURSOR"
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "").strip()
                if not text or len(text) <= 5:
                    continue
                # Strip timestamp prefix from user messages: "[Mon 2026-...] actual text"
                text = re.sub(r"^\[.*?\]\s*", "", text).strip()
                # Extract user_query tag if present
                match = re.search(r"<user_query>(.*?)</user_query>", text, re.DOTALL)
                if match:
                    text = match.group(1).strip()
                if not text:
                    continue
                max_len = 2000 if role == "user" else 500
                parts.append(f"{label}: {text[:max_len]}")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
    return "\n".join(parts)


def parse_openclaw_transcript(path: Path) -> str:
    """Extract conversation text from OpenClaw session .jsonl."""
    parts = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "message":
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            label = "USER" if role == "user" else "OPENCLAW"
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "").strip()
                if not text or len(text) <= 5:
                    continue
                # Strip [timestamp] prefix
                text = re.sub(r"^\[.*?\]\s*", "", text).strip()
                if not text:
                    continue
                max_len = 2000 if role == "user" else 500
                parts.append(f"{label}: {text[:max_len]}")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
    return "\n".join(parts)


def parse_markdown_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log(f"  parse error {path.name}: {e}")
        return ""


def find_markdown_files(roots: list[Path]) -> list[tuple[Path, str]]:
    """Discover CLAUDE.md/AGENTS.md at repo root + docs/*.md for each repo under roots."""
    results = []
    for root in roots:
        if not root.exists():
            continue
        for repo_dir in sorted(root.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            if any(s in repo_dir.parts for s in MARKDOWN_SKIP_DIRS):
                continue
            for fname in MARKDOWN_ROOT_FILES:
                p = repo_dir / fname
                if p.exists():
                    results.append((p, "markdown"))
            docs_dir = repo_dir / "docs"
            if docs_dir.is_dir():
                for p in sorted(docs_dir.glob("*.md")):
                    if p.name not in MARKDOWN_SKIP_NAMES:
                        results.append((p, "markdown"))
    return sorted(results, key=lambda x: x[0].stat().st_mtime)


def parse_git_log(repo_path: Path) -> str:
    """Return formatted commit log text for extraction. Strips boilerplate lines."""
    try:
        result = subprocess.run(
            ["git", "log", "--no-merges", "--format=COMMIT: %s%n%b", "--all"],
            cwd=repo_path, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            return ""
        lines = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            # Drop Co-Authored-By and empty separator lines between commits
            if stripped.lower().startswith("co-authored-by"):
                continue
            subject_lower = stripped.lower()
            if stripped.startswith("COMMIT: "):
                subj = stripped[8:].lower()
                if any(subj.startswith(s) for s in GIT_SKIP_SUBJECTS):
                    continue
            lines.append(line)
        return "\n".join(lines).strip()
    except Exception as e:
        log(f"  git log error {repo_path.name}: {e}")
        return ""


def git_repo_hash(repo_path: Path) -> str:
    """Use HEAD commit hash as the repo's change fingerprint."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip()[:16] if r.returncode == 0 else ""
    except Exception:
        return ""


def find_git_repos(roots: list[Path]) -> list[tuple[Path, str]]:
    """Find all git repos (first-level subdirs with .git) under roots."""
    results = []
    for root in roots:
        if not root.exists():
            continue
        for repo_dir in sorted(root.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            if (repo_dir / ".git").exists():
                results.append((repo_dir, "git"))
    return sorted(results, key=lambda x: x[0].name)


def _extract_and_write(
    text: str, system_prompt: str, model: str, dry_run: bool,
    label: str
) -> int:
    """Chunk text, extract facts via LLM, write to fleet memory. Returns facts written."""
    chunks = chunk_text(text)
    facts_written = 0
    total_chunks = len(chunks)
    for batch_start in range(0, total_chunks, BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        batch_end = min(batch_start + BATCH_SIZE, total_chunks)
        log(f"  [{label}] chunks {batch_start+1}-{batch_end}/{total_chunks} ({sum(len(c) for c in batch)} chars) → ollama [{model}]")
        items = call_ollama(batch, model, system=system_prompt)
        log(f"  [{label}] extracted {len(items)} items")
        for item in items:
            cat = item.get("category", "fact").lower()
            content = item.get("content", "").strip()
            if not content or len(content) < 10:
                continue
            if add_to_fleet_memory(content, cat, dry_run):
                facts_written += 1
            time.sleep(0.1)
    return facts_written


def process_forge_repo(
    client, owner: str, repo: str,
    known_hash: str | None, dry_run: bool, model: str
) -> tuple[int, dict | None]:
    fhash = client.repo_fingerprint(owner, repo)
    if known_hash == fhash:
        return -1, None

    facts_written = 0

    issues_text = client.fetch_issues(owner, repo)
    if len(issues_text) >= 200:
        facts_written += _extract_and_write(issues_text, GITEA_SYSTEM_PROMPT, model, dry_run, "issues")

    commits_text = client.fetch_commits(owner, repo)
    if len(commits_text) >= 200:
        facts_written += _extract_and_write(commits_text, GIT_SYSTEM_PROMPT, model, dry_run, "commits")

    if facts_written == 0:
        return 0, {"file_hash": fhash, "facts_written": 0, "skipped": True}

    return facts_written, {
        "file_hash": fhash,
        "facts_written": facts_written,
        "processed_at": datetime.now().isoformat(),
        "source": client.prefix
    }


def run_forge_pipeline(
    client, forge_label: str, skip_repos: set[str],
    cp: dict, cp_lock: threading.Lock, args
) -> int:
    try:
        repo_list = client.list_repos()
    except Exception as e:
        log(f"{forge_label}: failed to list repos: {e}")
        return 0
    log(f"Found {len(repo_list)} {forge_label} repos")

    def _needs_update(item: tuple[str, str]) -> bool:
        owner, repo = item
        known = cp["processed"].get(f"{client.prefix}:{owner}/{repo}", {}).get("file_hash")
        return known != client.repo_fingerprint(owner, repo)

    fp_workers = min(8, max(len(repo_list), 1))
    with ThreadPoolExecutor(max_workers=fp_workers) as pool:
        needs = list(pool.map(_needs_update, repo_list))

    pending = [item for item, need in zip(repo_list, needs)
               if need and f"{item[0]}/{item[1]}" not in skip_repos]
    skipped_count = sum(1 for item in repo_list if f"{item[0]}/{item[1]}" in skip_repos)
    log(f"{forge_label}: {len(repo_list) - len(pending) - skipped_count} up-to-date, "
        f"{skipped_count} skipped, {len(pending)} pending")
    if args.limit:
        pending = pending[:args.limit]
        log(f"{forge_label}: limited to {len(pending)}")

    def run_one_forge(item: tuple[str, str]) -> int:
        owner, repo = item
        key = f"{client.prefix}:{owner}/{repo}"
        log(f"processing {forge_label} {owner}/{repo}")
        with cp_lock:
            known = cp["processed"].get(key, {}).get("file_hash")
        try:
            n, entry = process_forge_repo(client, owner, repo, known, args.dry_run, args.model)
            with cp_lock:
                if entry is not None:
                    cp["processed"][key] = entry
                cp["stats"]["files"] += 1
                if n > 0:
                    cp["stats"]["facts"] += n
                save_checkpoint(cp)
            if n == -1:
                log(f"  already up-to-date, skip")
            elif n == 0:
                log(f"  nothing extracted")
            else:
                log(f"  wrote {n} facts")
            return max(n, 0)
        except Exception as e:
            log(f"  ERROR: {e}")
            return 0

    facts = 0
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_one_forge, item): item for item in pending}
            for future in as_completed(futures):
                facts += future.result()
    else:
        for item in pending:
            facts += run_one_forge(item)
    return facts


def chunk_text(text: str) -> list[str]:
    """Split text into chunks of ~CHUNK_CHARS characters."""
    if len(text) <= CHUNK_CHARS:
        return [text] if text.strip() else []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_CHARS
        if end < len(text):
            # split at newline boundary
            boundary = text.rfind("\n", start, end)
            if boundary > start:
                end = boundary
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


BATCH_SIZE = 1  # chunks per Ollama request (batching degrades qwen3:8b quality)


def call_ollama(chunks: list[str], model: str, system: str | None = None) -> list[dict]:
    """Send chunks to LLM backend, return extracted items. Pushes token telemetry."""
    client = create_llm_client(model=model)
    prompt = system or EXTRACTION_SYSTEM_PROMPT
    if system == MARKDOWN_SYSTEM_PROMPT:
        label = "document"
    elif system == GIT_SYSTEM_PROMPT:
        label = "git commit log"
    elif system == GITEA_SYSTEM_PROMPT:
        label = "Gitea issues"
    else:
        label = "transcript"
    user_content = f"Extract information from this {label}:\n\n{chunks[0]}"
    try:
        items, response = client.extract_json(
            system=prompt,
            user=user_content,
        )
        push_tokens(model, response.input_tokens, response.output_tokens, app="transcript-miner")
        if len(items) != len([i for i in items if isinstance(i, dict)]):
            log(f"  salvaged objects from malformed JSON")
        return items
    except Exception as e:
        log(f"  ollama error: {e}")
        return []


def add_to_fleet_memory(content: str, category: str, dry_run: bool) -> bool:
    """Write one fact to fleet memory via MCP HTTP."""
    if dry_run:
        log(f"  [DRY] [{category}] {content[:80]}")
        return True
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "add_memory",
            "arguments": {
                "content": content,
                "agent": "transcript-miner",
                "metadata": {"category": category}
            }
        }
    }
    for attempt in range(3):
        try:
            with _mem_write_sem:
                resp = httpx.post(
                    FLEET_MEMORY_URL,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    timeout=60
                )
            resp.raise_for_status()
            body = resp.json()
            if body.get("result", {}).get("isError"):
                err = body["result"].get("content", [{}])[0].get("text", "unknown")
                log(f"  fleet memory write error: {err[:120]}")
                return False
            return True
        except Exception as e:
            if attempt < 2:
                time.sleep(8 * (attempt + 1))
            else:
                log(f"  fleet memory write error (gave up after 3 attempts): {e}")
                return False
    return False


def find_all_transcripts(since: datetime | None, skip_subagents: bool = False) -> list[tuple[Path, str]]:
    """Return list of (path, source_type) for all transcript .jsonl files."""
    results = []

    # Claude Code and Codex
    for root, source in [(CLAUDE_SESSIONS_ROOT, "claude"), (CODEX_SESSIONS_ROOT, "codex")]:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            if skip_subagents and "subagents" in path.parts:
                continue
            if since:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
            results.append((path, source))

    # Antigravity (Google) — ~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl
    if ANTIGRAVITY_BRAIN_ROOT.exists():
        for session_dir in ANTIGRAVITY_BRAIN_ROOT.iterdir():
            if not session_dir.is_dir():
                continue
            transcript = session_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if transcript.exists():
                if since:
                    mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
                    if mtime < since:
                        continue
                results.append((transcript, "antigravity"))

    # Cursor — ~/.cursor/projects/<workspace>/agent-transcripts/<uuid>/<uuid>.jsonl
    if CURSOR_PROJECTS_ROOT.exists():
        for workspace_dir in CURSOR_PROJECTS_ROOT.iterdir():
            at_root = workspace_dir / "agent-transcripts"
            if not at_root.exists():
                continue
            for path in at_root.rglob("*.jsonl"):
                if since:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    if mtime < since:
                        continue
                results.append((path, "cursor"))

    # OpenClaw — ~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl
    if OPENCLAW_SESSIONS_ROOT.exists():
        for agent_dir in OPENCLAW_SESSIONS_ROOT.iterdir():
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.exists():
                continue
            for path in sessions_dir.glob("*.jsonl"):
                # Skip trajectory files
                if path.name.endswith(".trajectory.jsonl"):
                    continue
                if since:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    if mtime < since:
                        continue
                results.append((path, "openclaw"))

    return sorted(results, key=lambda x: x[0].stat().st_mtime)


def process_file(path: Path, source: str, known_hash: str | None, dry_run: bool, model: str) -> tuple[int, dict | None]:
    """
    Process one transcript file.
    Returns (n_facts, checkpoint_entry) — entry is None if already done (hash matches).
    n_facts == -1 means already processed; 0 means too short.
    """
    fhash = git_repo_hash(path) if source == "git" else file_hash(path)
    if known_hash == fhash:
        return -1, None

    if source == "claude":
        text = parse_claude_transcript(path)
        system_prompt = None
    elif source == "codex":
        text = parse_codex_transcript(path)
        system_prompt = None
    elif source == "antigravity":
        text = parse_antigravity_transcript(path)
        system_prompt = None
    elif source == "cursor":
        text = parse_cursor_transcript(path)
        system_prompt = None
    elif source == "openclaw":
        text = parse_openclaw_transcript(path)
        system_prompt = None
    elif source == "markdown":
        text = parse_markdown_file(path)
        system_prompt = MARKDOWN_SYSTEM_PROMPT
    elif source == "git":
        text = parse_git_log(path)
        system_prompt = GIT_SYSTEM_PROMPT
    else:
        text = parse_codex_transcript(path)
        system_prompt = None

    if len(text) < 200:
        return 0, {"file_hash": fhash, "facts_written": 0, "skipped": True}

    chunks = chunk_text(text)
    facts_written = 0
    total_chunks = len(chunks)

    for batch_start in range(0, total_chunks, BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        batch_end = min(batch_start + BATCH_SIZE, total_chunks)
        log(f"  chunks {batch_start+1}-{batch_end}/{total_chunks} ({sum(len(c) for c in batch)} chars) → ollama [{model}]")
        items = call_ollama(batch, model, system=system_prompt)
        log(f"  extracted {len(items)} items")
        for item in items:
            cat = item.get("category", "fact").lower()
            content = item.get("content", "").strip()
            if not content or len(content) < 10:
                continue
            if add_to_fleet_memory(content, cat, dry_run):
                facts_written += 1
            time.sleep(0.1)

    return facts_written, {
        "file_hash": fhash,
        "facts_written": facts_written,
        "processed_at": datetime.now().isoformat(),
        "source": source
    }


def main():
    parser = argparse.ArgumentParser(description="Mine session transcripts into fleet memory")
    parser.add_argument("--dry-run", action="store_true", help="Extract but don't write to fleet memory")
    parser.add_argument("--since", help="Only process files newer than YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Max files to process this run")
    parser.add_argument("--skip-subagents", action="store_true", help="Skip files inside subagents/ subdirectories")
    parser.add_argument("--no-transcripts", action="store_true", help="Skip transcript processing (run only --git/--gitea sources)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker threads (default: 1)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument("--checkpoint", help="Custom checkpoint file path (default: checkpoint.json)")
    parser.add_argument("--markdown", action="store_true", help="Also mine markdown docs from project repos")
    parser.add_argument("--markdown-roots", nargs="+", type=Path, default=MARKDOWN_ROOTS_DEFAULT,
                        help="Root directories containing repos with markdown docs")
    parser.add_argument("--git", action="store_true", help="Also mine git commit logs from project repos")
    parser.add_argument("--git-roots", nargs="+", type=Path, default=GIT_ROOTS_DEFAULT,
                        help="Root directories containing git repos to mine")
    parser.add_argument("--github", action="store_true", help="Mine GitHub issues and commits (requires GITHUB_TOKEN)")
    parser.add_argument("--github-orgs", nargs="+", default=[], metavar="ORG",
                        help="GitHub orgs to mine (default: authenticated user's own repos)")
    parser.add_argument("--github-url", default="https://api.github.com",
                        help="GitHub API base URL (override for GitHub Enterprise)")
    parser.add_argument("--gitlab", action="store_true", help="Mine GitLab issues and commits (requires GITLAB_TOKEN)")
    parser.add_argument("--gitlab-groups", nargs="+", default=[], metavar="GROUP",
                        help="GitLab groups to mine (default: owned projects)")
    parser.add_argument("--gitlab-url", default="https://gitlab.com",
                        help="GitLab base URL (override for self-hosted)")
    parser.add_argument("--gitea", action="store_true", help="Mine Gitea issues and commits")
    parser.add_argument("--gitea-orgs", nargs="+", default=GITEA_ORGS_DEFAULT,
                        help="Gitea orgs to mine (default: ai repos)")
    parser.add_argument("--skip-repos", nargs="+", default=[],
                        help="Repos to skip, format owner/repo — applies to all forges")
    args = parser.parse_args()

    if args.checkpoint:
        global CHECKPOINT_FILE
        CHECKPOINT_FILE = Path(args.checkpoint)

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    cp = load_checkpoint()
    cp_lock = threading.Lock()

    all_files = [] if args.no_transcripts else find_all_transcripts(since, skip_subagents=args.skip_subagents)
    md_files = []
    git_repos = []
    if args.markdown:
        if not args.markdown_roots:
            projects = os.getenv("PROJECTS_ROOT")
            if not projects:
                print("ERROR: --markdown requires --markdown-roots or PROJECTS_ROOT env var", file=sys.stderr)
                sys.exit(1)
            args.markdown_roots = [Path(projects)]
        md_files = find_markdown_files(args.markdown_roots)
        log(f"Found {len(md_files)} markdown files across {len(args.markdown_roots)} roots")
    if args.git:
        if not args.git_roots:
            projects = os.getenv("PROJECTS_ROOT")
            if not projects:
                print("ERROR: --git requires --git-roots or PROJECTS_ROOT env var", file=sys.stderr)
                sys.exit(1)
            args.git_roots = [Path(projects)]
        git_repos = find_git_repos(args.git_roots)
        log(f"Found {len(git_repos)} git repos across {len(args.git_roots)} roots")
    all_files = all_files + md_files + git_repos

    def _get_hash(path: Path, source: str) -> str:
        return git_repo_hash(path) if source == "git" else file_hash(path)

    already_done = sum(
        1 for p, s in all_files
        if str(p) in cp["processed"] and cp["processed"][str(p)].get("file_hash") == _get_hash(p, s)
    )
    pending = [t for t in all_files if not (
        str(t[0]) in cp["processed"] and
        cp["processed"][str(t[0])].get("file_hash") == _get_hash(t[0], t[1])
    )]

    parts = [f"{len(all_files)-len(md_files)-len(git_repos)} transcripts"]
    if md_files:
        parts.append(f"{len(md_files)} markdown")
    if git_repos:
        parts.append(f"{len(git_repos)} git repos")
    log(f"Found {len(all_files)} files ({', '.join(parts)}): {already_done} done, {len(pending)} pending"
        + (f" [skipped subagents]" if args.skip_subagents else ""))
    if args.limit:
        pending = pending[:args.limit]
        log(f"Limited to {len(pending)} files")
    log(f"Model: {args.model}  Workers: {args.workers}")

    total_facts = 0

    def run_one(item: tuple[Path, str]) -> int:
        path, source = item
        key = str(path)
        rel = str(path).replace(str(Path.home()), "~")
        with cp_lock:
            known_hash = cp["processed"].get(key, {}).get("file_hash")
        log(f"processing {source} {rel}")
        try:
            n, entry = process_file(path, source, known_hash, args.dry_run, args.model)
            with cp_lock:
                if entry is not None:
                    cp["processed"][key] = entry
                cp["stats"]["files"] += 1
                if n > 0:
                    cp["stats"]["facts"] += n
                save_checkpoint(cp)
            if n == -1:
                log(f"  already processed, skip")
            elif n == 0:
                log(f"  too short, skip")
            else:
                log(f"  wrote {n} facts")
            return max(n, 0)
        except Exception as e:
            log(f"  ERROR: {e}")
            with cp_lock:
                cp["stats"]["errors"] += 1
                save_checkpoint(cp)
            return 0

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_one, item): item for item in pending}
            for future in as_completed(futures):
                total_facts += future.result()
    else:
        for item in pending:
            total_facts += run_one(item)

    skip_set = set(args.skip_repos)

    if args.github:
        gh_token = os.getenv("GITHUB_TOKEN", "")
        if not gh_token:
            log("GitHub: GITHUB_TOKEN not set — skipping")
        else:
            gh_client = GitHubForgeClient(token=gh_token, base_url=args.github_url, orgs=args.github_orgs)
            total_facts += run_forge_pipeline(gh_client, "github", skip_set, cp, cp_lock, args)

    if args.gitlab:
        gl_token = os.getenv("GITLAB_TOKEN", "")
        if not gl_token:
            log("GitLab: GITLAB_TOKEN not set — skipping")
        else:
            gl_client = GitLabForgeClient(token=gl_token, base_url=args.gitlab_url, groups=args.gitlab_groups)
            total_facts += run_forge_pipeline(gl_client, "gitlab", skip_set, cp, cp_lock, args)

    if args.gitea:
        gitea_token = os.getenv("GITEA_TOKEN", "")
        if gitea_token:
            gitea_client = GiteaForgeClient(base_url=GITEA_URL, orgs=args.gitea_orgs, token=gitea_token)
        else:
            cred_user, cred_pw = get_gitea_credentials(GITEA_URL)
            if not cred_user:
                log("Gitea: no GITEA_TOKEN and no git credential store credentials — skipping")
                gitea_client = None
            else:
                gitea_client = GiteaForgeClient(base_url=GITEA_URL, orgs=args.gitea_orgs, user=cred_user, password=cred_pw)
        if gitea_client:
            total_facts += run_forge_pipeline(gitea_client, "gitea", skip_set, cp, cp_lock, args)

    log(f"Done. {total_facts} new facts written. Total in checkpoint: {cp['stats']['facts']}")


if __name__ == "__main__":
    main()
