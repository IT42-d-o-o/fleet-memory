## Fleet Memory (memory-mcp)

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
