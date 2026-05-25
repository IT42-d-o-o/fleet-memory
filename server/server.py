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

_qdrant_cfg = {"collection_name": COLLECTION, "host": QDRANT_HOST, "port": QDRANT_PORT}


def _openai_embedder(api_key: str, model: str = "text-embedding-3-small") -> dict:
    return {"provider": "openai", "config": {"model": model, "api_key": api_key}}


def _ollama_embedder(url: str, model: str = "nomic-embed-text") -> dict:
    return {"provider": "ollama", "config": {"model": model, "ollama_base_url": url}}


def _fastembed_embedder(model: str = "BAAI/bge-small-en-v1.5") -> dict:
    return {"provider": "fastembed", "config": {"model": model}}


if LLM_PROVIDER == "litellm":
    # Universal: any provider key + model string (anthropic/..., openai/..., openrouter/..., etc.)
    # Embeddings run locally via fastembed — no second API key needed.
    LLM_API_KEY = os.environ.get("LLM_API_KEY")
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "openai/gpt-4o-mini")
    if not LLM_API_KEY:
        log.error("LLM_API_KEY not set and LLM_PROVIDER=litellm - refusing to start")
        sys.exit(1)
    mem0_config = {
        "llm": {"provider": "litellm", "config": {"model": LLM_MODEL, "api_key": LLM_API_KEY, "temperature": 0.1}},
        "embedder": _fastembed_embedder(),
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg},
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=litellm llm=%s embedder=fastembed(local)", LLM_MODEL)

elif LLM_PROVIDER == "ollama":
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "qwen3:8b")
    EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "nomic-embed-text")
    mem0_config = {
        "llm": {"provider": "ollama", "config": {"model": LLM_MODEL, "ollama_base_url": OLLAMA_URL}},
        "embedder": _ollama_embedder(OLLAMA_URL, EMBED_MODEL),
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg},
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=ollama url=%s llm=%s embedder=%s", OLLAMA_URL, LLM_MODEL, EMBED_MODEL)

elif LLM_PROVIDER == "anthropic":
    # Anthropic has no embeddings API — fall back to OpenAI embeddings (if key set) or Ollama.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set and LLM_PROVIDER=anthropic - refusing to start")
        sys.exit(1)
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "claude-3-5-haiku-20241022")
    if os.environ.get("OPENAI_API_KEY"):
        embedder = _openai_embedder(os.environ["OPENAI_API_KEY"])
        log.info("backend=anthropic llm=%s embedder=openai", LLM_MODEL)
    elif os.environ.get("OLLAMA_URL"):
        embedder = _ollama_embedder(os.environ["OLLAMA_URL"])
        log.info("backend=anthropic llm=%s embedder=ollama", LLM_MODEL)
    else:
        log.error("LLM_PROVIDER=anthropic requires OPENAI_API_KEY or OLLAMA_URL for embeddings")
        sys.exit(1)
    mem0_config = {
        "llm": {"provider": "anthropic", "config": {"model": LLM_MODEL, "api_key": ANTHROPIC_API_KEY, "temperature": 0.1}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg},
        "history_db_path": HISTORY_DB,
    }

else:
    # openai — also handles OpenRouter and any OpenAI-compatible endpoint via OPENAI_API_BASE
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY not set and LLM_PROVIDER=%s - refusing to start", LLM_PROVIDER)
        sys.exit(1)
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "gpt-4o-mini")
    EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "text-embedding-3-small")
    OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "")
    llm_cfg: dict = {"model": LLM_MODEL, "api_key": OPENAI_API_KEY, "temperature": 0.1}
    if OPENAI_API_BASE:
        llm_cfg["openai_base_url"] = OPENAI_API_BASE
    # OpenRouter has no embeddings endpoint — fall back to Ollama if configured, else use OpenAI
    if OPENAI_API_BASE and os.environ.get("OLLAMA_URL"):
        embedder = _ollama_embedder(os.environ["OLLAMA_URL"])
        log.info("backend=openai-compat base=%s llm=%s embedder=ollama", OPENAI_API_BASE, LLM_MODEL)
    else:
        embedder = _openai_embedder(OPENAI_API_KEY, EMBED_MODEL)
        log.info("backend=openai%s llm=%s embedder=%s",
                 f"-compat({OPENAI_API_BASE})" if OPENAI_API_BASE else "", LLM_MODEL, EMBED_MODEL)
    mem0_config = {
        "llm": {"provider": "openai", "config": llm_cfg},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg},
        "history_db_path": HISTORY_DB,
    }

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
