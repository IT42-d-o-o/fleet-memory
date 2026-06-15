"""
validate.py — deterministic write-time guardrail for add_memory.

No LLM. A cheap structural detector decides *whether* a memory candidate looks
context-dependent (vague). If it does, the server returns a self_check response
instead of storing, and the writing agent (which has live session context)
either rewrites the memory or resubmits unchanged with self_checked=true.

The detector is a tripwire, not a judge: it only flags deixis in subject
position or dangling demonstratives — never every pronoun — so mid-sentence
pronouns with a local antecedent ("...the token is read-only; it returns 403")
stay clean. Occasional miss / false-positive is expected and handled by the
agent override; we log overrides so the wordlists can be tuned.
"""
import re

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
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@-]+")


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

    # 2. Dangling demonstrative — "this/that/these/those" + verb-ish, no noun.
    for i in range(len(toks) - 1):
        if toks[i] in _DEMONSTRATIVES and toks[i + 1] in VERBISH:
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
