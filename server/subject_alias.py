"""
subject_alias.py -- shared subject-canonicalization helper (Feature 4).

Loads subject_aliases.json ({lowercase alias: canonical}) and maps a raw
subject string to (canonical, raw). Hot-loaded per call, cached for
_CACHE_TTL seconds so an operator can edit the table without restarting the
service. Fail-safe: any read/parse error or missing file yields an empty
table (subject passes through unchanged) -- never raises.

Shared between server.py (add_memory, real-time), supersede.py (subject
grouping at lineage-reconciliation time) and subject_backfill.py (LLM
subject extraction) so aliased subjects merge into one lineage thread
instead of forking. This module is the ONLY alias table -- do not add
per-script alias maps.

Lookup order: exact lowercased match first, then slugified match, so both
human-entered forms ("sirchmunk mcp") and LLM/slug forms ("sirchmunk-mcp")
resolve to the same canonical subject.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

log = logging.getLogger("memory-mcp.subject_alias")

ALIASES_PATH = os.environ.get(
    "SUBJECT_ALIASES_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "subject_aliases.json"),
)
_CACHE_TTL = 60  # seconds

_cache: dict = {"loaded_at": 0.0, "table": {}}


def slugify(text: str) -> str:
    """Lowercase, replace non-[a-z0-9]+ runs with '-', strip leading/trailing '-'."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _load(force: bool = False) -> dict:
    now = time.time()
    if not force and (now - _cache["loaded_at"]) < _CACHE_TTL:
        return _cache["table"]
    table: dict = {}
    try:
        with open(ALIASES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for k, v in raw.items():
                key = str(k).strip().lower()
                val = str(v).strip()
                table[key] = val
                # also index the slugified form so slug-normalized callers hit too
                table.setdefault(slugify(key), val)
        else:
            log.warning("subject_aliases.json is not a JSON object -- ignoring")
    except FileNotFoundError:
        pass  # no alias table configured -- pass-through behavior
    except Exception as exc:  # noqa: BLE001 -- never let a bad edit break add_memory
        log.warning("subject_aliases.json load failed: %s -- using empty table", exc)
        table = {}
    _cache.update(loaded_at=now, table=table)
    return table


def canonicalize(subject: str | None) -> tuple[str | None, str | None]:
    """Return (canonical_subject, raw_subject).

    raw_subject is None unless the alias table actually remapped the subject
    (i.e. no-op for subjects that were already canonical or unmapped) -- the
    caller decides whether to also set a raw_subject default for other reasons.
    """
    if not subject:
        return subject, None
    table = _load()
    key = subject.strip().lower()
    canonical = table.get(key) or table.get(slugify(key))
    if canonical and canonical.strip().lower() != key:
        return canonical, subject
    return subject, None
