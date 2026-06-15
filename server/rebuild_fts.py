"""
rebuild_fts.py — rebuild the FTS5 side index from Qdrant (the source of truth).

Two uses:
  1. Backfill: first-time population of the index for memories that predate it.
  2. Reconciliation: run periodically (systemd timer / cron) to absorb any drift
     from mem0 update/dedup events, which mutate Qdrant but not the side index.

The rebuild is atomic — it builds the full row set then swaps it into the FTS5
table in one transaction, so search never sees a half-populated index.

Env (shared with server.py):
  QDRANT_HOST / QDRANT_PORT / MEM0_COLLECTION / MEM0_FTS_DB
"""
import logging
import os

from qdrant_client import QdrantClient

from fts_index import FtsIndex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fts-rebuild")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("MEM0_COLLECTION", "local_ai_cross_agent_memory")
FTS_DB = os.environ.get("MEM0_FTS_DB", "/opt/memory-mcp/fts.db")

# mem0 stores the memory text under payload key "data"; older rows may use
# "memory". Everything else in the payload is treated as metadata.
_TEXT_KEYS = ("data", "memory")
_SKIP_META = {"data", "memory", "hash"}


def iter_points():
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    offset = None
    total = 0
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            text = next((payload[k] for k in _TEXT_KEYS if payload.get(k)), "")
            if not text:
                continue
            namespace = payload.get("user_id") or ""
            meta = {k: v for k, v in payload.items() if k not in _SKIP_META}
            total += 1
            yield (str(p.id), namespace, text, meta)
        if offset is None:
            break
    log.info("scrolled %d memories from qdrant", total)


def main() -> None:
    rows = list(iter_points())
    idx = FtsIndex(FTS_DB)
    n = idx.rebuild(rows)
    log.info("rebuilt fts index %s: %d rows from collection %s", FTS_DB, n, COLLECTION)


if __name__ == "__main__":
    main()
