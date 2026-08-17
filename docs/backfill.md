# Backfill Pipeline

The miner (`miner/miner.py`) ingests existing content into fleet-memory.

## What it mines

| Source | Flag | Credential | Checkpoint key |
|--------|------|-----------|---------------|
| Claude transcripts | (default) | none | SHA-256 of file stat |
| Git repos | `--git` | none (local) | HEAD commit hash |
| GitHub issues + commits | `--github` | `GITHUB_TOKEN` | repo `updated_at` |
| GitLab issues + commits | `--gitlab` | `GITLAB_TOKEN` | repo `updated_at` |
| Gitea issues + commits | `--gitea` | `GITEA_TOKEN` or git credential store | repo `updated_at` |
| Markdown files | `--markdown` | none | file mtime |

## First run

```bash
cd miner/
pip install httpx python-dotenv  # or: pip install -r requirements.txt
cp .env.example .env             # fill in FLEET_MEMORY_URL and LLM key

# Claude transcripts only (fastest first run — default mode, no flag needed):
python miner.py --workers 4

# Add local git repos:
python miner.py \
  --git --git-roots ~/Projects \
  --workers 4

# GitHub (set GITHUB_TOKEN in .env first):
python miner.py \
  --github \
  --github-orgs myorg \
  --workers 4

# GitHub Enterprise:
python miner.py \
  --github --github-url https://github.mycompany.com/api/v3 \
  --workers 4

# GitLab (set GITLAB_TOKEN in .env first):
python miner.py \
  --gitlab \
  --gitlab-groups mygroup \
  --workers 4

# GitLab self-hosted:
python miner.py \
  --gitlab --gitlab-url https://gitlab.mycompany.com \
  --workers 4

# Gitea (set GITEA_URL + GITEA_TOKEN in .env, or git credential store):
python miner.py \
  --gitea \
  --gitea-orgs ai repos \
  --workers 4

# Everything at once:
python miner.py \
  --github --github-orgs myorg \
  --gitlab \
  --git --git-roots ~/Projects \
  --workers 4
```

## Incremental runs

Re-run any time. Checkpoint file (`checkpoint.json`) skips unchanged sources.
Safe to run on a cron:

```cron
0 */6 * * * cd /opt/fleet-memory/miner && python miner.py --transcripts ~/.claude/projects --workers 2
```

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `FLEET_MEMORY_URL` | `http://127.0.0.1:8800/mcp` | Fleet memory endpoint |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint (if using Ollama) |
| `LLM_API_KEY` | — | OpenAI-compatible API key |
| `LLM_BASE_URL` | — | Override API base (for OpenRouter, DeepSeek, etc.) |
| `LLM_MODEL` | `qwen3:8b` | Model for fact extraction |
| `ANTHROPIC_API_KEY` | — | Anthropic key (takes priority over LLM_API_KEY) |
| `OPENAI_API_KEY` | — | OpenAI key (fallback if LLM_API_KEY not set) |
| `GITHUB_TOKEN` | — | GitHub personal access token (for `--github`) |
| `GITHUB_URL` | `https://api.github.com` | GitHub API base (override for Enterprise) |
| `GITLAB_TOKEN` | — | GitLab personal access token (for `--gitlab`) |
| `GITLAB_URL` | `https://gitlab.com` | GitLab base URL (override for self-hosted) |
| `GITEA_TOKEN` | — | Gitea token (for `--gitea`; falls back to git credential store) |
| `GITEA_URL` | `http://127.0.0.1:3000` | Gitea instance URL |

## Quality expectations

- Transcripts: ~80% recall with 8b, ~95% with 30b+
- Git commits: ~70% with 8b (terse messages are hard), ~90% with 30b
- Gitea issues: ~60% with 8b, ~85% with 30b

Deduplication handled by mem0 (semantic, not exact). Re-running with better model improves results.
