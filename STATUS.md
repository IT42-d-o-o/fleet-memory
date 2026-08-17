# fleet-memory — STATUS

## What
Self-hosted shared AI agent memory layer (mem0 + Qdrant + MCP server + backfill miner) so multiple AI tools read/write a single persistent memory pool.

## Where
- Repo: internal Gitea (`ai/fleet-memory`, canonical) / https://github.com/IT42-d-o-o/fleet-memory.git (public mirror)
- Prod: N/A

## State
- Version: -
- Last deploy: 2026-07-21 — CT356 `/opt/memory-mcp` (shared subject canonicalizer, commit 9624ab5)
- Backlog: [docs/BACKLOG.md](docs/BACKLOG.md) — next up: dedup as a reconcile stage (#4), coarse-subject cleanup (#6), scored recall benchmark
- Known issues: GitHub mirror behind (expired PAT, gitea is canonical)
