# fleet-memory

Shared AI agent memory — mem0 + Qdrant + MCP server + backfill miner.

All your AI agents (Claude, Codex, Qwen, Gemini) share a single memory pool. Agents read past decisions, lessons, and context. They write back what they learn. Memory persists across sessions and tools.

## How it works

```
Claude Code ─── MCP ──▶ memory-mcp server (port 8800)
Codex ──────── MCP ──▶       │
OpenCode ───── MCP ──▶       ▼
                         mem0 (dedup + search)
                              │
                              ▼
                         Qdrant (vector store)
```

The miner backfills from existing sources (transcripts, git history, Gitea issues) so your memory pool starts populated.

## Structure

```
server/         mem0 + Qdrant MCP server + provisioning scripts
miner/          Backfill miner (transcripts, git, Gitea)
onboarding/     Setup script, MCP config snippet, CLAUDE.md snippet
docs/           LLM backend guide, backfill docs
```

## Quick start

### 1. Deploy server

```bash
# OpenAI (best quality):
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
bash server/provision.sh

# OR Ollama (local, free):
export LLM_PROVIDER=ollama
export OLLAMA_URL=http://127.0.0.1:11434
bash server/provision.sh
```

Endpoint: `http://<host>:8800/mcp`

### 2. Wire your AI client

Claude Code — add to MCP settings:
```json
{
  "mcpServers": {
    "memory-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8800/mcp"
    }
  }
}
```

Add `onboarding/claude-md-snippet.md` content to your `CLAUDE.md`.

### 3. Backfill

```bash
cd miner/
pip install httpx it42ai
python miner.py --workers 4
```

See `docs/backfill.md` for all options.

## MCP tools

| Tool | Purpose |
|------|---------|
| `add_memory(content, agent, metadata)` | Store a fact/lesson/decision |
| `search_memory(query, limit)` | Semantic search across all agents |

## LLM backends

See `docs/llm-backends.md`. Short version: OpenAI gives best quality; Ollama is free but lower recall on terse content.

## Requirements

Server: Debian/Ubuntu, Python 3.10+, systemd  
Miner: Python 3.10+, `httpx`, `it42ai` library
