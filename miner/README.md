# fleet-memory miner

Backfill miner for fleet memory. Mines AI session transcripts, git logs, markdown docs,
and forge issues (GitHub, GitLab, Gitea) into the shared mem0 memory pool.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # edit: set FLEET_MEMORY_URL and your LLM provider
```

## Usage

```bash
# Dry run — extract but don't write to memory
python miner.py --dry-run

# Transcripts only (default sources: Claude Code, Codex, Antigravity, Cursor, OpenClaw)
python miner.py --workers 4

# Mine git commit logs (set PROJECTS_ROOT or pass --git-roots)
python miner.py --git --git-roots ~/projects

# Mine markdown docs (CLAUDE.md, AGENTS.md, docs/*.md)
python miner.py --markdown --markdown-roots ~/projects

# Mine GitHub issues and commits
python miner.py --github --github-orgs myorg

# Mine GitLab
python miner.py --gitlab --gitlab-groups mygroup

# Mine Gitea
python miner.py --gitea --gitea-orgs ai repos

# All sources combined
python miner.py --git --git-roots ~/projects --markdown --markdown-roots ~/projects \
  --github --gitlab --gitea --workers 4

# Only files since a date
python miner.py --since 2026-04-01

# Resume after crash — just re-run, checkpoint skips done files
python miner.py
```

## Configuration

All config via environment variables (copy `.env.example` → `.env`):

| Variable | Purpose |
|---|---|
| `FLEET_MEMORY_URL` | MCP server endpoint (default: `http://127.0.0.1:8800/mcp`) |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITLAB_TOKEN` | GitLab personal access token |
| `GITEA_TOKEN` | Gitea token (or falls back to git credential store) |
| `GITEA_URL` | Gitea base URL (default: `http://127.0.0.1:3000`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (highest priority LLM provider) |
| `LLM_BASE_URL` | Any OpenAI-compatible endpoint base URL |
| `LLM_API_KEY` | API key for OpenAI-compatible endpoint |
| `OLLAMA_URL` | Ollama URL (default: `http://localhost:11434`) |
| `PUSHGATEWAY_URL` | Prometheus Pushgateway (optional, empty = disabled) |

## Checkpoint

`checkpoint.json` persists after every file. Safe to kill with Ctrl+C and resume.
Logs written to `miner.log` in the same directory.
