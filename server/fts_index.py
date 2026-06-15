"""
fts_index.py — SQLite FTS5 keyword side index for fleet-memory hybrid search.

Mirrors mem0/Qdrant memories into a local FTS5 table so exact lexical tokens
(IPs, CT ids, Vault paths, env keys, error strings) are retrievable by BM25
keyword match. Qdrant stays the semantic primary and the source of truth; this
index is a derived mirror, rebuilt from Qdrant on demand (see rebuild_fts.py).

Hybrid merge uses Reciprocal Rank Fusion (RRF), which is scale-free and needs
no score normalization between cosine similarity and BM25.
"""
import json
import logging
import re
import sqlite3
import threading

log = logging.getLogger("memory-mcp.fts")

# RRF constant. 60 is the standard value from the original RRF paper
# (Cormack et al. 2009). Larger k flattens rank influence; smaller k sharpens it.
RRF_K = 60

# FTS5 MATCH treats many characters as operators/syntax. For keyword recall over
# identifiers (IPs, paths, CT-ids, error strings) we tokenize the query into bare
# words and OR them as quoted phrases, so "10.20.0.136" or "secret/foo" survive.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/@-]+")


def _to_match_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    # Quote each token to neutralize FTS5 operator chars; OR them for recall.
    return " OR ".join('"%s"' % t.replace('"', '""') for t in tokens)


class FtsIndex:
    """Thread-safe FTS5 side index. All public methods are best-effort and never
    raise — a failing keyword index must never break the primary memory path."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "memory_id UNINDEXED, namespace UNINDEXED, memory, "
            "metadata UNINDEXED, tokenize='unicode61')"
        )
        self._db.commit()

    def mirror(self, memory_id: str, namespace: str, text: str, metadata: dict | None) -> None:
        """Insert/refresh one memory in the index. Best-effort; never raises."""
        if not memory_id or not text:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM mem_fts WHERE memory_id = ?", (memory_id,))
                self._db.execute(
                    "INSERT INTO mem_fts(memory_id, namespace, memory, metadata) "
                    "VALUES (?,?,?,?)",
                    (memory_id, namespace, text, json.dumps(metadata or {}, default=str)),
                )
                self._db.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort side index
            log.warning("fts mirror failed id=%s: %s", memory_id, exc)

    def search(self, query: str, namespaces: list[str], limit: int) -> list[dict]:
        """BM25 keyword search restricted to `namespaces`. Returns an ordered list
        of {id, memory, keyword_score} best-first. Never raises."""
        match = _to_match_query(query)
        if not match or not namespaces:
            return []
        try:
            placeholders = ",".join("?" * len(namespaces))
            sql = (
                "SELECT memory_id, memory, bm25(mem_fts) AS rank "
                "FROM mem_fts "
                f"WHERE mem_fts MATCH ? AND namespace IN ({placeholders}) "
                "ORDER BY rank LIMIT ?"
            )
            with self._lock:
                rows = self._db.execute(sql, (match, *namespaces, limit)).fetchall()
            # bm25() returns negative scores where lower = better; surfaced raw.
            return [{"id": r[0], "memory": r[1], "keyword_score": r[2]} for r in rows]
        except Exception as exc:  # noqa: BLE001 — best-effort side index
            log.warning("fts search failed q=%r: %s", query, exc)
            return []

    def rebuild(self, rows) -> int:
        """Atomically replace the whole index from an iterable of
        (memory_id, namespace, text, metadata) tuples. Returns new row count."""
        payload = [
            (i, ns, t, json.dumps(m or {}, default=str))
            for i, ns, t, m in rows
            if i and t
        ]
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM mem_fts")
            self._db.executemany(
                "INSERT INTO mem_fts(memory_id, namespace, memory, metadata) "
                "VALUES (?,?,?,?)",
                payload,
            )
            self._db.commit()
        return self.count()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM mem_fts").fetchone()[0]


def rrf_merge(semantic: list[dict], keyword: list[dict], limit: int, k: int = RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion of two ranked result lists.

    semantic: mem0 result dicts (have 'id', 'score'=cosine, 'memory', metadata...)
    keyword:  {'id','memory','keyword_score'} dicts from FtsIndex.search

    Returns merged mem0-shaped dicts with added rrf_score / semantic_score /
    keyword_score debug fields, best-first, truncated to `limit`. Each result is
    guaranteed a 'score' key (falls back to rrf_score for keyword-only hits) so
    existing consumers keep working.
    """
    fused: dict = {}

    def _add(items: list[dict], score_key: str, out_field: str) -> None:
        for rank, item in enumerate(items):
            mid = item.get("id")
            if not mid:
                continue
            entry = fused.setdefault(
                mid,
                {"item": item, "rrf_score": 0.0, "semantic_score": None, "keyword_score": None},
            )
            entry["rrf_score"] += 1.0 / (k + rank + 1)
            if score_key in item:
                entry[out_field] = item[score_key]
            # Prefer the richer mem0 item (carries metadata) as the base record.
            if item.get("metadata") and not entry["item"].get("metadata"):
                entry["item"] = item

    _add(semantic, "score", "semantic_score")
    _add(keyword, "keyword_score", "keyword_score")

    out = []
    for mid, entry in fused.items():
        merged = dict(entry["item"])
        merged["id"] = mid
        merged["rrf_score"] = round(entry["rrf_score"], 6)
        merged["semantic_score"] = entry["semantic_score"]
        merged["keyword_score"] = entry["keyword_score"]
        merged.setdefault("score", merged["rrf_score"])
        out.append(merged)
    out.sort(key=lambda x: x["rrf_score"], reverse=True)
    return out[:limit]
