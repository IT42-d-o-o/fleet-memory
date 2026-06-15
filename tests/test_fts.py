"""Self-contained tests for the FTS5 side index and RRF merge.

No mem0, no Qdrant, no network — pure SQLite FTS5 + the merge function, so this
runs anywhere and never touches the production memory service.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from fts_index import FtsIndex, rrf_merge, _to_match_query  # noqa: E402


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_exact_token_recall():
    idx = FtsIndex(_tmp())
    idx.mirror("1", "fleet", "Live sirchmunk endpoint 10.20.0.136:8765/mcp on Hetzner CT336", {})
    idx.mirror("2", "fleet", "User prefers direct communication without trailing summaries", {})
    idx.mirror("3", "fleet", "Vault path secret/infra/gitea field token for overseer", {})

    assert idx.search("10.20.0.136", ["fleet"], 5)[0]["id"] == "1"
    assert idx.search("secret/infra/gitea", ["fleet"], 5)[0]["id"] == "3"
    assert any(r["id"] == "1" for r in idx.search("CT336", ["fleet"], 5))
    print("exact-token recall OK")


def test_namespace_filter():
    idx = FtsIndex(_tmp())
    idx.mirror("a", "fleet:atila", "atila deploy CT314 token", {})
    idx.mirror("b", "fleet", "global fact CT314 token", {})

    assert {r["id"] for r in idx.search("CT314", ["fleet:atila"], 5)} == {"a"}
    assert {r["id"] for r in idx.search("CT314", ["fleet:atila", "fleet"], 5)} == {"a", "b"}
    print("namespace filter OK")


def test_mirror_is_upsert():
    idx = FtsIndex(_tmp())
    idx.mirror("x", "fleet", "first version alpha", {})
    idx.mirror("x", "fleet", "second version beta", {})
    assert idx.count() == 1
    assert not idx.search("alpha", ["fleet"], 5)
    assert idx.search("beta", ["fleet"], 5)
    print("mirror upsert OK")


def test_rebuild_atomic():
    idx = FtsIndex(_tmp())
    idx.mirror("old", "fleet", "stale data", {})
    n = idx.rebuild([("y", "fleet", "fresh data token42", {})])
    assert n == 1 and idx.count() == 1
    assert not idx.search("stale", ["fleet"], 5)
    assert idx.search("token42", ["fleet"], 5)
    print("rebuild atomic OK")


def test_match_query_neutralizes_operators():
    # Bare FTS5 would choke on these; tokenizer must quote them.
    assert _to_match_query("10.20.0.136") == '"10.20.0.136"'
    assert "OR" in _to_match_query("CT336 secret/infra/gitea")
    assert _to_match_query("   ") == ""
    print("match-query neutralize OK")


def test_rrf_merge_fuses_and_ranks():
    sem = [
        {"id": "A", "score": 0.91, "memory": "a", "metadata": {"k": 1}},
        {"id": "B", "score": 0.80, "memory": "b"},
    ]
    kw = [
        {"id": "B", "keyword_score": -1.2, "memory": "b"},
        {"id": "C", "keyword_score": -2.0, "memory": "c"},
    ]
    merged = rrf_merge(sem, kw, 10)
    ids = [m["id"] for m in merged]

    assert ids[0] == "B", ids                  # appears in both → top
    assert set(ids) == {"A", "B", "C"}
    b = next(m for m in merged if m["id"] == "B")
    assert b["semantic_score"] == 0.80 and b["keyword_score"] == -1.2
    assert all("rrf_score" in m and "score" in m for m in merged)
    # keyword-only hit still gets a usable score
    c = next(m for m in merged if m["id"] == "C")
    assert c["score"] == c["rrf_score"]
    print("rrf merge OK")


def test_search_failures_are_safe():
    idx = FtsIndex(_tmp())
    assert idx.search("", ["fleet"], 5) == []
    assert idx.search("anything", [], 5) == []
    print("safe-failure OK")


if __name__ == "__main__":
    for fn in [
        test_exact_token_recall,
        test_namespace_filter,
        test_mirror_is_upsert,
        test_rebuild_atomic,
        test_match_query_neutralizes_operators,
        test_rrf_merge_fuses_and_ranks,
        test_search_failures_are_safe,
    ]:
        fn()
    print("ALL FTS TESTS PASS")
