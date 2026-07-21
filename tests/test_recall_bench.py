"""Self-contained tests for recall_bench.py's pure logic.

NO qdrant_client, NO httpx, NO network -- this file proves the module imports
and its pure functions work even when neither package is installed (the
module lazily imports both only inside the functions that actually need
them: _get_qdrant_client, _mcp_post, push_metrics).
"""
import builtins
import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import recall_bench  # noqa: E402

from recall_bench import (  # noqa: E402
    extract_keywords,
    score_paragraph,
    top_k_paragraphs,
    is_hit,
    snippet,
    extract_result_snippets,
    shape_arm,
    build_report,
    build_paragraph,
    build_paragraphs_from_points,
    load_probes,
)


# ---------------------------------------------------------------------------
# Lazy-import proof: recall_bench must be importable -- and every pure
# function above must be callable -- with qdrant_client and httpx BOTH
# actually unavailable, not merely "not yet imported". We prove this by
# blocking both names at the __import__ level and forcing a fresh re-import
# of recall_bench; if the module (or anything it imports at module scope)
# eagerly needed either package, this raises ImportError and the test fails.
# ---------------------------------------------------------------------------

def test_recall_bench_imports_without_qdrant_or_httpx():
    blocked = {"qdrant_client", "httpx"}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        top = name.split(".")[0]
        if top in blocked:
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    saved_modules = {
        mod: sys.modules[mod]
        for mod in list(sys.modules)
        if mod == "recall_bench" or mod.split(".")[0] in blocked
    }
    for mod in saved_modules:
        del sys.modules[mod]

    builtins.__import__ = fake_import
    try:
        fresh = importlib.import_module("recall_bench")
        assert fresh is not None
        # sanity: the pure functions still work on the freshly (blocked) import
        assert fresh.is_hit("CT356 runs the server", ["ct356"]) is True
    finally:
        builtins.__import__ = real_import
        # Restore the original module objects so the rest of this test file
        # (which already bound names from the real recall_bench) is unaffected.
        for mod in list(sys.modules):
            if mod == "recall_bench" or mod.split(".")[0] in blocked:
                del sys.modules[mod]
        sys.modules.update(saved_modules)
    print("recall_bench imports cleanly with qdrant_client/httpx blocked OK")


# ---------------------------------------------------------------------------
# keyword extraction
# ---------------------------------------------------------------------------

def test_extract_keywords_strips_stopwords_and_punctuation():
    q = "Which container runs the fleet memory MCP server and on what address?"
    terms = extract_keywords(q)
    for stop in ("which", "the", "and", "on", "what"):
        assert stop not in terms
    for content in ("container", "runs", "fleet", "memory", "mcp", "server", "address"):
        assert content in terms
    print("extract_keywords stopword stripping OK")


def test_extract_keywords_lowercases():
    assert extract_keywords("CT356 IS THE HOST") == {"ct356", "host"}
    print("extract_keywords lowercasing OK")


def test_extract_keywords_empty_and_none_safe():
    assert extract_keywords("") == set()
    assert extract_keywords(None) == set()
    print("extract_keywords empty/None safety OK")


# ---------------------------------------------------------------------------
# paragraph scoring + top-k selection
# ---------------------------------------------------------------------------

def test_score_paragraph_counts_distinct_matching_terms():
    terms = {"ct356", "memory", "server"}
    para = "The memory server runs on CT356 in the fleet."
    assert score_paragraph(para, terms) == 3
    print("score_paragraph distinct-term count OK")


def test_score_paragraph_is_word_level_not_substring():
    # "cat" must not match "category" -- word-level tokenization only.
    terms = {"cat"}
    para = "This paragraph is about category theory, not felines."
    assert score_paragraph(para, terms) == 0
    print("score_paragraph word-level (no substring false positive) OK")


def test_score_paragraph_empty_terms_is_zero():
    assert score_paragraph("anything at all", set()) == 0
    print("score_paragraph empty terms OK")


def test_top_k_paragraphs_ranks_by_score_descending():
    paragraphs = [
        "irrelevant paragraph about weather",           # score 0
        "CT356 runs the fleet memory MCP server",       # score 3 (ct356/memory/server... )
        "CT356 is a container",                         # score 1
    ]
    terms = {"ct356", "memory", "server"}
    top = top_k_paragraphs(paragraphs, terms, k=2)
    assert top[0] == paragraphs[1]
    assert top[1] == paragraphs[2]
    print("top_k_paragraphs ranking OK")


def test_top_k_paragraphs_stable_on_ties():
    # Two paragraphs with equal score -- original relative order preserved.
    paragraphs = ["alpha ct356", "beta ct356", "gamma unrelated"]
    terms = {"ct356"}
    top = top_k_paragraphs(paragraphs, terms, k=3)
    assert top[0] == "alpha ct356"
    assert top[1] == "beta ct356"
    print("top_k_paragraphs tie stability OK")


def test_top_k_paragraphs_respects_k():
    paragraphs = [f"ct356 paragraph {i}" for i in range(10)]
    terms = {"ct356"}
    top = top_k_paragraphs(paragraphs, terms, k=5)
    assert len(top) == 5
    print("top_k_paragraphs k-limit OK")


# ---------------------------------------------------------------------------
# hit detection
# ---------------------------------------------------------------------------

def test_is_hit_case_insensitive_substring():
    assert is_hit("The server runs on CT356.", ["ct356"]) is True
    assert is_hit("The server runs on ct356.", ["CT356"]) is True
    print("is_hit case-insensitivity OK")


def test_is_hit_any_of_semantics():
    text = "the address is 192.168.50.138"
    assert is_hit(text, ["CT356", "192.168.50.138"]) is True  # only 2nd matches
    assert is_hit(text, ["CT999", "nope"]) is False
    print("is_hit any-of semantics OK")


def test_is_hit_empty_expect_any_is_false():
    assert is_hit("anything", []) is False
    assert is_hit("anything", None) is False
    print("is_hit empty expect_any safety OK")


def test_is_hit_empty_text_is_false():
    assert is_hit("", ["x"]) is False
    assert is_hit(None, ["x"]) is False
    print("is_hit empty text safety OK")


# ---------------------------------------------------------------------------
# snippet / result-snippet extraction
# ---------------------------------------------------------------------------

def test_snippet_truncates():
    assert snippet("a" * 200, 80) == "a" * 80
    assert snippet("short") == "short"
    print("snippet truncation OK")


def test_extract_result_snippets_from_valid_payload():
    raw = json.dumps({"results": [{"memory": "fact one"}, {"memory": "fact two"}]})
    snippets = extract_result_snippets(raw)
    assert snippets == ["fact one", "fact two"]
    print("extract_result_snippets valid payload OK")


def test_extract_result_snippets_falls_back_on_garbage():
    snippets = extract_result_snippets("not json at all")
    assert snippets == ["not json at all"]
    print("extract_result_snippets garbage fallback OK")


def test_extract_result_snippets_handles_missing_results_key():
    snippets = extract_result_snippets(json.dumps({"unexpected": "shape"}))
    assert snippets == []
    print("extract_result_snippets missing-key safety OK")


# ---------------------------------------------------------------------------
# report shaping
# ---------------------------------------------------------------------------

def test_shape_arm_computes_hit_rate():
    arm = shape_arm(hits=3, total=4, misses=[{"id": 4}])
    assert arm == {"hits": 3, "total": 4, "hit_rate": 0.75, "misses": [{"id": 4}]}
    print("shape_arm hit_rate computation OK")


def test_shape_arm_zero_total_is_safe():
    arm = shape_arm(hits=0, total=0, misses=[])
    assert arm["hit_rate"] == 0.0
    print("shape_arm zero-total safety OK")


def test_build_report_shape_with_baseline():
    mcp_arm = shape_arm(2, 2, [])
    baseline_arm = shape_arm(1, 2, [{"id": 2}])
    report = build_report("2026-07-21T00:00:00Z", 2, mcp_arm, baseline_arm)
    assert report == {
        "run_at": "2026-07-21T00:00:00Z",
        "probes": 2,
        "mcp": mcp_arm,
        "baseline": baseline_arm,
    }
    print("build_report shape with baseline OK")


def test_build_report_baseline_none_when_not_run():
    mcp_arm = shape_arm(1, 1, [])
    report = build_report("2026-07-21T00:00:00Z", 1, mcp_arm, None)
    assert report["baseline"] is None
    print("build_report baseline-None OK")


# ---------------------------------------------------------------------------
# markdown paragraph building (pure -- no qdrant_client import needed for
# these helpers, only fake point-like objects)
# ---------------------------------------------------------------------------

class _FakePoint:
    def __init__(self, payload):
        self.payload = payload


def test_build_paragraph_format():
    para = build_paragraph("fleet-memory", "runs on CT356")
    assert para == "## fleet-memory\n\nruns on CT356"
    print("build_paragraph format OK")


def test_build_paragraph_defaults_unknown_subject():
    para = build_paragraph("", "some fact")
    assert para.startswith("## unknown")
    print("build_paragraph default subject OK")


def test_build_paragraphs_from_points_skips_non_current():
    points = [
        _FakePoint({"data": "still true", "subject": "fleet-memory", "current": True}),
        _FakePoint({"data": "stale fact", "subject": "fleet-memory", "current": False}),
        _FakePoint({"data": "no current flag -- treated as current", "subject": "fleet-memory"}),
    ]
    paragraphs = build_paragraphs_from_points(points)
    assert len(paragraphs) == 2
    assert any("still true" in p for p in paragraphs)
    assert not any("stale fact" in p for p in paragraphs)
    assert any("no current flag" in p for p in paragraphs)
    print("build_paragraphs_from_points current-filter OK")


def test_build_paragraphs_from_points_skips_empty_content():
    points = [_FakePoint({"data": "", "subject": "x", "current": True})]
    assert build_paragraphs_from_points(points) == []
    print("build_paragraphs_from_points empty-content skip OK")


# ---------------------------------------------------------------------------
# probe loading (stdlib file IO only -- no network)
# ---------------------------------------------------------------------------

def test_load_probes_valid_file():
    probes_data = [
        {"id": 1, "question": "q1", "expect_any": ["a"], "subject": "s"},
        {"id": 2, "question": "q2", "expect_any": ["b"]},
    ]
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(probes_data, fh)
        loaded = load_probes(path)
        assert loaded == probes_data
    finally:
        os.unlink(path)
    print("load_probes valid file OK")


def test_load_probes_rejects_non_list():
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"not": "a list"}, fh)
        try:
            load_probes(path)
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        os.unlink(path)
    print("load_probes rejects non-list OK")


def test_load_probes_rejects_missing_required_keys():
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([{"id": 1, "question": "q1"}], fh)  # missing expect_any
        try:
            load_probes(path)
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        os.unlink(path)
    print("load_probes rejects missing keys OK")


def test_load_probes_raises_on_missing_file():
    try:
        load_probes("/nonexistent/path/probes.json")
        assert False, "expected an exception"
    except (FileNotFoundError, OSError):
        pass
    print("load_probes raises on missing file OK")


if __name__ == "__main__":
    for fn in [
        test_recall_bench_imports_without_qdrant_or_httpx,
        test_extract_keywords_strips_stopwords_and_punctuation,
        test_extract_keywords_lowercases,
        test_extract_keywords_empty_and_none_safe,
        test_score_paragraph_counts_distinct_matching_terms,
        test_score_paragraph_is_word_level_not_substring,
        test_score_paragraph_empty_terms_is_zero,
        test_top_k_paragraphs_ranks_by_score_descending,
        test_top_k_paragraphs_stable_on_ties,
        test_top_k_paragraphs_respects_k,
        test_is_hit_case_insensitive_substring,
        test_is_hit_any_of_semantics,
        test_is_hit_empty_expect_any_is_false,
        test_is_hit_empty_text_is_false,
        test_snippet_truncates,
        test_extract_result_snippets_from_valid_payload,
        test_extract_result_snippets_falls_back_on_garbage,
        test_extract_result_snippets_handles_missing_results_key,
        test_shape_arm_computes_hit_rate,
        test_shape_arm_zero_total_is_safe,
        test_build_report_shape_with_baseline,
        test_build_report_baseline_none_when_not_run,
        test_build_paragraph_format,
        test_build_paragraph_defaults_unknown_subject,
        test_build_paragraphs_from_points_skips_non_current,
        test_build_paragraphs_from_points_skips_empty_content,
        test_load_probes_valid_file,
        test_load_probes_rejects_non_list,
        test_load_probes_rejects_missing_required_keys,
        test_load_probes_raises_on_missing_file,
    ]:
        fn()
    print("ALL RECALL_BENCH TESTS PASS")
