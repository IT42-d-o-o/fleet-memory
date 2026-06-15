"""
memory-mcp — shared fleet-memory MCP server.

Thin MCP wrapper around mem0. Exposes add_memory / search_memory as MCP tools
over streamable-HTTP. mem0 handles fact extraction, dedup, embeddings, Qdrant.

LLM backend is env-var-driven:
  LLM_PROVIDER=openai  (default) — requires OPENAI_API_KEY
  LLM_PROVIDER=ollama             — requires OLLAMA_URL (default http://127.0.0.1:11434)

The OpenAI key is injected at runtime by run.sh from Vault or set directly
in the environment.
"""
import os
import sys
import json
import logging
from typing import Any

import uvicorn
from mem0 import Memory
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from fts_index import FtsIndex, rrf_merge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("memory-mcp")

# mem0 v3 hybrid search optionally uses spaCy (mem0ai[nlp]) for BM25/entity ranking.
# We run semantic-only, so silence its "spaCy not installed" warning emitted on every call.
logging.getLogger("mem0.utils.spacy_models").setLevel(logging.ERROR)

# --- configuration -------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").lower()
# Keyless mode: fastembed embeddings, no LLM. add_memory always stores verbatim
# (infer is forced False) so the LLM is never called and no API key is required.
KEYLESS = LLM_PROVIDER == "none"

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("MEM0_COLLECTION", "local_ai_cross_agent_memory")
HISTORY_DB = os.environ.get("MEM0_HISTORY_DB", "/opt/memory-mcp/history.db")
FTS_DB = os.environ.get("MEM0_FTS_DB", "/opt/memory-mcp/fts.db")
FLEET_NS = os.environ.get("MEM0_NAMESPACE", "fleet")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8800"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
TELEMETRY_FILE = os.environ.get("LLM_TEXTFILE_METRIC", "")

_EMBED_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}


def _qdrant_cfg(dims: int) -> dict:
    return {
        "collection_name": COLLECTION,
        "host": QDRANT_HOST,
        "port": QDRANT_PORT,
        "embedding_model_dims": dims,
    }


def _openai_embedder(api_key: str, model: str = "text-embedding-3-small") -> tuple[dict, int]:
    dims = _EMBED_DIMS.get(model, 1536)
    return {"provider": "openai", "config": {"model": model, "api_key": api_key}}, dims


def _ollama_embedder(url: str, model: str = "nomic-embed-text") -> tuple[dict, int]:
    dims = _EMBED_DIMS.get(model, 768)
    return {"provider": "ollama", "config": {"model": model, "ollama_base_url": url}}, dims


def _fastembed_embedder(model: str = "BAAI/bge-small-en-v1.5") -> tuple[dict, int]:
    dims = _EMBED_DIMS.get(model, 384)
    return {"provider": "fastembed", "config": {"model": model}}, dims


if KEYLESS:
    # No LLM, no API key. fastembed runs embeddings locally; add_memory stores
    # content verbatim (infer forced False). The llm block carries a placeholder
    # key only so the client constructs — it is never called.
    embedder, embed_dims = _fastembed_embedder()
    mem0_config = {
        "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini", "api_key": "keyless-unused"}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg(embed_dims)},
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=none (KEYLESS) embedder=fastembed dims=%d - LLM disabled, infer forced False", embed_dims)

elif LLM_PROVIDER == "litellm":
    # Universal: any provider key + model string (anthropic/..., openai/..., openrouter/..., etc.)
    # Embeddings run locally via fastembed — no second API key needed.
    LLM_API_KEY = os.environ.get("LLM_API_KEY")
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "openai/gpt-4o-mini")
    if not LLM_API_KEY:
        log.error("LLM_API_KEY not set and LLM_PROVIDER=litellm - refusing to start")
        sys.exit(1)
    # mem0's litellm provider doesn't forward api_key through litellm internals — set env var directly
    _model_prefix = LLM_MODEL.split("/")[0].lower() if "/" in LLM_MODEL else "openai"
    _env_map = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                "openrouter": "OPENROUTER_API_KEY", "together": "TOGETHERAI_API_KEY",
                "groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY"}
    _env_var = _env_map.get(_model_prefix, "OPENAI_API_KEY")
    os.environ.setdefault(_env_var, LLM_API_KEY)
    log.info("litellm auth: set %s from LLM_API_KEY", _env_var)
    embedder, embed_dims = _fastembed_embedder()
    mem0_config = {
        "llm": {"provider": "litellm", "config": {"model": LLM_MODEL, "api_key": LLM_API_KEY, "temperature": 0.1}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg(embed_dims)},
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=litellm llm=%s embedder=fastembed(local) dims=%d", LLM_MODEL, embed_dims)

elif LLM_PROVIDER == "ollama":
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "qwen3:8b")
    EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "nomic-embed-text")
    embedder, embed_dims = _ollama_embedder(OLLAMA_URL, EMBED_MODEL)
    mem0_config = {
        "llm": {"provider": "ollama", "config": {"model": LLM_MODEL, "ollama_base_url": OLLAMA_URL}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg(embed_dims)},
        "history_db_path": HISTORY_DB,
    }
    log.info("backend=ollama url=%s llm=%s embedder=%s dims=%d", OLLAMA_URL, LLM_MODEL, EMBED_MODEL, embed_dims)

elif LLM_PROVIDER == "anthropic":
    # Anthropic has no embeddings API — fall back to OpenAI embeddings (if key set) or Ollama.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set and LLM_PROVIDER=anthropic - refusing to start")
        sys.exit(1)
    LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "claude-3-5-haiku-20241022")
    if os.environ.get("OPENAI_API_KEY"):
        embedder, embed_dims = _openai_embedder(os.environ["OPENAI_API_KEY"])
        log.info("backend=anthropic llm=%s embedder=openai dims=%d", LLM_MODEL, embed_dims)
    elif os.environ.get("OLLAMA_URL"):
        embedder, embed_dims = _ollama_embedder(os.environ["OLLAMA_URL"])
        log.info("backend=anthropic llm=%s embedder=ollama dims=%d", LLM_MODEL, embed_dims)
    else:
        log.error("LLM_PROVIDER=anthropic requires OPENAI_API_KEY or OLLAMA_URL for embeddings")
        sys.exit(1)
    mem0_config = {
        "llm": {"provider": "anthropic", "config": {"model": LLM_MODEL, "api_key": ANTHROPIC_API_KEY, "temperature": 0.1}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg(embed_dims)},
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
        embedder, embed_dims = _ollama_embedder(os.environ["OLLAMA_URL"])
        log.info("backend=openai-compat base=%s llm=%s embedder=ollama dims=%d", OPENAI_API_BASE, LLM_MODEL, embed_dims)
    else:
        embedder, embed_dims = _openai_embedder(OPENAI_API_KEY, EMBED_MODEL)
        log.info("backend=openai%s llm=%s embedder=%s dims=%d",
                 f"-compat({OPENAI_API_BASE})" if OPENAI_API_BASE else "", LLM_MODEL, EMBED_MODEL, embed_dims)
    mem0_config = {
        "llm": {"provider": "openai", "config": llm_cfg},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": _qdrant_cfg(embed_dims)},
        "history_db_path": HISTORY_DB,
    }

log.info("init mem0 - qdrant=%s:%s collection=%s", QDRANT_HOST, QDRANT_PORT, COLLECTION)
memory = Memory.from_config(mem0_config)

# FTS5 keyword side index for hybrid (semantic + BM25) retrieval. Derived mirror
# of Qdrant; populate/reconcile with rebuild_fts.py. Best-effort — failures here
# never break the primary mem0 path.
fts = FtsIndex(FTS_DB)
log.info("fts side index ready at %s (%d rows)", FTS_DB, fts.count())

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
def add_memory(content: str, agent: str, project: str | None = None, metadata: dict[str, Any] | None = None, infer: bool = False) -> str:
    """Store a memory in the shared fleet memory.

    content:  the fact / decision / lesson to remember.
    agent:    name of the writing agent (e.g. 'claude', 'miner', 'overseer-bot').
    project:  optional project slug (e.g. 'atila', 'lexradar'). Stored under fleet:{project}.
    metadata: optional extra tags — merged with source provenance.
    infer:    False (default) stores content verbatim as one atomic fact — use when the
              caller already extracted a single fact. True re-runs mem0 LLM extraction to
              split/dedup — use only for raw multi-fact conversation snippets.
    """
    meta = dict(metadata or {})
    meta["source"] = agent
    namespace = f"{FLEET_NS}:{project}" if project else FLEET_NS
    eff_infer = infer and not KEYLESS
    result = memory.add(content, user_id=namespace, metadata=meta, infer=eff_infer)
    # Mirror each stored memory into the FTS5 side index (best-effort).
    for r in (result.get("results") or []):
        if r.get("id"):
            fts.mirror(r["id"], namespace, r.get("memory") or content, meta)
    _emit_metric(len(content))
    log.info("add_memory by %s ns=%s infer=%s -> %s", agent, namespace, eff_infer, result)
    return json.dumps(result, default=str)


@mcp.tool()
def search_memory(query: str, limit: int = 5, project: str | None = None) -> str:
    """Search the shared fleet memory.

    query:   natural-language query.
    limit:   max results (default 5).
    project: if set, searches fleet:{project} and global fleet merged by score.
    """
    if not query or not query.strip():
        return json.dumps({"results": []})

    namespaces = [f"{FLEET_NS}:{project}", FLEET_NS] if project else [FLEET_NS]

    # 1. semantic retrieval (Qdrant via mem0), deduped across namespaces by score
    seen: set = set()
    semantic: list = []
    for ns in namespaces:
        r = memory.search(query, filters={"user_id": ns}, limit=limit)
        for item in r.get("results", []):
            if item["id"] not in seen:
                seen.add(item["id"])
                semantic.append(item)
    semantic.sort(key=lambda x: x.get("score", 0), reverse=True)

    # 2. keyword retrieval (FTS5 BM25) over the same namespaces
    keyword = fts.search(query, namespaces, limit)

    # 3. fuse with Reciprocal Rank Fusion (scale-free; no score normalization)
    merged = rrf_merge(semantic, keyword, limit)

    _emit_metric(len(query))
    return json.dumps({"results": merged}, default=str)


if __name__ == "__main__":
    app = mcp.streamable_http_app()

    if MCP_AUTH_TOKEN:
        class _BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] != MCP_AUTH_TOKEN:
                    return StarletteResponse("Unauthorized", status_code=401)
                return await call_next(request)
        app.add_middleware(_BearerAuth)
        log.info("bearer auth enabled")
    else:
        log.warning("MCP_AUTH_TOKEN not set — server accepts unauthenticated requests")

    log.info("memory-mcp listening on %s:%s (streamable-http, path /mcp)", MCP_HOST, MCP_PORT)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
