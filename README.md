[![CI](https://github.com/IT42-d-o-o/fleet-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/IT42-d-o-o/fleet-memory/actions/workflows/ci.yml)

# fleet-memory

Shared AI agent memory — mem0 + Qdrant + MCP server + backfill miner.

All your AI agents (Claude, Codex, Qwen, Gemini) share a single memory pool. Agents read past decisions, lessons, and context. They write back what they learn. Memory persists across sessions and tools.

## Why

Every AI session starts blank. Claude asks the same questions. Agents repeat the same mistakes. Work done in one tool is invisible to every other tool.

fleet-memory is a self-hosted memory layer: agents write facts, lessons, and decisions once; every other agent reads them. Built for teams running 2+ AI agents that need to share state without a human relaying context between sessions.

**Before:** five agents, five isolated views of the same project.  
**After:** one shared memory pool — Claude writes a decision at 9am, Codex reads it at 3pm.

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
# Runs keyless by default (LLM_PROVIDER=none) — no API key, fully local.
# To enable LLM fact-extraction + dedup, set a provider in .env (see Write modes).
docker compose up -d
```

Endpoint: `http://localhost:8800/mcp`

> **Proxmox LXC users:** Docker's AppArmor profile cannot be applied from inside an LXC namespace. Both `docker compose build` and `docker compose up` fail with `unable to apply apparmor profile`. Fix:
> ```bash
> cp docker-compose.override.yml.example docker-compose.override.yml
> docker compose up -d
> ```
> Not needed on bare metal, standard VMs, or Docker Desktop.

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

### 3. Wire agent behavior

**This step is required.** Wiring the MCP server gives agents the tools — but without explicit
instructions, agents have no directive to search or write memory and will ignore it.

Paste the contents of `onboarding/agent-instructions.md` into your agent's instruction file:

| Agent | File |
|-------|------|
| Claude Code | `CLAUDE.md` at project root or `~/.claude/CLAUDE.md` (global) |
| Codex | `AGENTS.md` at project root |
| Cursor | `.cursor/rules` in project root |
| Other tools | wherever the tool accepts a persistent system prompt |

### 4. Backfill

```bash
cd miner/
cp .env.example .env   # edit: set FLEET_MEMORY_URL and your LLM provider
pip install httpx python-dotenv

python miner.py --workers 4                          # Claude transcripts (default)
python miner.py --github --github-orgs myorg         # GitHub issues + commits
python miner.py --gitlab --gitlab-groups mygroup     # GitLab issues + commits
python miner.py --gitea --gitea-orgs myorg           # Gitea issues + commits
python miner.py --git --git-roots ~/Projects         # local git repos
```

GitHub Enterprise and GitLab self-hosted are supported via `--github-url` / `--gitlab-url`. See `docs/backfill.md` for all options.

## MCP tools

| Tool | Purpose |
|------|---------|
| `add_memory(content, agent, project, metadata, infer)` | Store a fact/lesson/decision (see [Write modes](#write-modes)) |
| `search_memory(query, limit, project)` | Semantic search across all agents |

## Write modes

`add_memory` takes an `infer` flag that controls how mem0 processes the content:

| `infer` | Behavior | LLM calls per write | Use when |
|---------|----------|---------------------|----------|
| `false` *(default)* | Stores `content` verbatim as one memory | **0** | Caller already extracted a single atomic fact — most agent writes, and the miner |
| `true` | mem0 runs LLM fact-extraction + ADD/UPDATE/DELETE dedup against existing memory | **2** | Passing a raw multi-fact conversation snippet you want mem0 to split and dedup |

**The default is `infer=false`** — agents are expected to write single, already-phrased facts. This keeps writes free (no LLM call), fast, and verbatim (no re-extraction mangling).

Trade-off: `infer=false` skips mem0's dedup-on-write, so near-duplicate facts can accumulate if agents restate the same thing — run a periodic dedup pass if that matters.

### Keyless mode

Set **`LLM_PROVIDER=none`** (the default in `.env.example`) for fully keyless, fully local operation: `fastembed` runs embeddings on-device, `infer` is forced off, and **no API key is required to boot or run**. `add_memory` stores verbatim and `search_memory` ranks by semantic similarity — both make **zero external API calls**. To enable LLM-backed fact extraction and dedup, set a provider (`openai`, `litellm`, `anthropic`, `ollama`) and pass `infer=true` on writes.

## LLM backends

See `docs/llm-backends.md`. Short version: OpenAI gives best quality; Ollama is free but lower recall on terse content.

## Cost and privacy

**LLM calls:** with the default `infer=false`, `add_memory` makes **no** LLM calls — it embeds the content and stores it verbatim. With `infer=true` it makes **2 LLM API calls** per write (extract the fact, then classify it ADD/UPDATE/DELETE against existing memory). The **miner** always runs its own extraction LLM regardless of `infer`. Running the miner on large transcript archives (thousands of files) will generate meaningful API spend. Estimate before running at scale; use `--dry-run` first.

**Data leaves the machine:** the miner sends raw transcript text to your configured LLM to extract facts. If you use OpenAI or any cloud LLM, that content transits their API. To keep all data local, use Ollama (`LLM_PROVIDER=ollama`) — no data leaves your host.

**Qdrant telemetry:** Qdrant collects anonymous usage telemetry by default. To disable, add to `docker-compose.yml` under the `qdrant` service:
```yaml
environment:
  - QDRANT__TELEMETRY_DISABLED=true
```

**mem0 telemetry:** mem0 sends anonymous usage data to PostHog. To disable, set `MEM0_TELEMETRY=false` in your `.env`.

## Miner sources

**AI transcripts** — all major coding tools:

| Tool | Path |
|------|------|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` |
| Antigravity (Google) | `~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl` |
| Cursor | `~/.cursor/projects/<workspace>/agent-transcripts/**/*.jsonl` |
| OpenClaw | `~/.openclaw/agents/<agentId>/sessions/*.jsonl` |

**Code forges** — issues, PRs, and commit messages:

| Forge | Flag | Auth |
|-------|------|------|
| GitHub (cloud + Enterprise) | `--github` | `GITHUB_TOKEN` |
| GitLab (cloud + self-hosted) | `--gitlab` | `GITLAB_TOKEN` |
| Gitea (self-hosted) | `--gitea` | `GITEA_TOKEN` or git credential store |

## Built on

- [mem0](https://github.com/mem0ai/mem0) — memory layer with semantic deduplication (MIT)
- [Qdrant](https://github.com/qdrant/qdrant) — vector store (Apache 2.0)
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework (MIT)

See `NOTICE` for full attribution.

## What this is NOT

- **Not a RAG system** — no document chunking or PDF ingestion. It stores extracted facts, not raw documents.
- **Not a long-term knowledge base** — with `infer=true`, mem0 deduplicates and overwrites stale facts; it's a live memory, not an archive. The default `infer=false` stores verbatim and does *not* dedup on write — run a periodic dedup pass if you write many similar facts.
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
