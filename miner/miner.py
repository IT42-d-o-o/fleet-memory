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
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
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
from prompts import EXTRACTION_SYSTEM_PROMPT, MARKDOWN_SYSTEM_PROMPT, GIT_SYSTEM_PROMPT, GITEA_SYSTEM_PROMPT

# Force UTF-8 line-buffered stdout on Windows (fixes PowerShell console lag)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

CLAUDE_SESSIONS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
ANTIGRAVITY_BRAIN_ROOT = Path.home() / ".gemini" / "antigravity" / "brain"
CURSOR_PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
OPENCLAW_SESSIONS_ROOT = Path.home() / ".openclaw" / "agents"
LOG_FILE = Path(__file__).parent / "miner.log"

FLEET_MEMORY_URL = os.getenv("FLEET_MEMORY_URL", "http://127.0.0.1:8800/mcp")


def _infer_default_model() -> str | None:
    if m := os.getenv("LLM_MODEL"):
        return m
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude-haiku-4-5-20251001"
    if os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return "gpt-4o-mini"
    if os.getenv("OLLAMA_URL"):
        return "qwen3:8b"
    return None


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

_mem_write_sem = threading.Semaphore(2)  # max 2 concurrent fleet-memory writes

logger = logging.getLogger("miner")


def load_checkpoint(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"processed": {}, "stats": {"files": 0, "facts": 0, "errors": 0}}


def save_checkpoint(cp: dict, path: Path):
    path.write_text(json.dumps(cp, indent=2, ensure_ascii=False), encoding="utf-8")


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
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
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
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
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
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
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
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
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
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
    return "\n".join(parts)


def parse_markdown_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.error(f"parse error {path.name}: {type(e).__name__}: {e}")
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
            logger.error(f"git log failed in {repo_path.name} (rc={result.returncode}): {result.stderr.strip()[:200]}")
            return ""
        lines = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            # Drop Co-Authored-By and empty separator lines between commits
            if stripped.lower().startswith("co-authored-by"):
                continue
            if stripped.startswith("COMMIT: "):
                subj = stripped[8:].lower()
                if any(subj.startswith(s) for s in GIT_SKIP_SUBJECTS):
                    continue
            lines.append(line)
        return "\n".join(lines).strip()
    except Exception as e:
        logger.error(f"git log error {repo_path.name}: {type(e).__name__}: {e}")
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


# Static dispatch table: source → (parser_fn, system_prompt)
PARSERS: dict[str, tuple] = {
    "claude":      (parse_claude_transcript,      None),
    "codex":       (parse_codex_transcript,       None),
    "antigravity": (parse_antigravity_transcript,  None),
    "cursor":      (parse_cursor_transcript,      None),
    "openclaw":    (parse_openclaw_transcript,    None),
    "markdown":    (parse_markdown_file,          MARKDOWN_SYSTEM_PROMPT),
    "git":         (parse_git_log,                GIT_SYSTEM_PROMPT),
}


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


def extract_facts(chunk: str, model: str, system: str | None = None) -> list[dict]:
    """Send one text chunk to LLM backend, return extracted fact objects. Pushes token telemetry."""
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
    try:
        items, response = client.extract_json(
            system=prompt,
            user=f"Extract information from this {label}:\n\n{chunk}",
        )
        push_tokens(model, response.input_tokens, response.output_tokens, app="transcript-miner")
        if len(items) != len([i for i in items if isinstance(i, dict)]):
            logger.info("  salvaged objects from malformed JSON")
        return items
    except Exception as e:
        logger.error(f"LLM extraction failed (model={model}): {type(e).__name__}: {e}")
        logger.debug(traceback.format_exc())
        return []


def add_to_fleet_memory(content: str, category: str, dry_run: bool) -> bool:
    """Write one fact to fleet memory via MCP HTTP."""
    if dry_run:
        logger.info(f"  [DRY] [{category}] {content[:80]}")
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
                    timeout=180
                )
            resp.raise_for_status()
            body = resp.json()
            if body.get("result", {}).get("isError"):
                err = body["result"].get("content", [{}])[0].get("text", "unknown")
                logger.error(f"fleet memory rejected write: {err[:120]}")
                return False
            return True
        except Exception as e:
            if attempt < 2:
                sleep_secs = 8 * (attempt + 1)
                logger.info(f"  fleet memory write failed (attempt {attempt+1}/3): {type(e).__name__}: {e} — retry in {sleep_secs}s")
                time.sleep(sleep_secs)
            else:
                logger.error(f"fleet memory write gave up after 3 attempts: {type(e).__name__}: {e}")
                return False
    return False


def _extract_and_write(
    text: str, system_prompt: str | None, model: str, dry_run: bool,
    label: str
) -> int:
    """Chunk text, extract facts via LLM, write to fleet memory. Returns facts written."""
    chunks = chunk_text(text)
    facts_written = 0
    total_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        logger.info(f"  [{label}] chunk {i+1}/{total_chunks} ({len(chunk)} chars) → llm [{model}]")
        items = extract_facts(chunk, model, system=system_prompt)
        logger.info(f"  [{label}] extracted {len(items)} items")
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
    cp: dict, cp_lock: threading.Lock, args, checkpoint_file: Path
) -> int:
    try:
        repo_list = client.list_repos()
    except Exception as e:
        logger.error(f"{forge_label}: failed to list repos: {type(e).__name__}: {e}")
        return 0
    logger.info(f"Found {len(repo_list)} {forge_label} repos")

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
    logger.info(f"{forge_label}: {len(repo_list) - len(pending) - skipped_count} up-to-date, "
                f"{skipped_count} skipped, {len(pending)} pending")
    if args.limit:
        pending = pending[:args.limit]
        logger.info(f"{forge_label}: limited to {len(pending)}")

    def run_one_forge(item: tuple[str, str]) -> int:
        owner, repo = item
        key = f"{client.prefix}:{owner}/{repo}"
        logger.info(f"processing {forge_label} {owner}/{repo}")
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
                save_checkpoint(cp, checkpoint_file)
            if n == -1:
                logger.info("  already up-to-date, skip")
            elif n == 0:
                logger.info("  nothing extracted")
            else:
                logger.info(f"  wrote {n} facts")
            return max(n, 0)
        except Exception as e:
            logger.error(f"{forge_label} {owner}/{repo}: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())
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
    Process one transcript/doc/repo file.
    Returns (n_facts, checkpoint_entry) — entry is None if already done (hash matches).
    n_facts == -1 means already processed; 0 means too short or nothing extracted.
    """
    fhash = git_repo_hash(path) if source == "git" else file_hash(path)
    if known_hash == fhash:
        return -1, None

    if source not in PARSERS:
        raise ValueError(f"unknown source: {source}")
    parser_fn, system_prompt = PARSERS[source]
    text = parser_fn(path)

    if len(text) < 200:
        return 0, {"file_hash": fhash, "facts_written": 0, "skipped": True}

    facts_written = _extract_and_write(text, system_prompt, model, dry_run, label=source)

    return facts_written, {
        "file_hash": fhash,
        "facts_written": facts_written,
        "processed_at": datetime.now().isoformat(),
        "source": source
    }


def main():
    parser = argparse.ArgumentParser(description="Mine session transcripts into fleet memory")
    parser.add_argument("--dry-run", action="store_true", help="Extract but don't write to fleet memory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging (exception tracebacks, retry details)")
    parser.add_argument("--since", help="Only process files newer than YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Max files to process this run")
    parser.add_argument("--skip-subagents", action="store_true", help="Skip files inside subagents/ subdirectories")
    parser.add_argument("--no-transcripts", action="store_true", help="Skip transcript processing (run only forge/git/markdown sources)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker threads (default: 1)")
    parser.add_argument("--model", default=None, help="LLM model for extraction (inferred from env if not set: OpenAI→gpt-4o-mini, Anthropic→claude-haiku, Ollama→qwen3:8b)")
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

    if not args.model:
        args.model = _infer_default_model()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    if not args.model:
        print("ERROR: no LLM model configured. Set LLM_MODEL in .env or pass --model.", file=sys.stderr)
        print("  OpenAI:    LLM_API_KEY=sk-...  LLM_MODEL=gpt-4o-mini", file=sys.stderr)
        print("  Anthropic: ANTHROPIC_API_KEY=sk-ant-...  LLM_MODEL=claude-haiku-4-5-20251001", file=sys.stderr)
        print("  Ollama:    OLLAMA_URL=http://localhost:11434  LLM_MODEL=qwen3:8b", file=sys.stderr)
        sys.exit(1)

    checkpoint_file = Path(args.checkpoint) if args.checkpoint else Path(__file__).parent / "checkpoint.json"

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    cp = load_checkpoint(checkpoint_file)
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
        logger.info(f"Found {len(md_files)} markdown files across {len(args.markdown_roots)} roots")
    if args.git:
        if not args.git_roots:
            projects = os.getenv("PROJECTS_ROOT")
            if not projects:
                print("ERROR: --git requires --git-roots or PROJECTS_ROOT env var", file=sys.stderr)
                sys.exit(1)
            args.git_roots = [Path(projects)]
        git_repos = find_git_repos(args.git_roots)
        logger.info(f"Found {len(git_repos)} git repos across {len(args.git_roots)} roots")
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
    logger.info(
        f"Found {len(all_files)} files ({', '.join(parts)}): {already_done} done, {len(pending)} pending"
        + (" [skipped subagents]" if args.skip_subagents else "")
    )
    if args.limit:
        pending = pending[:args.limit]
        logger.info(f"Limited to {len(pending)} files")
    logger.info(f"Model: {args.model}  Workers: {args.workers}")

    total_facts = 0

    def run_one(item: tuple[Path, str]) -> int:
        path, source = item
        key = str(path)
        rel = str(path).replace(str(Path.home()), "~")
        with cp_lock:
            known_hash = cp["processed"].get(key, {}).get("file_hash")
        logger.info(f"processing {source} {rel}")
        try:
            n, entry = process_file(path, source, known_hash, args.dry_run, args.model)
            with cp_lock:
                if entry is not None:
                    cp["processed"][key] = entry
                cp["stats"]["files"] += 1
                if n > 0:
                    cp["stats"]["facts"] += n
                save_checkpoint(cp, checkpoint_file)
            if n == -1:
                logger.info("  already processed, skip")
            elif n == 0:
                logger.info("  too short, skip")
            else:
                logger.info(f"  wrote {n} facts")
            return max(n, 0)
        except Exception as e:
            logger.error(f"{source} {rel}: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())
            with cp_lock:
                cp["stats"]["errors"] += 1
                save_checkpoint(cp, checkpoint_file)
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
            logger.info("GitHub: GITHUB_TOKEN not set — skipping")
        else:
            try:
                r = httpx.get(
                    f"{args.github_url}/user",
                    headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
                    timeout=10
                )
                r.raise_for_status()
                username = r.json().get("login", "?")
                logger.info(f"GitHub: authenticated as {username}")
                gh_client = GitHubForgeClient(token=gh_token, base_url=args.github_url, orgs=args.github_orgs)
                total_facts += run_forge_pipeline(gh_client, "github", skip_set, cp, cp_lock, args, checkpoint_file)
            except Exception as e:
                logger.error(f"GitHub: token validation failed — {type(e).__name__}: {e} — skipping")

    if args.gitlab:
        gl_token = os.getenv("GITLAB_TOKEN", "")
        if not gl_token:
            logger.info("GitLab: GITLAB_TOKEN not set — skipping")
        else:
            api_base = args.gitlab_url.rstrip("/")
            try:
                r = httpx.get(
                    f"{api_base}/api/v4/user",
                    headers={"PRIVATE-TOKEN": gl_token},
                    timeout=10
                )
                r.raise_for_status()
                username = r.json().get("username", "?")
                logger.info(f"GitLab: authenticated as {username}")
                gl_client = GitLabForgeClient(token=gl_token, base_url=args.gitlab_url, groups=args.gitlab_groups)
                total_facts += run_forge_pipeline(gl_client, "gitlab", skip_set, cp, cp_lock, args, checkpoint_file)
            except Exception as e:
                logger.error(f"GitLab: token validation failed — {type(e).__name__}: {e} — skipping")

    if args.gitea:
        gitea_token = os.getenv("GITEA_TOKEN", "")
        if gitea_token:
            gitea_client = GiteaForgeClient(base_url=GITEA_URL, orgs=args.gitea_orgs, token=gitea_token)
        else:
            cred_user, cred_pw = get_gitea_credentials(GITEA_URL)
            if not cred_user:
                logger.info("Gitea: no GITEA_TOKEN and no git credential store credentials — skipping")
                gitea_client = None
            else:
                gitea_client = GiteaForgeClient(base_url=GITEA_URL, orgs=args.gitea_orgs, user=cred_user, password=cred_pw)
        if gitea_client:
            total_facts += run_forge_pipeline(gitea_client, "gitea", skip_set, cp, cp_lock, args, checkpoint_file)

    logger.info(f"Done. {total_facts} new facts written. Total in checkpoint: {cp['stats']['facts']}")


if __name__ == "__main__":
    main()
