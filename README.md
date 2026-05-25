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

**Option A — Docker (any OS, recommended for local use):**

```bash
cp .env.example .env
# Edit .env — set OPENAI_API_KEY or switch to Ollama
docker compose up -d
```

Endpoint: `http://localhost:8800/mcp`

**Option B — Linux systemd (production, Debian/Ubuntu):**

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
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
      "url": "http://127.0.0.1:8800/mcp",
      "headers": {
        "Authorization": "Bearer <your-MCP_AUTH_TOKEN>"
      }
    }
  }
}
```
Omit `headers` if `MCP_AUTH_TOKEN` is not set.

Add `onboarding/claude-md-snippet.md` content to your `CLAUDE.md`.

### 3. Backfill

```bash
cd miner/
cp .env.example .env   # edit: set FLEET_MEMORY_URL and your LLM provider
pip install httpx python-dotenv
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

## Transcript sources

The miner backfills from all major AI coding tools:

| Tool | Path |
|------|------|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` |
| Antigravity (Google) | `~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl` |
| Cursor | `~/.cursor/projects/<workspace>/agent-transcripts/**/*.jsonl` |
| OpenClaw | `~/.openclaw/agents/<agentId>/sessions/*.jsonl` |

## Built on

- [mem0](https://github.com/mem0ai/mem0) — memory layer with semantic deduplication (MIT)
- [Qdrant](https://github.com/qdrant/qdrant) — vector store (Apache 2.0)
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework (MIT)

See `NOTICE` for full attribution.

## What this is NOT

- **Not a RAG system** — no document chunking or PDF ingestion. It stores extracted facts, not raw documents.
- **Not a long-term knowledge base** — mem0 deduplicates and overwrites stale facts; it's a live memory, not an archive.
- **Not multi-tenant** — single namespace (`fleet`) shared by all agents. Namespace isolation (`MEM0_NAMESPACE`) is available but there is no auth-per-namespace.
- **Not a replacement for structured storage** — don't store logs, metrics, or relational data here. Store observations and decisions.
- **Not production-hardened for public internet** — set `MCP_AUTH_TOKEN` and put it behind a reverse proxy with TLS before exposing externally.

## Backup and restore

**Docker:**
```bash
# Backup Qdrant data volume
docker run --rm -v fleet-memory_qdrant_data:/data -v $(pwd):/out alpine \
  tar czf /out/qdrant-backup-$(date +%Y%m%d).tar.gz /data

# Restore
docker run --rm -v fleet-memory_qdrant_data:/data -v $(pwd):/out alpine \
  tar xzf /out/qdrant-backup-YYYYMMDD.tar.gz -C /
```

**Systemd:**
```bash
systemctl stop memory-mcp
tar czf qdrant-backup.tar.gz /opt/qdrant/storage
cp /opt/memory-mcp/history.db history.db.bak
systemctl start memory-mcp
```

## Upgrade

**Docker:**
```bash
git pull
docker compose up -d --build
```

**Systemd:**
```bash
git -C /opt/fleet-memory pull
pip install -r /opt/fleet-memory/server/requirements.txt --upgrade
systemctl restart memory-mcp
```

**Embedding model change:** if you switch from one embedder to another (e.g. OpenAI → fastembed), the Qdrant collection must be rebuilt because vector dimensions differ. Delete and recreate: `docker compose down -v && docker compose up -d`.

## Requirements

Server: Debian/Ubuntu, Python 3.10+, systemd  
Miner: Python 3.10+, `httpx`, `python-dotenv`
