"""
validate.py — deterministic write-time guardrails for add_memory.

Two independent wheels:

Wheel 1 (vagueness): a cheap structural detector flags context-dependent /
  dangling-reference memory candidates. If it fires the server returns a
  self_check response instead of storing; the writing agent can override with
  self_checked=true once it has verified the memory is self-contained.

Wheel 3 (secrets): a pattern + entropy detector blocks plaintext secret VALUES
  from ever entering fleet-memory. This wheel is NOT bypassable — no
  self_checked flag disarms it. False-negative risk (leaking a secret to every
  agent that reads memory) is treated as more serious than false-positive risk
  (blocking a legit write).

  The detector explicitly ALLOWS:
    - Vault path references ("secret/infra/gitea", "secret/apps/foo") — the
      whole convention stores paths, never values.
    - Redacted tokens ("sk-****", "b7Y5****", "<REDACTED>", "***").
    - Obvious placeholders ("your-api-key", "changeme", "xxxxxxxx", "example",
      "<token>", "TODO").
    - Git commit SHAs (40 lowercase hex chars) — common in memory, not secrets.
    - Long English prose, Windows/Unix paths, URLs without credentials, IPs,
      CT ids, version numbers.
"""
import math
import re
from collections import Counter

# Bare pronouns / demonstratives / relative-time words that signal an
# unresolved reference when they OPEN a memory.
PRON_LEAD = {
    "it", "this", "that", "these", "those", "they", "he", "she",
    "them", "him", "her", "there", "here", "then", "later",
    "former", "latter", "one", "ones",
}

# Verb/modal/aux tokens. A demonstrative immediately followed by one of these
# is dangling (no noun bound to it): "this should..." vs "this server...".
VERBISH = {
    "should", "is", "are", "was", "were", "will", "must", "can", "could",
    "would", "did", "does", "do", "has", "have", "had", "needs", "need",
    "may", "might", "shall",
}

_DEMONSTRATIVES = {"this", "that", "these", "those"}

# Conjunctions / clause-break words. A demonstrative is only "dangling" when it
# starts a clause (position 0 or right after one of these). After a noun it is a
# relative pronoun ("pronouns that have ...") and must NOT be flagged.
_CLAUSE_START = {
    "and", "but", "or", "so", "yet", "because", "while", "then",
    "thus", "therefore", "however", "although", "since",
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@-]+")

# ---------------------------------------------------------------------------
# Wheel 3 — SECRET DETECTOR
# ---------------------------------------------------------------------------

# 1. Private key PEM blocks.
_RE_PRIVATE_KEY = re.compile(
    r"-----BEGIN\s+(?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
)

# 2. AWS Access Key ID.
_RE_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# 3. GitHub tokens (PAT, OAuth, server-to-server, user-to-server, refresh).
_RE_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")

# 4a. Vault service tokens (v2 hvs.*).
_RE_VAULT_TOKEN_V2 = re.compile(r"\bhvs\.[A-Za-z0-9]{20,}\b")

# 4b. Vault legacy tokens (s.XXXX — 26+ chars total).
_RE_VAULT_TOKEN_V1 = re.compile(r"\bs\.[A-Za-z0-9]{24,}\b")

# 5. Slack tokens.
_RE_SLACK = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")

# 6. Generic Bearer JWT (three base64url segments).
_RE_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)

# 7. Inline assignment of a secret-looking value.
#    Matches:  password = "S3cr3t!23"
#              api_key: abcdef012345678
#              token=ghp_something
#    The value is captured in group "val".  Length and placeholder checks
#    are applied in code below.
_SECRET_KEY_NAMES = (
    r"password|passwd|pwd|secret|api[_-]?key|apikey"
    r"|token|access[_-]?key|private[_-]?key"
)
_RE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:" + _SECRET_KEY_NAMES + r")\s*[:=]\s*['\"]?(?P<val>\S+?)['\"]?(?:\s|$|;|,)"
)

# 8. High-entropy standalone token catch-all.
#    A run of base64/hex chars of length >= 32 with Shannon entropy > 4.0 bits/char.
_RE_CANDIDATE_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")

# Git commit SHA — 40 lowercase hex chars.  Explicitly excluded from the
# catch-all: commit hashes appear constantly in memory and are not secrets.
_RE_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")

# Vault path reference — explicitly allowed: secret/<word-chars and -/>
_RE_VAULT_PATH = re.compile(r"\bsecret/[\w/-]+\b")

# Redacted markers — skip these, the value is already sanitised.
_RE_REDACTED = re.compile(
    r"\b[A-Za-z0-9_-]{1,8}\*{4,}\b"        # b7Y5**** / sk-****
    r"|<REDACTED>"
    r"|^\*+$"                                # pure stars
    r"|\*{3,}"                               # inline *** / ****
    r"|<\s*(?:token|secret|key|password)\s*>",  # <token>, <key>
    re.IGNORECASE,
)

# Obvious placeholders — case-insensitive substring match.
_PLACEHOLDERS = {
    "your-api-key", "your_api_key", "yourapikey",
    "changeme", "change-me", "change_me",
    "xxxxxxxx", "xxxx", "1234567890",
    "example", "placeholder", "insert-here",
    "<token>", "todo", "n/a", "none",
    "your-token", "your_token",
}

# Minimum length for assignment values to be flagged (short values like "true",
# "false", "1", "null" are obviously not secrets).
_MIN_SECRET_VALUE_LEN = 8

# Shannon entropy threshold for the catch-all (bits per character).
# English text averages ~4.0; base64 random data averages ~5.5.  4.0 is the
# threshold: values ABOVE this are flagged.  Tuned to avoid long English
# sentences (which stay well below 4.0 due to repeated chars and spaces).
_ENTROPY_THRESHOLD = 4.0


def _shannon_entropy(s: str) -> float:
    """Return Shannon entropy in bits per character for string s."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _strip_vault_paths(text: str) -> str:
    """Remove Vault path references before pattern matching to avoid false positives."""
    return _RE_VAULT_PATH.sub("VAULT_PATH_REF", text)


def _is_placeholder(val: str) -> bool:
    """Return True if val is an obvious placeholder or redacted marker."""
    v = val.strip().lower()
    if v in _PLACEHOLDERS:
        return True
    if _RE_REDACTED.search(val):
        return True
    # If the value is all one repeated character (e.g. "xxxxxxxx", "11111111")
    if len(set(v)) == 1 and len(v) >= 4:
        return True
    return False


def detect_secrets(content: str) -> list[str]:
    """Return a list of human-readable secret-detection flags.

    Empty list = no secrets detected.  Each flag names the detector that fired
    and redacts the matched value to first-4-chars + ****.  The original secret
    VALUE is never echoed.

    Vault PATH references (secret/infra/...) are explicitly allowed and stripped
    before any matching so they cannot trigger the assignment or entropy detectors.
    """
    flags: list[str] = []
    text = content or ""
    if not text.strip():
        return flags

    # Strip Vault path refs so they do not trigger downstream patterns.
    safe = _strip_vault_paths(text)

    # --- 1. Private key PEM block ---
    if _RE_PRIVATE_KEY.search(safe):
        flags.append("private-key-block: PEM private key block detected")

    # --- 2. AWS Access Key ID ---
    m = _RE_AWS_KEY.search(safe)
    if m:
        v = m.group()
        flags.append(f"aws-key-id: AWS access key id detected ({v[:4]}****)")

    # --- 3. GitHub token ---
    m = _RE_GITHUB_TOKEN.search(safe)
    if m:
        v = m.group()
        flags.append(f"github-token: GitHub token detected ({v[:4]}****)")

    # --- 4. Vault tokens (but NOT Vault paths — those are already stripped) ---
    m = _RE_VAULT_TOKEN_V2.search(safe)
    if m:
        v = m.group()
        flags.append(f"vault-token-v2: Vault service token detected ({v[:4]}****)")

    m = _RE_VAULT_TOKEN_V1.search(safe)
    if m:
        v = m.group()
        flags.append(f"vault-token-v1: Vault legacy token detected ({v[:4]}****)")

    # --- 5. Slack token ---
    m = _RE_SLACK.search(safe)
    if m:
        v = m.group()
        flags.append(f"slack-token: Slack token detected ({v[:4]}****)")

    # --- 6. JWT ---
    m = _RE_JWT.search(safe)
    if m:
        v = m.group()
        flags.append(f"jwt: JWT / bearer token detected ({v[:4]}****)")

    # --- 7. Inline assignment ---
    for m in _RE_ASSIGNMENT.finditer(safe):
        val = m.group("val")
        if len(val) < _MIN_SECRET_VALUE_LEN:
            continue
        if _is_placeholder(val):
            continue
        redacted = val[:4] + "****"
        key_part = m.group().split("=")[0].split(":")[0].strip()
        flags.append(f"secret-assignment: key={key_part!r} value={redacted}")
        break  # one flag per content is enough; don't enumerate all assignments

    # --- 8. High-entropy standalone token (catch-all) ---
    # Skip if already flagged — avoids double-reporting the same span.
    if not flags:
        for m in _RE_CANDIDATE_TOKEN.finditer(safe):
            token = m.group()
            # Exclude 40-char lowercase hex (git SHA).
            if _RE_GIT_SHA.match(token):
                continue
            # Exclude short or placeholder values.
            if _is_placeholder(token):
                continue
            ent = _shannon_entropy(token)
            if ent > _ENTROPY_THRESHOLD:
                redacted = token[:4] + "****"
                flags.append(
                    f"high-entropy-token: standalone token entropy={ent:.2f} ({redacted})"
                )
                break  # one flag is enough

    return flags


def build_secret_block(flags: list[str]) -> dict:
    """Structured response returned when the secret detector fires.

    Unlike the vagueness self_check, this block is NOT bypassable — the caller
    must never store and must surface the rejection to the writing agent.
    """
    return {
        "stored": False,
        "error": "MEMORY_CONTAINS_SECRET",
        "flags": flags,
        "action": (
            "The memory content appears to contain a plaintext secret. "
            "Store only the Vault path (e.g. secret/infra/gitea), NOT the value. "
            "This rejection CANNOT be bypassed with self_checked=true."
        ),
    }


def detect(content: str, subject: str | None = None) -> list[str]:
    """Return a list of human-readable ambiguity flags. Empty list = clean."""
    flags: list[str] = []
    text = content or ""
    toks = _TOKEN_RE.findall(text.lower())
    if not toks:
        return ["content is empty"]

    # 1. Leading deixis — opens with a bare pronoun / relative-time word.
    #    Demonstratives are excluded here: a leading "this/that" may be bound to
    #    a noun ("this server ...") and is judged only by the dangling rule below.
    if toks[0] in PRON_LEAD and toks[0] not in _DEMONSTRATIVES:
        flags.append(f"opens with unresolved reference '{toks[0]}'")

    # 2. Dangling demonstrative — "this/that/these/those" + verb-ish, no noun,
    #    but only when it starts a clause (else it's a relative pronoun after a
    #    noun, e.g. "pronouns that have ...", which is fine).
    for i in range(len(toks) - 1):
        if toks[i] in _DEMONSTRATIVES and toks[i + 1] in VERBISH:
            at_clause_start = i == 0 or toks[i - 1] in _CLAUSE_START
            if at_clause_start:
                flags.append(f"dangling demonstrative '{toks[i]} {toks[i + 1]}'")
                break

    # 3/4. Subject rules (only when a subject is supplied).
    if subject:
        s = subject.strip().lower()
        if s in PRON_LEAD:
            flags.append("subject is a pronoun, not explicit")
        elif s and s not in text.lower():
            flags.append("content does not contain the subject")

    return flags


def build_self_check(flags: list[str]) -> dict:
    """Structured response returned when a candidate trips the detector."""
    return {
        "stored": False,
        "error": "MEMORY_NEEDS_SELF_CHECK",
        "flags": flags,
        "questions": [
            "Is the subject explicit and present in the content?",
            "Does this memory stand alone with no session context?",
        ],
        "action": (
            "Rewrite as one self-contained atomic proposition, OR resubmit "
            "unchanged with self_checked=true if it is already correct."
        ),
    }
