"""Self-contained tests for dedup_stage.py's pure logic.

NO qdrant_client, NO httpx, NO network -- this file proves the module imports
and its pure functions work even when neither package is installed (the
module lazily imports both only inside the functions that actually need
them: _get_qdrant_client, scroll_all_with_vectors's caller, _resident_models,
_call_judge_backend, push_metrics).
"""
import builtins
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import dedup_stage  # noqa: E402

from dedup_stage import (  # noqa: E402
    skeleton,
    digit_sequences,
    is_number_guard_quarantine,
    cosine_similarity,
    UnionFind,
    cluster_pairs,
    completeness_score,
    choose_head,
    candidate_pairs_for_group,
)


# ---------------------------------------------------------------------------
# Lazy-import proof: dedup_stage must be importable -- and every pure
# function above must be callable -- with qdrant_client and httpx BOTH
# actually unavailable, not merely "not yet imported". We prove this by
# blocking both names at the __import__ level and forcing a fresh re-import
# of dedup_stage; if the module (or anything it imports at module scope)
# eagerly needed either package, this raises ImportError and the test fails.
# ---------------------------------------------------------------------------

def test_dedup_stage_imports_without_qdrant_or_httpx():
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
        if mod == "dedup_stage" or mod.split(".")[0] in blocked
    }
    for mod in saved_modules:
        del sys.modules[mod]

    builtins.__import__ = fake_import
    try:
        fresh = importlib.import_module("dedup_stage")
        assert fresh is not None
        # sanity: the pure functions still work on the freshly (blocked) import
        assert fresh.skeleton("bumped to 42") == "bumped to #"
    finally:
        builtins.__import__ = real_import
        # Restore the original module objects so the rest of this test file
        # (which already bound names from the real dedup_stage) is unaffected.
        for mod in list(sys.modules):
            if mod == "dedup_stage" or mod.split(".")[0] in blocked:
                del sys.modules[mod]
        sys.modules.update(saved_modules)
    print("dedup_stage imports cleanly with qdrant_client/httpx blocked OK")


# ---------------------------------------------------------------------------
# skeleton / number guard
# ---------------------------------------------------------------------------

def test_skeleton_blanks_digits_and_collapses_whitespace():
    assert skeleton("VERSION_CODE bumped to 42") == "version_code bumped to #"
    assert skeleton("VERSION_CODE   bumped to   42") == "version_code bumped to #"
    assert skeleton("no digits here") == "no digits here"
    print("skeleton OK")


def test_digit_sequences_extraction():
    assert digit_sequences("bumped to 42 from 41") == ["42", "41"]
    assert digit_sequences("no digits") == []
    print("digit_sequences OK")


def test_number_guard_quarantines_equal_skeleton_different_numbers():
    a = "VERSION_CODE bumped to 42"
    b = "VERSION_CODE bumped to 43"
    assert is_number_guard_quarantine(a, b) is True
    print("number guard quarantines differing numbers OK")


def test_number_guard_allows_equal_skeleton_same_numbers():
    a = "VERSION_CODE bumped to 42"
    b = "VERSION_CODE bumped to 42"
    assert is_number_guard_quarantine(a, b) is False
    print("number guard passes identical numbers OK")


def test_number_guard_allows_different_skeleton():
    a = "VERSION_CODE bumped to 42"
    b = "Deployment port changed to 42"
    assert is_number_guard_quarantine(a, b) is False
    print("number guard passes different skeleton OK")


# ---------------------------------------------------------------------------
# cosine similarity
# ---------------------------------------------------------------------------

def test_cosine_similarity_identical_vectors():
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9
    print("cosine identical OK")


def test_cosine_similarity_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(cosine_similarity(a, b) - 0.0) < 1e-9
    print("cosine orthogonal OK")


def test_cosine_similarity_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-9
    print("cosine opposite OK")


def test_cosine_similarity_empty_or_mismatched_is_safe():
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    print("cosine safe-failure OK")


# ---------------------------------------------------------------------------
# union-find clustering
# ---------------------------------------------------------------------------

def test_union_find_transitive_merge():
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")
    assert uf.find("a") != uf.find("d")
    print("union-find transitive OK")


def test_cluster_pairs_groups_and_keeps_singletons():
    ids = ["a", "b", "c", "d"]
    pairs = [("a", "b"), ("b", "c")]  # d shares no pair
    clusters = cluster_pairs(ids, pairs)
    clusters_as_sets = [set(c) for c in clusters]
    assert {"a", "b", "c"} in clusters_as_sets
    assert {"d"} in clusters_as_sets
    assert len(clusters) == 2
    print("cluster_pairs grouping OK")


def test_cluster_pairs_no_pairs_all_singletons():
    ids = ["x", "y", "z"]
    clusters = cluster_pairs(ids, [])
    assert sorted(clusters) == [["x"], ["y"], ["z"]]
    print("cluster_pairs no-pairs OK")


# ---------------------------------------------------------------------------
# head selection
# ---------------------------------------------------------------------------

def test_completeness_score_why_beats_claim_type_beats_length():
    rec_why = {"why": "because it broke prod", "claim_type": None, "content": "short"}
    rec_claim_type = {"why": None, "claim_type": "lesson", "content": "a much much longer piece of content here"}
    rec_plain_long = {"why": None, "claim_type": None, "content": "an extremely long piece of content that is very descriptive"}

    score_why, _ = completeness_score(rec_why)
    score_claim, _ = completeness_score(rec_claim_type)
    score_plain, _ = completeness_score(rec_plain_long)

    assert score_why > score_claim > score_plain
    print("completeness score ordering OK")


def test_choose_head_prefers_why_over_claim_type_over_length():
    id_to_record = {
        "A": {"why": None, "claim_type": None, "content": "a somewhat longer piece of text than B"},
        "B": {"why": None, "claim_type": "lesson", "content": "short"},
        "C": {"why": "root cause was X", "claim_type": None, "content": "x"},
    }
    head = choose_head(["A", "B", "C"], id_to_record)
    assert head == "C"  # why (+2) beats claim_type (+1) beats plain length
    print("choose_head why-beats-claim_type OK")


def test_choose_head_falls_back_to_length_when_scores_tie():
    id_to_record = {
        "A": {"why": None, "claim_type": None, "content": "short"},
        "B": {"why": None, "claim_type": None, "content": "a much longer piece of content"},
    }
    assert choose_head(["A", "B"], id_to_record) == "B"
    print("choose_head length tiebreak OK")


def test_choose_head_never_consults_created_at():
    # Give the "wrong" (older-looking) one every completeness advantage and
    # confirm it wins even though a created_at field is present and would,
    # if consulted, point the other way. choose_head must not even look at it.
    id_to_record = {
        "OLD_BUT_COMPLETE": {
            "why": "documented root cause",
            "claim_type": "lesson",
            "content": "complete record",
            "created_at": "2020-01-01T00:00:00Z",
        },
        "NEW_BUT_BARE": {
            "why": None,
            "claim_type": None,
            "content": "x",
            "created_at": "2099-01-01T00:00:00Z",
        },
    }
    head = choose_head(["OLD_BUT_COMPLETE", "NEW_BUT_BARE"], id_to_record)
    assert head == "OLD_BUT_COMPLETE"
    print("choose_head ignores created_at OK")


# ---------------------------------------------------------------------------
# candidate-pair generation
# ---------------------------------------------------------------------------

def test_candidate_pairs_for_group_threshold_filter():
    records = [
        {"id": "1", "content": "a", "vector": [1.0, 0.0]},
        {"id": "2", "content": "b", "vector": [1.0, 0.0]},       # identical to 1 -> sim 1.0
        {"id": "3", "content": "c", "vector": [0.0, 1.0]},       # orthogonal to 1/2 -> sim 0.0
        {"id": "4", "content": "d", "vector": [0.99, 0.01]},     # near 1/2 but not identical
    ]
    pairs = candidate_pairs_for_group(records, threshold=0.90)
    pair_ids = {frozenset((a, b)) for a, b, _sim in pairs}

    assert frozenset(("1", "2")) in pair_ids
    assert frozenset(("1", "3")) not in pair_ids
    assert frozenset(("2", "3")) not in pair_ids
    # 4 is close enough to 1 and 2 to clear a 0.90 threshold
    assert frozenset(("1", "4")) in pair_ids
    assert frozenset(("2", "4")) in pair_ids
    print("candidate_pairs_for_group threshold filter OK")


def test_candidate_pairs_for_group_skips_missing_vectors():
    records = [
        {"id": "1", "content": "a", "vector": [1.0, 0.0]},
        {"id": "2", "content": "b", "vector": None},
    ]
    pairs = candidate_pairs_for_group(records, threshold=0.5)
    assert pairs == []
    print("candidate_pairs_for_group missing-vector safety OK")


if __name__ == "__main__":
    for fn in [
        test_dedup_stage_imports_without_qdrant_or_httpx,
        test_skeleton_blanks_digits_and_collapses_whitespace,
        test_digit_sequences_extraction,
        test_number_guard_quarantines_equal_skeleton_different_numbers,
        test_number_guard_allows_equal_skeleton_same_numbers,
        test_number_guard_allows_different_skeleton,
        test_cosine_similarity_identical_vectors,
        test_cosine_similarity_orthogonal_vectors,
        test_cosine_similarity_opposite_vectors,
        test_cosine_similarity_empty_or_mismatched_is_safe,
        test_union_find_transitive_merge,
        test_cluster_pairs_groups_and_keeps_singletons,
        test_cluster_pairs_no_pairs_all_singletons,
        test_completeness_score_why_beats_claim_type_beats_length,
        test_choose_head_prefers_why_over_claim_type_over_length,
        test_choose_head_falls_back_to_length_when_scores_tie,
        test_choose_head_never_consults_created_at,
        test_candidate_pairs_for_group_threshold_filter,
        test_candidate_pairs_for_group_skips_missing_vectors,
    ]:
        fn()
    print("ALL DEDUP_STAGE TESTS PASS")
