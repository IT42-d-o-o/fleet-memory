#!/usr/bin/env python3
"""
heal_namespaces.py — one-off: collapse case-fragmented project namespaces.

Unnormalized `project=` writers created split namespaces (fleet:MilestoneDashboard
vs fleet:milestonedashboard, fleet:FistOfGods, ...) so a session searching the
lowercase slug could not see the case-preserved facts. Found 2026-07-22 by the
recall benchmark's namespace diagnosis. server.py now slug-normalizes project on
both write and search; this script heals the existing store.

Rewrites payload user_id to fleet:{slugified} in Qdrant (set_payload,
non-destructive to content) and updates the FTS mirror's namespace column.
Idempotent — already-normalized namespaces are untouched.

Run on CT356:
  python3 /opt/memory-mcp/heal_namespaces.py [--dry-run]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")
sys.path.insert(0, "/opt/memory-mcp")

import subject_alias  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

COLLECTION = "local_ai_cross_agent_memory"
FTS_DB = "/opt/memory-mcp/fts.db"


def normalized(ns: str) -> str:
    if not ns.startswith("fleet:"):
        return ns
    return "fleet:" + subject_alias.slugify(ns.split(":", 1)[1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = QdrantClient(host="127.0.0.1", port=6333, check_compatibility=False)
    pts, off = [], None
    while True:
        res, off = client.scroll(COLLECTION, limit=500, offset=off,
                                 with_payload=True, with_vectors=False)
        pts.extend(res)
        if off is None:
            break

    moves = {}
    for pt in pts:
        ns = (pt.payload or {}).get("user_id", "")
        target = normalized(ns)
        if target != ns:
            moves.setdefault((ns, target), []).append(pt.id)

    total = 0
    for (src, dst), ids in sorted(moves.items()):
        print(f"{src} -> {dst}: {len(ids)} points")
        total += len(ids)
        if not args.dry_run:
            client.set_payload(collection_name=COLLECTION,
                               payload={"user_id": dst}, points=ids)

    db = sqlite3.connect(FTS_DB)
    rows = db.execute("SELECT DISTINCT namespace FROM mem_fts").fetchall()
    for (ns,) in rows:
        target = normalized(ns)
        if target != ns:
            n = db.execute("SELECT count(*) FROM mem_fts WHERE namespace=?", (ns,)).fetchone()[0]
            print(f"fts {ns} -> {target}: {n} rows")
            if not args.dry_run:
                db.execute("UPDATE mem_fts SET namespace=? WHERE namespace=?", (target, ns))
    if not args.dry_run:
        db.commit()
    db.close()

    print(f"done: {total} qdrant points across {len(moves)} namespace pairs"
          + ("  (DRY RUN)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
