# Backfill Pipeline

The miner (`miner/miner.py`) ingests existing content into fleet-memory.

## What it mines

| Source | How | Checkpoint key |
|--------|-----|---------------|
| Claude transcripts | JSONL files in `~/.claude/projects/*/` | SHA-256 of file stat |
| Git repos | `git log` + file content | HEAD commit hash |
| Gitea issues | REST API, all orgs/repos | repo `updated_at` |

## First run

```bash
cd miner/
pip install httpx it42ai  # or: pip install -r requirements.txt

# Claude transcripts only (fastest first run — default mode, no flag needed):
python miner.py --workers 4

# Add git repos:
python miner.py \
  --git --git-roots ~/Projects \
  --workers 4

# Add Gitea (requires GITEA_URL env + git credential store):
export GITEA_URL=http://your-gitea:3000
python miner.py \
  --gitea \
  --gitea-orgs ai repos \
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
| `LLM_API_KEY` | — | OpenAI/OpenRouter API key |
| `LLM_BASE_URL` | — | Override API base (for OpenRouter etc.) |
| `LLM_MODEL` | — | Override default model |
| `OPENAI_API_KEY` | — | OpenAI key (fallback if LLM_API_KEY not set) |
| `GITEA_URL` | — | Gitea instance URL |

## Quality expectations

- Transcripts: ~80% recall with 8b, ~95% with 30b+
- Git commits: ~70% with 8b (terse messages are hard), ~90% with 30b
- Gitea issues: ~60% with 8b, ~85% with 30b

Deduplication handled by mem0 (semantic, not exact). Re-running with better model improves results.
