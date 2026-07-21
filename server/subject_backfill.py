#!/usr/bin/env python3
"""
subject_backfill.py — Phase 1 memory-lineage: backfill metadata.subject on fleet-memory points.

Scrolls all Qdrant points that lack a canonical subject, calls gpt-4o-mini to extract
the single concrete entity the fact is about, slugifies + aliases the result, then
writes it back into metadata.subject in-place via set_payload (no re-embedding).

Run on CT356:
  python3 /opt/memory-mcp/subject_backfill.py [--dry-run] [--sample] [--limit N]

Flags:
  --sample    Print 5 raw payloads and exit (confirm payload shape before a real run).
  --dry-run   Compute and log every planned change but call NO set_payload.
  --limit N   Stop after processing N untagged points (useful for partial test runs).

Vault path: secret/Mem0 field openai_api
Vault token: /etc/memory-mcp/vault-token
"""

import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

import argparse
from collections import defaultdict


# ---------------------------------------------------------------------------
# Vault bootstrap — identical pattern to dedup.py
# ---------------------------------------------------------------------------

def vault_get(path, field):
    r = subprocess.run(
        ["vault", "kv", "get", f"-field={field}", path],
        capture_output=True, text=True,
        env={**os.environ, "VAULT_ADDR": "http://10.10.10.107:8200",
             "VAULT_TOKEN": open("/etc/memory-mcp/vault-token").read().strip()}
    )
    if r.returncode != 0:
        sys.exit(f"Vault error: {r.stderr}")
    return r.stdout.strip()


os.environ["OPENAI_API_KEY"] = vault_get("secret/Mem0", "openai_api")

from qdrant_client import QdrantClient  # noqa: E402  (after sys.path insert)
import openai  # noqa: E402
import httpx   # noqa: E402


# ---------------------------------------------------------------------------
# Logging — structured, same style as reclassify.py
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("subject_backfill")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLLECTION = "local_ai_cross_agent_memory"
QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
PUSHGATEWAY = "http://192.168.50.223:9091"
LLM_MODEL = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Subject canonicalization — SHARED table, no local alias map.
# subject_alias.py loads subject_aliases.json (same dir); add new aliases THERE.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subject_alias  # noqa: E402

slugify = subject_alias.slugify


def canonical_subject(raw: str) -> str:
    """Slugify raw LLM output then map to canonical alias if known."""
    slug = slugify(raw)
    canonical, _ = subject_alias.canonicalize(slug)
    return canonical or slug


EXTRACTION_PROMPT = (
    'Given this memory fact, return JSON {"subject": "<the single explicit entity the fact '
    "is about — a concrete name/host/service/project/person, e.g. CT356, Observus, InfraAtlas, "
    'Tomislav>"}. The subject MUST be a concrete named noun present in or directly named by the '
    "fact, never a pronoun. "
    "Resolve possessives/genitives to the underlying named entity, never the possessive phrase: "
    "for \"User's login page\" the subject is the project or service the login page belongs to, "
    "NOT \"User's login page\"; for \"Tomislav's repository\" return the repository or project "
    "name if named, else \"Tomislav\". Prefer a known entity named elsewhere in the fact (a CT id, "
    "project slug, service, host, or person) over a generic descriptive phrase. Never return a "
    "file path or a multi-word descriptive phrase as the subject when a named entity is present. "
    "Fact: "
)


def extract_subject(client: openai.OpenAI, text: str) -> tuple[str | None, int, int]:
    """
    Call gpt-4o-mini to extract the subject from a memory fact.
    Returns (raw_subject_or_None, prompt_tokens, completion_tokens).
    On any API/parse error, logs a warning and returns (None, 0, 0).
    """
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT + text}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw_json = resp.choices[0].message.content or ""
        data = json.loads(raw_json)
        subject = data.get("subject", "").strip()
        if not subject:
            log.warning("LLM returned empty subject for: %s", text[:80])
            return None, 0, 0
        p_tok = resp.usage.prompt_tokens if resp.usage else 0
        c_tok = resp.usage.completion_tokens if resp.usage else 0
        return subject, p_tok, c_tok
    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for fact '%s': %s", text[:60], exc)
        return None, 0, 0
    except Exception as exc:
        log.warning("OpenAI call failed for fact '%s': %s", text[:60], exc)
        return None, 0, 0


def push_telemetry(tokens_in: int, tokens_out: int) -> None:
    """Push LLM token totals to Prometheus Pushgateway. Best-effort — never raises."""
    try:
        body = (
            f"# HELP llm_tokens_total Total LLM tokens used\n"
            f"# TYPE llm_tokens_total counter\n"
            f'llm_tokens_total{{model="{LLM_MODEL}",direction="in"}} {tokens_in}\n'
            f'llm_tokens_total{{model="{LLM_MODEL}",direction="out"}} {tokens_out}\n'
        )
        url = f"{PUSHGATEWAY}/metrics/job/llm_usage/app/fleet-memory-backfill"
        httpx.post(url, content=body, headers={"Content-Type": "text/plain"}, timeout=10)
        log.info("Telemetry pushed: in=%d out=%d", tokens_in, tokens_out)
    except Exception as exc:
        log.warning("Pushgateway push failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill metadata.subject on fleet-memory Qdrant points."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log planned changes without writing to Qdrant.")
    parser.add_argument("--sample", action="store_true",
                        help="Print 5 raw payloads and exit (verify payload shape).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after tagging N untagged points (0 = unlimited).")
    args = parser.parse_args()

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)

    # --sample: quick sanity-check for operators before committing to a full run
    if args.sample:
        result, _ = client.scroll(
            collection_name=COLLECTION,
            limit=5,
            with_payload=True,
            with_vectors=False,
        )
        for p in result:
            print(p.id, p.payload)
        return

    # ---- Scroll all points -------------------------------------------------
    all_points = []
    offset = None
    while True:
        res, offset = client.scroll(
            COLLECTION, limit=500, offset=offset,
            with_payload=True, with_vectors=False,
        )
        all_points.extend(res)
        if offset is None:
            break

    log.info("Total points fetched: %d", len(all_points))

    oai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    total = 0
    already_tagged = 0
    newly_tagged = 0
    skipped = 0
    tokens_in_total = 0
    tokens_out_total = 0
    subject_counts: dict[str, int] = defaultdict(int)

    for point in all_points:
        total += 1
        payload = point.payload or {}

        # Read memory text — same fallback chain as dedup.py / reclassify.py
        text = payload.get("data") or payload.get("memory") or ""

        # Check existing subject from BOTH possible locations
        existing_meta = payload.get("metadata") or {}
        existing_subject = (
            existing_meta.get("subject")
            or payload.get("subject")
            or ""
        )
        if existing_subject:
            already_tagged += 1
            log.debug("SKIP (already tagged) %s subject=%s", point.id, existing_subject)
            continue

        if not text:
            skipped += 1
            log.warning("SKIP (no text) %s", point.id)
            continue

        # Extract subject via LLM
        raw_subject, p_tok, c_tok = extract_subject(oai_client, text)
        tokens_in_total += p_tok
        tokens_out_total += c_tok

        if raw_subject is None:
            skipped += 1
            continue

        canonical = canonical_subject(raw_subject)
        subject_counts[canonical] += 1

        log.info("%-20s ← %-20s | %s", canonical, raw_subject, text[:80])

        if not args.dry_run:
            # mem0 stores metadata as FLAT top-level Qdrant payload keys (category,
            # source, ...) and reassembles them into a nested dict only in search
            # results. So write subject as a flat top-level key — set_payload merges,
            # leaving category/source untouched.
            client.set_payload(
                collection_name=COLLECTION,
                payload={"subject": canonical, "raw_subject": raw_subject},
                points=[point.id],
            )

        newly_tagged += 1

        if args.limit and newly_tagged >= args.limit:
            log.info("Reached --limit %d, stopping early.", args.limit)
            break

    # ---- Telemetry ---------------------------------------------------------
    push_telemetry(tokens_in_total, tokens_out_total)

    # ---- Summary -----------------------------------------------------------
    log.info(
        "Done: total=%d  already_tagged=%d  newly_tagged=%d  skipped(errors)=%d",
        total, already_tagged, newly_tagged, skipped,
    )
    log.info("Top subjects assigned:")
    for subj, count in sorted(subject_counts.items(), key=lambda x: -x[1]):
        log.info("  %-30s %d", subj, count)

    if args.dry_run:
        log.info("DRY RUN — no writes made")


if __name__ == "__main__":
    main()
