"""Tests for the deterministic add_memory write guardrail (no LLM, no network)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from validate import detect, build_self_check  # noqa: E402


def test_clean_memories_pass():
    clean = [
        "Fleet-memory should add an SQLite FTS5 side index while keeping Qdrant as the semantic backend.",
        "Gitea token field at Vault secret/infra/gitea is read-only; it returns 403 on writes.",
        "this server runs Qdrant on port 6333",  # 'this' bound to a noun
        "CT356 hosts the mem0 memory-mcp service on port 8800.",
    ]
    for c in clean:
        assert detect(c) == [], (c, detect(c))
    print("clean memories pass OK")


def test_leading_deixis_flagged():
    assert detect("It should add FTS5 later") and "unresolved reference 'it'" in detect("It should add FTS5 later")[0]
    assert detect("This needs a fix")
    assert detect("They are done")
    print("leading deixis flagged OK")


def test_dangling_demonstrative_flagged():
    f = detect("The index is built but that should be rebuilt nightly")
    assert any("dangling demonstrative" in x for x in f), f
    # bound demonstrative is clean
    assert detect("The index is built but that nightly job rebuilds it") == []
    print("dangling demonstrative OK")


def test_midsentence_pronoun_is_clean():
    # 'it' has a local antecedent ('token') in the same string -> must NOT flag
    assert detect("The Gitea token is read-only; it returns 403 on push") == []
    print("midsentence pronoun clean OK")


def test_relative_that_is_clean():
    # 'that'/'this' as a relative pronoun after a noun must NOT flag, but a
    # clause-starting demonstrative still must.
    assert detect("The detector ignores pronouns that have a local antecedent") == []
    assert detect("Use the index that is rebuilt nightly by the timer") == []
    f = detect("The index is built but that should be rebuilt nightly")
    assert any("dangling demonstrative" in x for x in f), f
    print("relative-that clean OK")


def test_subject_rules():
    assert "content does not contain the subject" in detect("FastAPI app on CT357", subject="Likvidator")
    assert detect("Likvidator runs on CT357", subject="Likvidator") == []
    assert "subject is a pronoun" in detect("Likvidator runs on CT357", subject="it")[0] or \
        any("pronoun" in x for x in detect("Likvidator runs on CT357", subject="it"))
    print("subject rules OK")


def test_empty_content():
    assert detect("") == ["content is empty"]
    assert detect("   ") == ["content is empty"]
    print("empty content OK")


def test_self_check_shape():
    r = build_self_check(["opens with unresolved reference 'it'"])
    assert r["stored"] is False
    assert r["error"] == "MEMORY_NEEDS_SELF_CHECK"
    assert r["flags"] and r["questions"] and r["action"]
    print("self_check shape OK")


if __name__ == "__main__":
    for fn in [
        test_clean_memories_pass,
        test_leading_deixis_flagged,
        test_dangling_demonstrative_flagged,
        test_midsentence_pronoun_is_clean,
        test_relative_that_is_clean,
        test_subject_rules,
        test_empty_content,
        test_self_check_shape,
    ]:
        fn()
    print("ALL VALIDATE TESTS PASS")
