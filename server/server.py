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
import time
import logging
import datetime
from typing import Any

import uvicorn
from mem0 import Memory
from qdrant_client import QdrantClient
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from fts_index import FtsIndex, rrf_merge
from validate import detect, build_self_check, detect_secrets, build_secret_block
import authority
import gate
import subject_alias

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

# Direct Qdrant handle for supersession resolution at read time. mem0's search
# returns scored hits but offers no get-by-id; we use this to (a) fetch metadata
# for keyword-only hits the FTS index returns without it, and (b) swap a stale
# hit for its current head. Lineage fields (current/superseded_by) are written
# into payload["metadata"] by supersede.py.
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)


def _fetch_record(point_id: str) -> dict | None:
    """Fetch one point by id and shape it like a search hit. None if absent."""
    try:
        recs = qdrant.retrieve(COLLECTION, ids=[point_id], with_payload=True, with_vectors=False)
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        log.warning("qdrant retrieve failed id=%s: %s", point_id, exc)
        return None
    if not recs:
        return None
    p = recs[0].payload or {}
    # Raw Qdrant payload is flat: mem0 stores metadata keys (category, source,
    # subject, current, superseded_by, ...) top-level and only nests them into a
    # "metadata" dict in search results. Reassemble that shape here so callers can
    # read hit["metadata"]["superseded_by"] uniformly.
    _reserved = {"data", "memory", "text", "text_lemmatized", "hash",
                 "user_id", "created_at", "updated_at"}
    meta = {k: v for k, v in p.items() if k not in _reserved}
    return {
        "id": str(recs[0].id),
        "memory": p.get("data") or p.get("memory") or p.get("text") or "",
        "metadata": meta,
    }


def _resolve_supersession(hits: list[dict], include_superseded: bool) -> list[dict]:
    """Replace each stale hit with its current head, deduping heads.

    A hit is stale when its metadata carries current=False / a superseded_by id.
    Facts not yet processed by supersede.py have no lineage fields and default to
    current (never swapped). When include_superseded is True this is a no-op.
    """
    if include_superseded:
        return hits
    out: list[dict] = []
    seen: set = set()
    for hit in hits:
        meta = hit.get("metadata") or {}
        # Keyword-only hits arrive without metadata — pull it so we can judge them.
        if "current" not in meta and "superseded_by" not in meta:
            fetched = _fetch_record(hit.get("id"))
            if fetched:
                meta = fetched.get("metadata") or {}
        superseded_by = meta.get("superseded_by")
        is_current = meta.get("current", True)
        if is_current or not superseded_by:
            if hit["id"] not in seen:
                seen.add(hit["id"])
                out.append(hit)
            continue
        # Stale: walk to the current head (one hop normally; loop-guard for safety).
        head_id, guard = superseded_by, 0
        head = None
        while head_id and guard < 10:
            cand = _fetch_record(head_id)
            if not cand:
                break
            head = cand
            nxt = (cand.get("metadata") or {}).get("superseded_by")
            if not nxt or nxt == head_id:
                break
            head_id, guard = nxt, guard + 1
        if head is None:
            if hit["id"] not in seen:
                seen.add(hit["id"])
                out.append(hit)
            continue
        if head["id"] not in seen:
            seen.add(head["id"])
            # Carry the stale hit's ranking so result ordering stays stable.
            out.append({
                **head,
                "rrf_score": hit.get("rrf_score"),
                "semantic_score": hit.get("semantic_score"),
                "keyword_score": hit.get("keyword_score"),
                "score": hit.get("score", hit.get("rrf_score")),
                "superseded_from": hit["id"],
            })
        # else head already present — drop the stale duplicate
    return out

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
_CLAIM_TYPES = {"decision", "lesson", "fact", "preference", "prediction"}
_WHY_REQUIRED_TYPES = {"decision", "lesson"}


@mcp.tool()
def add_memory(content: str, agent: str, project: str | None = None, metadata: dict[str, Any] | None = None, infer: bool = False, subject: str | None = None, self_checked: bool = False, type: str = "fact", why: str | None = None) -> str:
    """Store a memory in the shared fleet memory.

    content:  the fact / decision / lesson to remember.
    agent:    name of the writing agent (e.g. 'claude', 'miner', 'overseer-bot').
    project:  optional project slug (e.g. 'atila', 'lexradar'). Stored under fleet:{project}.
    metadata: optional extra tags — merged with source provenance.
    infer:    False (default) stores content verbatim as one atomic fact — use when the
              caller already extracted a single fact. True re-runs mem0 LLM extraction to
              split/dedup — use only for raw multi-fact conversation snippets.
    subject:  optional explicit subject of the memory. When given it is validated
              (must be explicit and present in content) and stored in metadata.
    self_checked: set True to bypass the deterministic write guardrail when you have
              confirmed the memory is self-contained despite a flag (logged as override).
    type:     Write Contract claim type — one of
              decision|lesson|fact|preference|prediction.
              Optional, defaults to "fact" (existing callers that omit it are
              unaffected). Case-insensitive, normalized on write. Stored in
              metadata.claim_type.
    why:      optional rationale string. REQUIRED when type is "decision" or
              "lesson" (Write Contract rule 3) — omitting it is rejected with
              MEMORY_NEEDS_WHY, not bypassable by self_checked (structural
              requirement, cheap to satisfy). Stored in metadata.why AND
              appended to the stored content ("\\n\\nWhy: <why>") so recall
              surfaces the rationale; not required for fact/preference/prediction.
              type="prediction": a dated, falsifiable prediction about a named
              system. REQUIRES metadata to carry non-empty "expires_on" (ISO
              date string, YYYY-MM-DD, must parse via date.fromisoformat) and
              non-empty "verify_hint" (a string describing how to check the
              prediction later) — omitting either is rejected with
              MEMORY_PREDICTION_NEEDS_FIELDS, not bypassable by self_checked.
              meta["status"] defaults to "open" unless the caller supplies a
              status in metadata.

    Write guardrail: a deterministic detector (no LLM) screens for vague,
    context-dependent candidates. If it flags one, NOTHING is stored and a
    self_check JSON is returned — rewrite the memory self-contained, or resubmit
    unchanged with self_checked=true. Clean candidates are stored immediately.

    Write CONTRACT gate (2026-07-12): after the vagueness wheel, three more
    steps run in order — (1) Rule-8 authority cross-check against the fleet
    registry mirror (deterministic, NOT bypassable — MEMORY_AUTHORITY_CONFLICT
    on a hard placement contradiction), (2) an LLM quality gate (advisory,
    bypassable with self_checked=true — MEMORY_FAILS_WRITE_CONTRACT on REJECT,
    fails OPEN if the gate LLM is unreachable), (3) subject alias
    canonicalization (metadata.subject set to the canonical form, original
    preserved in metadata.raw_subject).

    Write CONTRACT rules 2/3 gate (2026-07-13): a new deterministic step runs
    right after subject alias canonicalization, before the authority check —
    (a) `type` is normalized/validated against the enum, unknown values are
    rejected with MEMORY_INVALID_TYPE (NOT bypassable — a typo must not
    silently disable the rule-3 check below); (b) if type is decision/lesson
    and `why` is empty, rejected with MEMORY_NEEDS_WHY (NOT bypassable by
    self_checked — structural, not advisory). `why` is value-scanned by the
    secret detector alongside content, then appended to the stored content
    (not fed through the vagueness wheel or the LLM gate, to avoid false
    positives on rationale prose) right before the memory.add() call.
    """
    namespace = f"{FLEET_NS}:{project}" if project else FLEET_NS

    # --- Wheel 3: secret detector (NOT bypassable by self_checked) -----------
    # Run BEFORE the vagueness guardrail so a secret is never stored even when
    # the writing agent has pre-approved with self_checked=True. `why` is
    # scanned alongside content since it is a second value channel that ends
    # up in both metadata and (later) the stored content.
    # If secrets are detected: store NOTHING, log WARNING (redacted), return
    # MEMORY_CONTAINS_SECRET.  self_checked=true has NO effect here.
    secret_flags = list(detect_secrets(content))
    if why:
        for f in detect_secrets(why):
            if f not in secret_flags:
                secret_flags.append(f)
    if secret_flags:
        log.warning(
            "add_memory SECRET BLOCKED by %s ns=%s flags=%s",
            agent, namespace, secret_flags,
        )
        return json.dumps(build_secret_block(secret_flags))

    # --- Wheel 1: vagueness guardrail — challenge only on detected ambiguity ---
    flags = detect(content, subject)
    if flags:
        if not self_checked:
            log.info("add_memory self_check by %s ns=%s flags=%s", agent, namespace, flags)
            return json.dumps(build_self_check(flags))
        log.warning("add_memory self_checked OVERRIDE by %s ns=%s flags=%s content=%r",
                    agent, namespace, flags, content[:160])

    # --- subject alias canonicalization (Feature 4) ---------------------------
    canonical_subject, raw_subject = subject_alias.canonicalize(subject)
    log_subject = canonical_subject or subject

    # --- Write CONTRACT rules 2/3: typed claim + why (deterministic, NOT
    # bypassable) --------------------------------------------------------------
    # Runs after subject validation, before the authority/LLM gates. A typo in
    # `type` must not silently disable the rule-3 why-requirement below, so
    # unknown values are rejected rather than coerced to "fact".
    claim_type = (type or "fact").strip().lower()
    if claim_type not in _CLAIM_TYPES:
        log.info("add_memory INVALID TYPE by %s ns=%s type=%r", agent, namespace, type)
        return json.dumps({
            "stored": False,
            "error": "MEMORY_INVALID_TYPE",
            "message": f"type={type!r} is not one of {sorted(_CLAIM_TYPES)}.",
        })
    why_clean = why.strip() if why else ""
    if claim_type in _WHY_REQUIRED_TYPES and not why_clean:
        log.info("add_memory NEEDS WHY by %s ns=%s type=%s", agent, namespace, claim_type)
        return json.dumps({
            "stored": False,
            "error": "MEMORY_NEEDS_WHY",
            "message": (
                f"type={claim_type} requires a 'why' (the reason/rationale), "
                "per Write Contract rule 3."
            ),
        })

    # --- prediction structural requirement (deterministic, NOT bypassable) --
    # A prediction is only useful if it can later be checked and closed out,
    # so metadata must carry an ISO expires_on date and a verify_hint. This
    # mirrors the NEEDS_WHY check above and is NOT bypassable by self_checked.
    if claim_type == "prediction":
        pred_meta = metadata or {}
        expires_on = str(pred_meta.get("expires_on") or "").strip()
        verify_hint = str(pred_meta.get("verify_hint") or "").strip()
        expires_on_valid = False
        if expires_on:
            try:
                datetime.date.fromisoformat(expires_on)
                expires_on_valid = True
            except ValueError:
                expires_on_valid = False
        if not expires_on_valid or not verify_hint:
            log.info("add_memory PREDICTION NEEDS FIELDS by %s ns=%s expires_on=%r verify_hint=%r",
                      agent, namespace, expires_on, verify_hint)
            return json.dumps({
                "stored": False,
                "error": "MEMORY_PREDICTION_NEEDS_FIELDS",
                "message": (
                    "type=prediction requires metadata.expires_on (ISO date "
                    "string, YYYY-MM-DD) and metadata.verify_hint (a string "
                    "describing how to check the prediction later)."
                ),
            })

    # --- Rule 8: authority cross-check (deterministic, NOT bypassable) -------
    # Runs AFTER the vagueness wheel, BEFORE the LLM gate — a hard registry
    # contradiction is rejected outright and never reaches the (slower, best-
    # effort) LLM call. self_checked has no effect here, mirroring the secret
    # gate: a placement contradiction must never be stored.
    authority_flags, authority_available = authority.check(content)
    if authority_flags:
        log.warning("add_memory AUTHORITY CONFLICT by %s ns=%s flags=%s", agent, namespace, authority_flags)
        return json.dumps(authority.build_authority_block(authority_flags))

    # --- LLM gate (semantic, advisory-strict, bypassable with self_checked) --
    gate_verdict, gate_raw, gate_backend = gate.evaluate(content)
    if gate_verdict == "reject" and not self_checked:
        gate.append_log("rejected", log_subject, content, gate_backend)
        log.info("add_memory LLM GATE REJECT by %s ns=%s answer=%r", agent, namespace, gate_raw)
        return json.dumps({
            "stored": False,
            "error": "MEMORY_FAILS_WRITE_CONTRACT",
            "gate_answer": gate_raw,
            "action": (
                "The write-contract gate judged this candidate not worth storing long-term. "
                "Resubmit with self_checked=true to store anyway, or rewrite as a decision, "
                "lesson, durable fact, or stable preference."
            ),
        })
    if gate_verdict == "reject" and self_checked:
        gate_meta = "bypassed"
        log.warning("add_memory LLM GATE OVERRIDE by %s ns=%s answer=%r", agent, namespace, gate_raw)
    elif gate_verdict == "store":
        gate_meta = "passed"
    else:
        gate_meta = "skipped"
    gate.append_log(gate_meta, log_subject, content, gate_backend)

    meta = dict(metadata or {})
    meta["source"] = agent
    meta["gate"] = gate_meta
    meta["claim_type"] = claim_type
    if why_clean:
        meta["why"] = why_clean
    if claim_type == "prediction":
        meta.setdefault("status", "open")
    if authority_available:
        meta["authority_checked"] = True
    if canonical_subject:
        meta.setdefault("subject", canonical_subject)
        if raw_subject:
            meta["raw_subject"] = raw_subject

    # Append why to the stored content (not run through vagueness/LLM gates
    # above — rationale prose can read like dangling reference or throwaway
    # chatter and would false-positive there) so semantic + FTS recall surface
    # the rationale, not just metadata. Skip if already present verbatim.
    store_content = content
    if why_clean and "why:" not in content.lower():
        store_content = content.rstrip() + "\n\nWhy: " + why_clean

    eff_infer = infer and not KEYLESS
    result = memory.add(store_content, user_id=namespace, metadata=meta, infer=eff_infer)
    # Mirror each stored memory into the FTS5 side index (best-effort).
    for r in (result.get("results") or []):
        if r.get("id"):
            fts.mirror(r["id"], namespace, r.get("memory") or store_content, meta)
    _emit_metric(len(content))
    log.info("add_memory by %s ns=%s infer=%s gate=%s -> %s", agent, namespace, eff_infer, gate_meta, result)
    return json.dumps(result, default=str)


@mcp.tool()
def search_memory(query: str, limit: int = 5, project: str | None = None,
                  include_superseded: bool = False) -> str:
    """Search the shared fleet memory.

    query:   natural-language query.
    limit:   max results (default 5).
    project: if set, searches fleet:{project} and global fleet merged by score.
    include_superseded: when True, returns stale facts as-is instead of resolving
             each to its current head (default False). Use to inspect fact history.
    """
    if not query or not query.strip():
        return json.dumps({"results": []})

    namespaces = [f"{FLEET_NS}:{project}", FLEET_NS] if project else [FLEET_NS]

    # Over-fetch so the supersession swap/dedup still yields up to `limit` rows.
    fetch_n = limit if include_superseded else limit * 3

    # 1. semantic retrieval (Qdrant via mem0), deduped across namespaces by score.
    # Per-namespace timing: each memory.search() embeds the query remotely (one cloud
    # RTT per namespace — the dominant, spiky cost; local Qdrant is sub-10ms), so a
    # project-scoped search pays it twice.
    seen: set = set()
    semantic: list = []
    vec_ms: list = []
    t0 = time.perf_counter()
    for ns in namespaces:
        t_ns = time.perf_counter()
        r = memory.search(query, filters={"user_id": ns}, limit=fetch_n)
        vec_ms.append((time.perf_counter() - t_ns) * 1000)
        for item in r.get("results", []):
            if item["id"] not in seen:
                seen.add(item["id"])
                semantic.append(item)
    semantic.sort(key=lambda x: x.get("score", 0), reverse=True)

    # 2. keyword retrieval (FTS5 BM25) over the same namespaces
    t1 = time.perf_counter()
    keyword = fts.search(query, namespaces, fetch_n)

    # 3. fuse with Reciprocal Rank Fusion (scale-free; no score normalization)
    merged = rrf_merge(semantic, keyword, fetch_n)

    # 4. resolve supersession — swap each stale hit for its current head — then trim
    t2 = time.perf_counter()
    resolved = _resolve_supersession(merged, include_superseded)

    t3 = time.perf_counter()
    log.info(
        "search ns=%s vec_ms=%s fts_ms=%.0f resolve_ms=%.0f total_ms=%.0f hits=%d",
        namespaces, [round(v) for v in vec_ms],
        (t2 - t1) * 1000, (t3 - t2) * 1000, (t3 - t0) * 1000, len(resolved[:limit]),
    )

    _emit_metric(len(query))
    return json.dumps({"results": resolved[:limit]}, default=str)


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
