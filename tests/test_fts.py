"""Self-contained tests for the FTS5 side index and RRF merge.

No mem0, no Qdrant, no network — pure SQLite FTS5 + the merge function, so this
runs anywhere and never touches the production memory service.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from fts_index import (  # noqa: E402
    FtsIndex,
    FUSION_STRATEGIES,
    admit_keyword_hits,
    identifier_tokens,
    keyword_coverage,
    query_has_identifier,
    rrf_merge,
    _to_match_query,
)


def _tmp() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_exact_token_recall():
    idx = FtsIndex(_tmp())
    idx.mirror("1", "fleet", "Live sirchmunk endpoint 10.98.0.136:8765/mcp on Hetzner CT336", {})
    idx.mirror("2", "fleet", "User prefers direct communication without trailing summaries", {})
    idx.mirror("3", "fleet", "Vault path secret/infra/gitea field token for overseer", {})

    assert idx.search("10.98.0.136", ["fleet"], 5)[0]["id"] == "1"
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
    assert _to_match_query("10.98.0.136") == '"10.98.0.136"'
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


def test_query_has_identifier():
    assert query_has_identifier("which container runs CT356?")          # digits
    assert query_has_identifier("curl 10.98.0.136:8765 fails")          # IP:port
    assert query_has_identifier("read secret/infra/gitea please")       # path
    assert query_has_identifier("why MEMORY_NEEDS_WHY rejection")       # ALL-CAPS code
    assert not query_has_identifier("why do new vector groups not appear")
    assert not query_has_identifier("")
    print("identifier detection OK")


def test_keyword_coverage_weighting():
    # Identifier tokens dominate the mass: matching the IP alone scores higher
    # than matching only the prose words around it.
    q = "where is 10.98.0.136 exposed"
    assert keyword_coverage(q, "sirchmunk lives at 10.98.0.136:8765") > \
        keyword_coverage(q, "where the exposed prose matches but no address")
    assert keyword_coverage(q, "") == 0.0
    assert keyword_coverage("", "anything") == 0.0
    assert 0.0 <= keyword_coverage(q, "unrelated text entirely") < 0.3
    assert keyword_coverage("CT314", "atila deploy CT314 token") == 1.0
    print("coverage weighting OK")


def _fusion_fixtures():
    sem = [
        {"id": "S1", "score": 0.82, "memory": "semantic best answer"},
        {"id": "S2", "score": 0.74, "memory": "semantic runner up"},
    ]
    kw = [
        {"id": "K1", "keyword_score": -1.5, "memory": "noise matching only short words"},
        {"id": "K2", "keyword_score": -1.0, "memory": "deploy CT314 exact identifier hit"},
    ]
    return sem, kw


def test_rrf_merge_classic_is_default_and_unchanged():
    sem, kw = _fusion_fixtures()
    assert [m["id"] for m in rrf_merge(sem, kw, 10)] == \
        [m["id"] for m in rrf_merge(sem, kw, 10, strategy="classic", query="whatever")]
    # positional legacy call shape still accepted
    assert rrf_merge(sem, kw, 10, 60)[0]["id"] in {"S1", "K1"}
    print("classic default unchanged OK")


def test_rrf_merge_gated_drops_keyword_on_prose():
    sem, kw = _fusion_fixtures()
    merged = rrf_merge(sem, kw, 10, strategy="gated",
                       query="why does the fake recorder produce pictures")
    assert [m["id"] for m in merged] == ["S1", "S2"]  # semantic-only, order kept
    print("gated prose query OK")


def test_rrf_merge_gated_keeps_keyword_on_identifier():
    sem, kw = _fusion_fixtures()
    merged = rrf_merge(sem, kw, 10, strategy="gated", query="deploy CT314")
    assert {m["id"] for m in merged} == {"S1", "S2", "K1", "K2"}
    print("gated identifier query OK")


def test_rrf_merge_gated_exact_phrase_opens_gate():
    sem, kw = _fusion_fixtures()
    merged = rrf_merge(sem, kw, 10, strategy="gated", query="exact identifier hit")
    assert any(m["id"] == "K2" for m in merged)
    print("gated exact-phrase OK")


def test_rrf_merge_gated_coverage_composes():
    sem, kw = _fusion_fixtures()
    # prose query: gate closed -> semantic-only, same as plain gated
    assert [m["id"] for m in rrf_merge(sem, kw, 10, strategy="gated_coverage",
                                       query="why does the recorder produce pictures")] == ["S1", "S2"]
    # identifier query: gate opens but votes are coverage-weighted -> the
    # covering hit votes, the zero-coverage noise hit is dropped entirely
    merged = rrf_merge(sem, kw, 10, strategy="gated_coverage", query="deploy CT314")
    ids = {m["id"] for m in merged}
    assert "K2" in ids and "K1" not in ids
    print("gated-coverage compose OK")


def test_rrf_merge_coverage_damps_noise():
    sem, kw = _fusion_fixtures()
    merged = rrf_merge(sem, kw, 10, strategy="coverage", query="deploy CT314")
    ids = {m["id"] for m in merged}
    # K2 covers the query -> votes; K1 has zero coverage -> dropped, never a
    # zero-score row padding the list (review finding 5)
    assert "K2" in ids and "K1" not in ids
    # semantic votes untouched: S1 still leads over pure-keyword noise
    assert merged[0]["id"] == "S1"
    print("coverage damping OK")


def test_identifier_tokens_boundary_punct():
    assert identifier_tokens("why did deploy fail.") == []            # "fail." is prose
    assert identifier_tokens("deploy to CT314 failed") == ["CT314"]
    assert identifier_tokens("check 10.98.0.136 now") == ["10.98.0.136"]
    assert identifier_tokens("this and/or that e-mail") == []         # prose compounds
    assert identifier_tokens("MCP CLI URL DNS") == []                 # bare acronyms
    assert identifier_tokens("top 5 issues, best 42") == []           # short numbers
    assert identifier_tokens("port 8800 open") == ["8800"]            # >=3 digits
    assert identifier_tokens("why MEMORY_NEEDS_WHY?") == ["MEMORY_NEEDS_WHY"]
    assert identifier_tokens("read secret/infra/gitea.") == ["secret/infra/gitea"]
    assert identifier_tokens("") == []
    print("identifier tokens OK")


def test_identifier_tokens_compound_rule():
    # alphabetic-only hyphen/slash compounds are prose regardless of length
    # (review v2 finding 2: long-term / open-source / read/write admitted junk)
    assert identifier_tokens("a long-term open-source read/write plan") == []
    assert identifier_tokens("is api-prod down") == []
    # digit or mixed-case signal keeps a compound
    assert identifier_tokens("restart CT-314 now") == ["CT-314"]
    assert identifier_tokens("the e-Racun review queue") == ["e-Racun"]
    print("compound rule OK")


def test_identifier_tokens_dotfiles_and_paths():
    # leading dot/slash is meaningful — not stripped for known shapes
    # (review v2 finding 3: ".env" and "/etc" produced no identifier)
    assert identifier_tokens("where does .env live") == [".env"]
    assert identifier_tokens("mounts under /etc/agentry today") == ["/etc/agentry"]
    assert identifier_tokens("check /etc now.") == ["/etc"]
    # trailing punctuation still stripped off known shapes
    assert identifier_tokens("look at .gitignore.") == [".gitignore"]
    print("dotfile/path shapes OK")


def test_match_query_expands_identifier_variants():
    # CT314 must also retrieve CT-314 / CT 314 spellings and vice versa
    m = _to_match_query("CT314")
    assert '"CT314"' in m and '"CT 314"' in m
    m2 = _to_match_query("CT-314")
    assert '"CT-314"' in m2 and '"CT314"' in m2
    # prose does not expand
    assert _to_match_query("deploy failed") == '"deploy" OR "failed"'
    print("match-query expansion OK")


def test_end_to_end_per_hit_through_real_fts():
    # Review v2 finding 1: prebuilt hit dicts masked the retrieval gap. This
    # goes text -> FtsIndex -> search() -> rrf_merge(per_hit) for real.
    idx = FtsIndex(_tmp())
    idx.mirror("hyph", "fleet", "deploy of CT-314 failed at boot", {})
    idx.mirror("space", "fleet", "backup CT 314 finished", {})
    idx.mirror("compact", "fleet", "CT314 compact spelling here", {})
    idx.mirror("noise", "fleet", "generic deploy prose without ids", {})

    for q in ("why did the deploy to CT314 fail",
              "why did the deploy to CT-314 fail"):
        kw = idx.search(q, ["fleet"], 10)
        got = {r["id"] for r in kw}
        assert {"hyph", "space", "compact"} <= got, (q, got)
        merged = rrf_merge([], kw, 10, strategy="per_hit", query=q)
        ids = {m["id"] for m in merged}
        assert {"hyph", "space", "compact"} <= ids, (q, ids)
        assert "noise" not in ids
    print("end-to-end per-hit OK")


def test_admit_keyword_hits_token_boundaries():
    kw = [
        {"id": "A", "memory": "deploy of CT-314 failed at boot"},     # punct variant
        {"id": "B", "memory": "generic deployment prose, no id"},
        {"id": "C", "memory": "backup CT 314 finished"},              # split variant
        {"id": "D", "memory": "concatenated CT3141 different id"},    # NOT a boundary match
    ]
    admitted = [h["id"] for h in admit_keyword_hits("why did the deploy to CT314 fail", kw)]
    assert admitted == ["A", "C"], admitted
    # no genuine identifier -> nothing admitted, even with matching prose
    assert admit_keyword_hits("why did the deploy fail.", kw) == []
    print("per-hit admission OK")


def test_rrf_merge_per_hit_full_vote_rescues():
    # Review finding 1: with 5 semantic results at limit=5, a damped keyword
    # vote (coverage/61) can never beat 1/65. per_hit gives admitted exact-id
    # hits a FULL vote, so the keyword-only rescue lands in the top-5.
    sem = [{"id": f"S{i}", "score": 0.9 - i * 0.05, "memory": f"semantic {i}"}
           for i in range(5)]
    kw = [{"id": "K", "keyword_score": -1.0, "memory": "CT314 chat42 API address fact"},
          {"id": "N", "keyword_score": -0.5, "memory": "unrelated deployment prose"}]
    merged = rrf_merge(sem, kw, 5, strategy="per_hit",
                       query="why did the deploy to CT314 fail last week")
    ids = [m["id"] for m in merged]
    assert "K" in ids, ids          # rescued into top-5 (1/61 > 1/65)
    assert "N" not in ids           # non-admitted hit dropped, not damped
    # prose query -> semantic-only
    assert [m["id"] for m in rrf_merge(sem, kw, 5, strategy="per_hit",
                                       query="why did the deploy fail")] == \
        [f"S{i}" for i in range(5)]
    print("per-hit full-vote rescue OK")


def test_fusion_strategies_registry_and_unknown_fallback():
    assert set(FUSION_STRATEGIES) == {"classic", "coverage", "gated",
                                      "gated_coverage", "per_hit"}
    # unknown strategy inside rrf_merge degrades to classic, never raises
    sem, kw = _fusion_fixtures()
    assert [m["id"] for m in rrf_merge(sem, kw, 10, strategy="tpyo", query="x")] == \
        [m["id"] for m in rrf_merge(sem, kw, 10)]
    print("strategy registry OK")


def test_rrf_merge_weight_failure_falls_back():
    # A weighting failure must never break search: item whose memory object
    # explodes on str() still merges (weight falls back to 1.0).
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")
    sem, _ = _fusion_fixtures()
    kw = [{"id": "K", "keyword_score": -1.0, "memory": Boom()}]
    merged = rrf_merge(sem, kw, 10, strategy="coverage", query="deploy CT314")
    assert any(m["id"] == "K" for m in merged)  # classic-weight fallback
    print("weight-failure fallback OK")


if __name__ == "__main__":
    for fn in [
        test_exact_token_recall,
        test_namespace_filter,
        test_mirror_is_upsert,
        test_rebuild_atomic,
        test_match_query_neutralizes_operators,
        test_rrf_merge_fuses_and_ranks,
        test_search_failures_are_safe,
        test_query_has_identifier,
        test_keyword_coverage_weighting,
        test_rrf_merge_classic_is_default_and_unchanged,
        test_rrf_merge_gated_drops_keyword_on_prose,
        test_rrf_merge_gated_keeps_keyword_on_identifier,
        test_rrf_merge_gated_exact_phrase_opens_gate,
        test_rrf_merge_gated_coverage_composes,
        test_rrf_merge_coverage_damps_noise,
        test_identifier_tokens_boundary_punct,
        test_identifier_tokens_compound_rule,
        test_identifier_tokens_dotfiles_and_paths,
        test_match_query_expands_identifier_variants,
        test_end_to_end_per_hit_through_real_fts,
        test_admit_keyword_hits_token_boundaries,
        test_rrf_merge_per_hit_full_vote_rescues,
        test_fusion_strategies_registry_and_unknown_fallback,
        test_rrf_merge_weight_failure_falls_back,
    ]:
        fn()
    print("ALL FTS TESTS PASS")
