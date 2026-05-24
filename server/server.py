"""
memory-mcp — shared fleet-memory MCP server.

Thin MCP wrapper around mem0. Exposes add_memory / search_memory as MCP tools
over streamable-HTTP. mem0 handles fact extraction, dedup, embeddings, Qdrant.

LLM backend is env-var-driven:
  LLM_PROVIDER=openai  (default) — requires OPENAI_API_KEY
  LLM_PROVIDER=ollama             — requires OLLAMA_URL (default http://127.0.0.1:11434)

The OpenAI key is injected at runtime by run.sh from Vault (IT42 setup) or
set directly in the environment (standalone/product installs).
"""
import os
import sys
import json
import logging
from typing import Any

from mem0 import Memory
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("memory-mcp")

# --- configuration -------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").lower()

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("MEM0_COLLECTION", "local_ai_cross_agent_memory")
HISTORY_DB = os.environ.get("MEM0_HISTORY_DB", "/opt/memory-mcp/history.db")
FLEET_NS = os.environ.get("MEM0_NAMESPACE", "fleet")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8800"))
TELEMETRY_FILE = os.environ.get(
    "LLM_TEXTFILE_METRIC",
    "/var/lib/node_exporter/textfile_collector/memory_mcp.prom",
)

if LLM_PROVIDER == "ollama":
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "qwen3:8b")
    EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "nomic-embed-text")
    mem0_config = {
        "llm": {
            "provider": "ollama",
            "config": {"model": LLM_MODEL, "ollama_base_url": OLLAMA_URL},
        },
        "embedder": {
            "provider": "ollama",
            "config": {"model": EMBED_MODEL, "ollama_base_url": OLLAMA_URL},
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {"collection_name": COLLECTION, "host": QDRANT_HOST, "port": QDRANT_PORT},
        },
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=ollama url=%s llm=%s embedder=%s", OLLAMA_URL, LLM_MODEL, EMBED_MODEL)
else:
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY not set and LLM_PROVIDER=openai - refusing to start")
        sys.exit(1)
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "gpt-4o-mini")
    EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "text-embedding-3-small")
    mem0_config = {
        "llm": {
            "provider": "openai",
            "config": {"model": LLM_MODEL, "api_key": OPENAI_API_KEY, "temperature": 0.1},
        },
        "embedder": {
            "provider": "openai",
            "config": {"model": EMBED_MODEL, "api_key": OPENAI_API_KEY},
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {"collection_name": COLLECTION, "host": QDRANT_HOST, "port": QDRANT_PORT},
        },
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=openai llm=%s embedder=%s", LLM_MODEL, EMBED_MODEL)

log.info("init mem0 - qdrant=%s:%s collection=%s", QDRANT_HOST, QDRANT_PORT, COLLECTION)
memory = Memory.from_config(mem0_config)

mcp = FastMCP(
    "memory-mcp",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    json_response=True,
)

# --- telemetry (best-effort) ---------------------------------------------
_tokens_total = 0


def _emit_metric(text_len: int) -> None:
    global _tokens_total
    try:
        _tokens_total += max(1, text_len // 4)
        tmp = TELEMETRY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("# HELP llm_tokens_total Estimated LLM tokens used.\n")
            fh.write("# TYPE llm_tokens_total counter\n")
            fh.write('llm_tokens_total{app="memory-mcp"} %d\n' % _tokens_total)
        os.replace(tmp, TELEMETRY_FILE)
    except Exception as exc:
        log.debug("telemetry skipped: %s", exc)


# --- MCP tools -----------------------------------------------------------
@mcp.tool()
def add_memory(content: str, agent: str, metadata: dict[str, Any] | None = None) -> str:
    """Store a memory in the shared fleet memory.

    content:  the fact / decision / lesson to remember.
    agent:    name of the writing agent (e.g. 'claude', 'miner', 'overseer-bot').
    metadata: optional extra tags — merged with source provenance.
    """
    meta = dict(metadata or {})
    meta["source"] = agent
    result = memory.add(content, user_id=FLEET_NS, metadata=meta, infer=True)
    _emit_metric(len(content))
    log.info("add_memory by %s -> %s", agent, result)
    return json.dumps(result, default=str)


@mcp.tool()
def search_memory(query: str, limit: int = 5) -> str:
    """Search the shared fleet memory.

    query: natural-language query.
    limit: max results (default 5).
    """
    result = memory.search(query, filters={"user_id": FLEET_NS}, limit=limit)
    _emit_metric(len(query))
    return json.dumps(result, default=str)


if __name__ == "__main__":
    log.info("memory-mcp listening on %s:%s (streamable-http, path /mcp)", MCP_HOST, MCP_PORT)
    mcp.run(transport="streamable-http")
