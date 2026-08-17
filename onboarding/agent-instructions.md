## Fleet Memory — Agent Instructions

Without this section in your agent's instruction file, the agent has the MCP tools wired but no
directive to use them — it will ignore fleet memory entirely.

**Paste the section below into your agent's instruction file:**

| Agent | File |
|-------|------|
| Claude Code | `CLAUDE.md` at project root (or `~/.claude/CLAUDE.md` for all projects) |
| Codex | `AGENTS.md` at project root |
| Cursor | `.cursor/rules` in project root |
| OpenCode / Qwen / others | wherever the tool accepts a persistent system prompt |

Agents that don't support a persistent system prompt (Antigravity, some Gemini modes) won't
proactively search or write — the miner backfill handles populating memory from their transcripts.

---

### Fleet Memory (memory-mcp)

Shared memory store at `http://<your-server-ip>:8800/mcp` (streamable-HTTP MCP).
All agents share `user_id: "fleet"`. The `agent` field tracks provenance per write.

### Taxonomy — `metadata.category`

| Category | What belongs | Writer |
|---|---|---|
| `infra` | Host specs, IPs, hostnames, service status | read-only reference |
| `user` | User preferences, expertise, working style | any agent |
| `project` | Active tasks, decisions, milestones, blockers | all agents |
| `lesson` | Approach X failed/succeeded on task Y because Z — reusable | all agents |
| `tool` | Paths, endpoints, env vars — no plaintext secrets | any agent |

### Session Start — always search

Run these 3 queries at the start of every session before acting:
```
search_memory("user preferences working style")
search_memory("active projects blockers decisions")
search_memory("lessons learned failures approaches")
```

### Write Triggers

Write to memory-mcp when:
- **Task complete**: key decision or milestone → `project`
- **Bug solved non-obviously**: root cause + fix approach → `lesson`
- **New tool/path discovered**: endpoint, secret path, env var → `tool`
- **User corrects behavior**: preference or constraint → `user`

Always tag `metadata: {"category": "<type>"}`. Never write secrets — reference paths only.

### Example writes

```python
# After solving a non-obvious bug:
add_memory(
    content="SQLite WAL mode must be enabled before first write or concurrent reads deadlock under load",
    agent="claude",
    metadata={"category": "lesson"}
)

# After learning a user preference:
add_memory(
    content="User prefers single bundled PRs over many small ones for refactors",
    agent="claude",
    metadata={"category": "user"}
)

# After discovering a tool endpoint:
add_memory(
    content="Fleet memory MCP endpoint: http://192.168.1.10:8800/mcp — streamable-HTTP transport",
    agent="claude",
    metadata={"category": "tool"}
)
```

### What NOT to store

- Raw code, file contents, stack traces — store the insight, not the artifact
- Facts already in the codebase or docs — memory is for non-obvious, cross-session knowledge
- Secrets or credentials — store Vault paths or references only
- Noise from routine tasks — only write when something is worth knowing in a future session
