"""Tests for the deterministic add_memory write guardrail (no LLM, no network)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from validate import detect, build_self_check, detect_secrets, build_secret_block  # noqa: E402


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


# ---------------------------------------------------------------------------
# Wheel 3 — secret detector tests
# NOTE: All token values below are SYNTHETIC / FAKE.  No real credentials.
# ---------------------------------------------------------------------------

def _has_flag(flags: list[str], prefix: str) -> bool:
    return any(f.startswith(prefix) for f in flags)


# --- Positive tests: each pattern must be caught ---------------------------

def test_secret_private_key_block():
    content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    flags = detect_secrets(content)
    assert _has_flag(flags, "private-key-block"), f"expected private-key-block, got {flags}"
    # Ensure the raw key material is not echoed in the flag string
    assert "MIIEowIBAAKCAQEA" not in " ".join(flags)
    print("private-key-block caught OK")


def test_secret_openssh_key_block():
    content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
    flags = detect_secrets(content)
    assert _has_flag(flags, "private-key-block"), flags
    print("openssh-private-key-block caught OK")


def test_secret_aws_key_id():
    # Synthetic AWS key ID — uses FAKE prefix AKIAFAKETEST00000001
    content = "AWS key: AKIAFAKETEST00000001 is configured in the CI runner"
    flags = detect_secrets(content)
    assert _has_flag(flags, "aws-key-id"), flags
    # Redacted form: first 4 chars + ****
    assert "AKIA****" in " ".join(flags) or "AKIAF****" in " ".join(flags)
    print("aws-key-id caught OK")


def test_secret_github_token():
    # Synthetic PAT (ghp_ prefix, 36 lowercase alphanumeric filler)
    fake_token = "ghp_" + "a" * 36
    content = f"GitHub token for CI: {fake_token}"
    flags = detect_secrets(content)
    assert _has_flag(flags, "github-token"), flags
    assert fake_token not in " ".join(flags)
    print("github-token caught OK")


def test_secret_github_token_oauth():
    fake_token = "gho_" + "B" * 36
    flags = detect_secrets(f"OAuth token={fake_token}")
    assert _has_flag(flags, "github-token"), flags
    print("github-oauth-token caught OK")


def test_secret_vault_token_v2():
    # hvs. prefix — Vault HCP service token
    fake_token = "hvs." + "A" * 24
    content = f"Vault token: {fake_token}"
    flags = detect_secrets(content)
    assert _has_flag(flags, "vault-token-v2"), flags
    assert fake_token not in " ".join(flags)
    print("vault-token-v2 caught OK")


def test_secret_vault_token_v1():
    # s. legacy format (26+ alphanumeric after 's.')
    fake_token = "s." + "X" * 24
    content = f"root token is {fake_token} (legacy)"
    flags = detect_secrets(content)
    assert _has_flag(flags, "vault-token-v1"), flags
    print("vault-token-v1 caught OK")


def test_secret_slack_token():
    fake_token = "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx"
    content = f"Slack bot token: {fake_token}"
    flags = detect_secrets(content)
    assert _has_flag(flags, "slack-token"), flags
    print("slack-token caught OK")


def test_secret_jwt():
    # Synthetic JWT (three base64url segments, all clearly fake)
    fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkZha2VVc2VyIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    content = f"bearer {fake_jwt}"
    flags = detect_secrets(content)
    assert _has_flag(flags, "jwt"), flags
    assert fake_jwt not in " ".join(flags)
    print("jwt caught OK")


def test_secret_inline_assignment_password():
    content = 'DB connection: password = "S3cr3t!Db99"'
    flags = detect_secrets(content)
    assert _has_flag(flags, "secret-assignment"), flags
    # Value must be redacted in the flag
    assert "S3cr3t!Db99" not in " ".join(flags)
    assert "S3cr" in " ".join(flags)  # first 4 chars present
    print("inline-password-assignment caught OK")


def test_secret_inline_assignment_api_key():
    content = "api_key: zK9mQ3pXvL7nRtYw2sJhF5bAeUoDi8Gc"
    flags = detect_secrets(content)
    assert _has_flag(flags, "secret-assignment"), flags
    print("inline-api-key-assignment caught OK")


def test_secret_inline_assignment_token():
    content = "token=ghxFAKE1234567890abcdefABCDEF"
    flags = detect_secrets(content)
    # Should be caught by assignment or high-entropy
    assert flags, f"expected a flag, got {flags}"
    print("inline-token-assignment caught OK")


def test_secret_high_entropy_catch_all():
    # Random-looking base64 string, 40 chars, not a git SHA, not preceded by a
    # secret-key-name assignment so the entropy catch-all is the one that fires.
    # Prefix with a neutral word to avoid "token:" triggering the assignment rule.
    fake_blob = "Zq7Lm3Kp9Nd2Xw5Ry8Vt4Bu6Js1Ao0Fc/EgHiPz="
    content = f"session credential: {fake_blob}"
    flags = detect_secrets(content)
    assert _has_flag(flags, "high-entropy-token"), (
        f"expected high-entropy-token, got {flags}"
    )
    print("high-entropy-token catch-all OK")


def test_secret_high_entropy_not_triggered_by_long_sentence():
    # Long English prose must NOT fire the catch-all — entropy is too low.
    long_sentence = (
        "The fleet memory service stores atomic facts extracted from agent sessions "
        "and routes writes through a deterministic guardrail that rejects vague references."
    )
    flags = detect_secrets(long_sentence)
    assert not _has_flag(flags, "high-entropy-token"), (
        f"false positive on long sentence: {flags}"
    )
    print("high-entropy not triggered by long sentence OK")


# --- Negative tests: allowed content must pass clean -----------------------

def test_secret_vault_path_refs_allowed():
    """Vault PATH references must NEVER be flagged."""
    allowed = [
        "Gitea token is at Vault secret/infra/gitea field token",
        "Cloudflare credentials at secret/infra/cloudflare — use cf-expose skill",
        "LLM key lives at secret/ai/llm field LLM_API_KEY",
        "Fleet memory seed at secret/Mem0",
        "secret/apps/infraatlas holds the seed_password field",
        "Use secret/apps/sudreg for OIB→company lookups",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on Vault path ref: {c!r} -> {flags}"
    print("vault-path-refs allowed OK")


def test_secret_redacted_values_allowed():
    """Already-redacted values must not be flagged."""
    allowed = [
        "token: sk-****",
        "api key is b7Y5****",
        "value: <REDACTED>",
        "password: ***",
        "secret: ****",
        "key=<token>",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on redacted value: {c!r} -> {flags}"
    print("redacted-values allowed OK")


def test_secret_placeholders_allowed():
    """Obvious placeholder strings must not be flagged."""
    allowed = [
        "api_key: your-api-key",
        "password: changeme",
        "token: xxxxxxxx",
        "Set token=<token> in the config",
        "insert-here is a placeholder",
        "example value goes here",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on placeholder: {c!r} -> {flags}"
    print("placeholders allowed OK")


def test_secret_git_sha_allowed():
    """40-char lowercase hex git commit SHAs must NOT be flagged."""
    sha = "da6b8b3a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f"  # 40 lowercase hex chars
    allowed = [
        f"commit {sha} fixes the routing bug",
        f"HEAD is at {sha}",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on git SHA: {c!r} -> {flags}"
    print("git-sha allowed OK")


def test_secret_normal_prose_allowed():
    """Normal prose, long sentences, paths, IPs, CT ids must not be flagged."""
    allowed = [
        "CT356 hosts the mem0 memory-mcp service on port 8800.",
        "Gitea token field at Vault secret/infra/gitea is read-only; it returns 403 on writes.",
        "Fleet-memory should add an SQLite FTS5 side index while keeping Qdrant as the semantic backend.",
        "The service is reachable at http://192.168.50.138:8800/mcp with bearer auth.",
        "Deploy path is C:\\Users\\tomis\\Projects\\ai\\fleet-memory\\server\\server.py",
        "/opt/memory-mcp/history.db is the SQLite history database.",
        "version 1.2.3 was released on 2026-06-15 with bugfix #42.",
        "The agent loop design document is at docs/agent-loop-design.md",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on normal prose: {c!r} -> {flags}"
    print("normal-prose allowed OK")


def test_secret_windows_path_allowed():
    """Windows paths must not be flagged as high-entropy tokens."""
    paths = [
        "C:\\Users\\tomis\\Projects\\ai\\fleet-memory\\server\\server.py",
        "C:\\Program Files\\SomeApp\\bin\\app.exe",
    ]
    for c in paths:
        flags = detect_secrets(c)
        assert not flags, f"False positive on Windows path: {c!r} -> {flags}"
    print("windows-path allowed OK")


def test_secret_long_sentence_allowed():
    """A long English sentence must not trigger the high-entropy catch-all."""
    long_sentence = (
        "The deterministic write guardrail screens memory candidates for vague "
        "context-dependent references before storing them in the Qdrant vector store "
        "so that every agent reading back the memory gets a self-contained atomic fact."
    )
    flags = detect_secrets(long_sentence)
    assert not flags, f"False positive on long sentence: {flags}"
    print("long-sentence allowed OK")


def test_secret_block_shape():
    """build_secret_block must return the right JSON shape."""
    flags = ["aws-key-id: AWS access key id detected (AKIA****)"]
    r = build_secret_block(flags)
    assert r["stored"] is False
    assert r["error"] == "MEMORY_CONTAINS_SECRET"
    assert r["flags"] == flags
    assert "CANNOT be bypassed" in r["action"]
    print("secret-block shape OK")


def test_secret_assignment_short_value_allowed():
    """Short assignment values (< 8 chars) must not be flagged."""
    allowed = [
        "password: yes",
        "token: 123",
        "secret: ok",
    ]
    for c in allowed:
        flags = detect_secrets(c)
        assert not flags, f"False positive on short assignment value: {c!r} -> {flags}"
    print("short-assignment-value allowed OK")


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
        # Wheel 3
        test_secret_private_key_block,
        test_secret_openssh_key_block,
        test_secret_aws_key_id,
        test_secret_github_token,
        test_secret_github_token_oauth,
        test_secret_vault_token_v2,
        test_secret_vault_token_v1,
        test_secret_slack_token,
        test_secret_jwt,
        test_secret_inline_assignment_password,
        test_secret_inline_assignment_api_key,
        test_secret_inline_assignment_token,
        test_secret_high_entropy_catch_all,
        test_secret_high_entropy_not_triggered_by_long_sentence,
        test_secret_vault_path_refs_allowed,
        test_secret_redacted_values_allowed,
        test_secret_placeholders_allowed,
        test_secret_git_sha_allowed,
        test_secret_normal_prose_allowed,
        test_secret_windows_path_allowed,
        test_secret_long_sentence_allowed,
        test_secret_block_shape,
        test_secret_assignment_short_value_allowed,
    ]:
        fn()
    print("ALL VALIDATE TESTS PASS")
