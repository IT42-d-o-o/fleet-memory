#!/usr/bin/env python3
"""
supersede.py — detect and mark supersession chains in fleet-memory (CT356).

Groups Qdrant points by (user_id, subject), calls gpt-4o-mini once per group
to cluster facts into CLAIM THREADS (same attribute/state of the subject),
then writes lineage metadata in-place via set_payload — NON-DESTRUCTIVE, no
deletes, no re-embedding.

Fields written into payload["metadata"] for every processed point:
  current        bool   True if newest in its thread or a singleton
  superseded_by  str|null  id of the thread head, or null if current
  supersedes     list[str] ids made stale by this fact (populated on head only)
  valid_from     str    copy of created_at

Idempotent: re-running recomputes from scratch; prior lineage fields are
overwritten. Requires subject_backfill.py to have run first — points with
no metadata.subject are skipped.

Run on CT356:
  python3 /opt/memory-mcp/supersede.py [--dry-run] [--subject SLUG] [--min-group N]
  python3 /opt/memory-mcp/supersede.py --since-state          # incremental fast-path
  python3 /opt/memory-mcp/supersede.py --since 2026-06-15T00:00:00Z

Incremental mode (--since / --since-state):
  Only subject groups that contain at least one point with created_at newer than
  the given timestamp are reprocessed. Groups with no recent point are left
  completely untouched -- no LLM call, no payload writes, prior lineage preserved.

  Correctness note: when ANY member of a group is newer than since_ts, the WHOLE
  group is recomputed. This ensures that a new fact which supersedes an old fact
  from years ago is properly linked -- the old members get re-judged together with
  the new member in one LLM call.

Concurrency lock (--lock-file, default /opt/memory-mcp/.supersede.lock):
  Atomic lockfile prevents two overlapping reconciles (e.g. session-close fast-path
  and the nightly timer). If the lock is already held, the process exits 0 without
  doing any work -- the running instance covers the window. Bypass with --no-lock
  for manual / dry-run invocations.

Vault path: secret/Mem0 field openai_api
Vault token: /etc/memory-mcp/vault-token
"""
import os
import sys
import json
import logging
import argparse
import subprocess
from collections import defaultdict
from datetime import datetime, timezone

import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

# Shared with server.py add_memory (Feature 4, 2026-07-12) so aliased subjects
# (e.g. "memory-mcp" / "mem0" / "ct356" -> "fleet-memory") merge into one
# lineage thread instead of forking across their raw and canonical forms.
import subject_alias  # noqa: E402 — must come after sys.path insert; script dir is on sys.path[0]

# ---------------------------------------------------------------------------
# Vault helper — verbatim from dedup.py
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

from qdrant_client import QdrantClient  # noqa: E402 — must come after sys.path insert

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("supersede")

COLLECTION = "local_ai_cross_agent_memory"
PUSHGATEWAY = "http://192.168.50.223:9091"
OPENAI_MODEL = "gpt-4o-mini"
# Subject groups larger than this are skipped (all marked current) rather than
# sent to the judge — too big for one call and almost always a generic catch-all
# subject (e.g. 'user') with no real supersession.
MAX_LLM_GROUP = 80

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Detect and mark supersession chains in fleet-memory."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log planned chains but write nothing to Qdrant.")
    parser.add_argument("--subject", metavar="SLUG", default=None,
                        help="Restrict to one canonical subject slug (e.g. infraatlas).")
    parser.add_argument("--min-group", metavar="N", type=int, default=2,
                        help="Only process subject groups with at least N facts (default 2).")

    # --- Incremental / since mode -------------------------------------------
    since_grp = parser.add_mutually_exclusive_group()
    since_grp.add_argument(
        "--since", metavar="TIMESTAMP",
        help=(
            "ISO8601 UTC timestamp. Only process subject groups that contain at "
            "least one point with created_at > TIMESTAMP. Groups with no recent "
            "point are left completely untouched."
        ),
    )
    since_grp.add_argument(
        "--since-state", action="store_true",
        help=(
            "Read the last-run timestamp from --state-file. If the file is "
            "missing/empty, treat as full run (first run). On a successful "
            "non-dry-run completion, write the run's start-time to the state "
            "file for the next incremental run."
        ),
    )
    parser.add_argument(
        "--state-file", metavar="PATH",
        default="/opt/memory-mcp/.supersede_last_run",
        help="State file path for --since-state (default: /opt/memory-mcp/.supersede_last_run).",
    )

    # --- Concurrency lock ---------------------------------------------------
    parser.add_argument(
        "--lock-file", metavar="PATH",
        default="/opt/memory-mcp/.supersede.lock",
        help="Atomic lockfile path (default: /opt/memory-mcp/.supersede.lock).",
    )
    parser.add_argument(
        "--no-lock", action="store_true",
        help="Bypass the concurrency lock (for manual or dry-run invocations).",
    )

    return parser.parse_args()

# ---------------------------------------------------------------------------
# Scroll all points — verbatim pattern from dedup.py
# ---------------------------------------------------------------------------

def scroll_all(client: QdrantClient) -> list[dict]:
    all_points = []
    offset = None
    while True:
        res, offset = client.scroll(
            COLLECTION,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(res)
        if offset is None:
            break
    return all_points


def extract_records(all_points) -> list[dict]:
    records = []
    for pt in all_points:
        p = pt.payload or {}
        # mem0 keeps metadata FLAT at the top level of the Qdrant payload — subject
        # was written there by subject_backfill.py. Read it flat (with a legacy
        # nested fallback for safety).
        meta = p.get("metadata") or {}
        # Alias-canonicalize defensively: most points already carry the canonical
        # form (server.py writes it at write time), but older points predating
        # the alias table may still hold a raw form directly -- map it here too
        # so grouping-by-subject merges them into the same lineage thread.
        raw_subj = p.get("subject") or meta.get("subject")
        canonical_subj, _ = subject_alias.canonicalize(raw_subj)
        records.append({
            "id": str(pt.id),
            "content": p.get("data", "") or p.get("memory", "") or p.get("text", ""),
            "user_id": p.get("user_id", ""),
            "created_at": p.get("created_at", "") or meta.get("created_at", ""),
            "subject": canonical_subj,
            "_payload": p,
        })
    return records

# ---------------------------------------------------------------------------
# OpenAI supersession judge
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a supersession judge for a knowledge base.

You receive a NUMBERED list of facts that all concern the SAME SUBJECT.
Group them into CLAIM THREADS.

A CLAIM THREAD is two or more facts that assert the SAME SPECIFIC ATTRIBUTE of
the subject, where a NEWER fact REPLACES an OLDER one because they cannot both
be true at the same time (the value changed, moved, was corrected, or restated).

THE TEST for putting two facts in one thread — ask: "Could both facts be true
AT THE SAME TIME?"
- YES (they describe DIFFERENT attributes, or one adds detail, or both still
  hold) -> DIFFERENT threads. They do NOT supersede each other.
- NO (they give conflicting/replacing values for the ONE same attribute)
  -> SAME thread; the newest by created date is current.

Default to SEPARATE threads whenever unsure. Over-merging destroys valid facts;
keeping facts apart is the safe error. Different attributes are NEVER the same
thread even for the same subject — e.g. deployment port vs runtime framework vs
container id vs database path vs license key vs feature list are all distinct.

Return ONLY valid JSON — no prose, no markdown fence — in this exact shape:
{"threads": [[n, n, ...], ...]}
Each inner array lists the NUMBERS (the integer shown before each fact) of the
facts in one thread. A fact that shares a thread with no other is its own
single-member array. Every input number must appear in exactly one thread.

EXAMPLE
Input facts about subject "atlas":
1. created='2026-01-01' content='Atlas runs on port 5093'
2. created='2026-02-01' content='Atlas runs on ASP.NET Core 9'
3. created='2026-01-10' content='Atlas database is at /opt/atlas/app.db'
4. created='2026-03-01' content='Atlas database moved to /opt/atlas-data/app.db'
5. created='2026-02-15' content='Atlas Demo instance is CT359'
Correct output:
{"threads": [[1], [2], [3, 4], [5]]}
Reason: port, framework and demo-instance are distinct attributes that can all be
true simultaneously -> separate threads. Facts 3 and 4 are the SAME attribute
(database location) with a changed value -> one thread; 4 is newer = current.
"""

def call_llm(facts: list[dict], api_key: str) -> tuple[list[list[str]], int, int]:
    """
    Call gpt-4o-mini to cluster facts into claim threads.

    Returns (threads, prompt_tokens, completion_tokens).
    threads is a list of id-lists. Raises on unrecoverable error.
    """
    import httpx  # available in venv

    numbered = "\n".join(
        f"{i+1}. created={f['created_at']!r} content={f['content']!r}"
        for i, f in enumerate(facts)
    )
    user_msg = f"Subject group — {len(facts)} facts:\n\n{numbered}"

    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    }

    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()

    usage = body.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    raw_text = body["choices"][0]["message"]["content"]
    parsed = json.loads(raw_text)
    threads_raw = parsed.get("threads", [])

    # The judge returns 1-based item NUMBERS, not ids — map them back to real
    # point ids here. This is deterministic and immune to the model echoing
    # indices, hallucinating UUIDs, or reformatting ids.
    if not isinstance(threads_raw, list):
        raise ValueError(f"Expected list of threads, got: {type(threads_raw)}")
    n_facts = len(facts)
    threads: list[list[str]] = []
    for t in threads_raw:
        if not isinstance(t, list):
            raise ValueError(f"Thread member is not a list: {t!r}")
        ids: list[str] = []
        for x in t:
            try:
                n = int(x)
            except (ValueError, TypeError):
                log.warning("judge returned non-integer thread member %r — skipping", x)
                continue
            if 1 <= n <= n_facts:
                ids.append(facts[n - 1]["id"])
            else:
                log.warning("judge returned out-of-range index %d (facts=%d) — skipping", n, n_facts)
        if ids:
            threads.append(ids)

    return threads, prompt_tokens, completion_tokens

# ---------------------------------------------------------------------------
# LLM telemetry — best-effort Pushgateway push
# ---------------------------------------------------------------------------

def push_telemetry(total_in: int, total_out: int) -> None:
    try:
        import httpx
        body = (
            f'# TYPE llm_tokens_total counter\n'
            f'llm_tokens_total{{model="{OPENAI_MODEL}",direction="in"}} {total_in}\n'
            f'llm_tokens_total{{model="{OPENAI_MODEL}",direction="out"}} {total_out}\n'
        )
        url = f"{PUSHGATEWAY}/metrics/job/llm_usage/app/fleet-memory-supersede"
        httpx.post(url, content=body.encode(), timeout=10)
        log.info("Telemetry pushed: in=%d out=%d", total_in, total_out)
    except Exception as exc:
        log.warning("Pushgateway push failed (non-fatal): %s", exc)

# ---------------------------------------------------------------------------
# Core supersession logic
# ---------------------------------------------------------------------------

def sort_thread_by_created_at(thread_ids: list[str], id_to_record: dict) -> list[str]:
    """
    Order a thread oldest→newest by created_at.
    Records with empty created_at sort first (treat as oldest).
    """
    return sorted(
        thread_ids,
        key=lambda rid: id_to_record[rid]["created_at"] if rid in id_to_record else "",
    )


def compute_lineage_for_group(
    records: list[dict],
    threads: list[list[str]],
    id_to_record: dict,
) -> dict[str, dict]:
    """
    Given the LLM thread assignment and time-ordered members, compute the four
    lineage fields for every record id in this group.

    Returns {id: {current, superseded_by, supersedes, valid_from}}.
    """
    lineage: dict[str, dict] = {}

    # Collect all ids that appeared in LLM output so we can catch any missed ones
    seen_ids: set[str] = set()

    for thread in threads:
        if not thread:
            continue
        # Sort oldest→newest by created_at (authoritative; do not trust LLM ordering)
        ordered = sort_thread_by_created_at(thread, id_to_record)
        seen_ids.update(ordered)

        head_id = ordered[-1]  # newest = current

        for i, rid in enumerate(ordered):
            rec = id_to_record.get(rid)
            valid_from = rec["created_at"] if rec else ""
            if rid == head_id:
                lineage[rid] = {
                    "current": True,
                    "superseded_by": None,
                    "supersedes": ordered[:-1],  # all older members
                    "valid_from": valid_from,
                }
            else:
                lineage[rid] = {
                    "current": False,
                    "superseded_by": head_id,
                    "supersedes": [],
                    "valid_from": valid_from,
                }

    # Any ids the LLM omitted (defensive) — mark as singleton/current
    for rec in records:
        rid = rec["id"]
        if rid not in seen_ids:
            log.warning("LLM omitted id %s from threads — marking as singleton", rid[:8])
            lineage[rid] = {
                "current": True,
                "superseded_by": None,
                "supersedes": [],
                "valid_from": rec["created_at"],
            }

    return lineage


def singleton_lineage(rec: dict) -> dict:
    """Uniform lineage for singletons (no LLM call needed)."""
    return {
        "current": True,
        "superseded_by": None,
        "supersedes": [],
        "valid_from": rec["created_at"],
    }

# ---------------------------------------------------------------------------
# Write lineage fields back to Qdrant
# ---------------------------------------------------------------------------

def write_lineage(
    client: QdrantClient,
    point_id: str,
    existing_payload: dict,
    lineage_fields: dict,
    dry_run: bool,
) -> None:
    """
    Write lineage_fields as FLAT top-level payload keys via set_payload.
    mem0 stores metadata flat and reassembles it into a nested dict only in search
    results, so current/superseded_by/supersedes/valid_from go top-level. set_payload
    merges, so unrelated keys (subject, category, source) are preserved.
    """
    if not dry_run:
        client.set_payload(
            collection_name=COLLECTION,
            payload=dict(lineage_fields),
            points=[point_id],
        )

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_thread(subject: str, thread_ordered: list[str], id_to_record: dict) -> None:
    """Log a single claim thread in a readable chain format."""
    parts = []
    for i, rid in enumerate(thread_ordered):
        rec = id_to_record.get(rid, {})
        snippet = (rec.get("content") or "")[:60].replace("\n", " ")
        marker = "[current]" if i == len(thread_ordered) - 1 else ""
        parts.append(f"<{rid[:8]} {repr(snippet)}> {marker}".strip())
    chain = " -> ".join(parts)
    log.info("%s thread: %s", subject, chain)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: str) -> int:
    """
    Atomically create the lockfile. Returns a file descriptor on success.
    Raises FileExistsError if already locked.
    """
    return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def _release_lock(fd: int, lock_path: str) -> None:
    """Close and remove the lockfile."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def _read_state_file(path: str) -> str | None:
    """Return the ISO8601 timestamp stored in the state file, or None if absent/empty."""
    try:
        with open(path) as fh:
            ts = fh.read().strip()
        return ts if ts else None
    except FileNotFoundError:
        return None


def _write_state_file(path: str, ts: str) -> None:
    """Persist the ISO8601 run-start timestamp to the state file."""
    with open(path, "w") as fh:
        fh.write(ts + "\n")


def main() -> None:
    args = parse_args()
    dry_run: bool = args.dry_run
    subject_filter: str | None = args.subject
    min_group: int = args.min_group

    # Capture run-start time at the very beginning (used for state-file update).
    run_start_ts: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Resolve since_ts ---------------------------------------------------
    since_ts: str | None = None  # None = full run
    if args.since:
        since_ts = args.since
        log.info("Incremental mode: --since %s", since_ts)
    elif args.since_state:
        since_ts = _read_state_file(args.state_file)
        if since_ts:
            log.info("Incremental mode: --since-state resolved to %s (from %s)", since_ts, args.state_file)
        else:
            log.info("--since-state: state file absent or empty — running full pass")

    # --- Concurrency lock ---------------------------------------------------
    lock_fd: int | None = None
    if not args.no_lock:
        try:
            lock_fd = _acquire_lock(args.lock_file)
            log.info("Lock acquired: %s", args.lock_file)
        except FileExistsError:
            log.info("Another reconcile is running -- exiting (lock held at %s)", args.lock_file)
            sys.exit(0)

    run_succeeded = False
    try:
        _run(
            dry_run=dry_run,
            subject_filter=subject_filter,
            min_group=min_group,
            since_ts=since_ts,
            run_start_ts=run_start_ts,
            state_file=args.state_file if args.since_state else None,
        )
        run_succeeded = True
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, args.lock_file)
            log.info("Lock released: %s", args.lock_file)


def _run(
    dry_run: bool,
    subject_filter: str | None,
    min_group: int,
    since_ts: str | None,
    run_start_ts: str,
    state_file: str | None,
) -> None:
    """
    Core reconcile logic, extracted so the lock/state wrapper in main() stays clean.
    """
    api_key = os.environ["OPENAI_API_KEY"]

    client = QdrantClient(host="127.0.0.1", port=6333, check_compatibility=False)

    # --- Scroll all points --------------------------------------------------
    log.info("Scrolling all points from %s ...", COLLECTION)
    all_points = scroll_all(client)
    log.info("Total points: %d", len(all_points))

    records = extract_records(all_points)

    # --- Filter / skip no-subject records -----------------------------------
    valid_records: list[dict] = []
    skipped_no_subject = 0
    for rec in records:
        if not rec["subject"]:
            skipped_no_subject += 1
            continue
        if subject_filter and rec["subject"] != subject_filter:
            continue
        valid_records.append(rec)

    if skipped_no_subject:
        log.warning(
            "Skipped %d points with no metadata.subject -- run subject_backfill.py first.",
            skipped_no_subject,
        )

    log.info("Valid (have subject) records for processing: %d", len(valid_records))

    id_to_record: dict[str, dict] = {r["id"]: r for r in valid_records}

    # --- Group by subject ACROSS namespaces ---------------------------------
    # Lineage is a property of the entity, not the namespace. The same fact often
    # lives in both global `fleet` and `fleet:{project}` (e.g. a DB path stated
    # globally and restated under the project) -- grouping by subject only lets a
    # newer value in one namespace supersede an older value in the other. Server
    # resolution + project search already span both namespaces, so cross-ns heads
    # resolve fine.
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in valid_records:
        groups[rec["subject"]].append(rec)

    # --- Process each group -------------------------------------------------
    total_prompt_tokens = 0
    total_completion_tokens = 0
    groups_processed = 0
    groups_skipped_stale = 0
    total_threads_found = 0
    total_superseded = 0

    # Collect all lineage decisions: {point_id: lineage_fields}
    all_lineage: dict[str, dict] = {}
    # Map point_id -> payload for the write step
    id_to_payload: dict[str, dict] = {
        str(pt.id): (pt.payload or {}) for pt in all_points
    }

    for subject, group in sorted(groups.items()):
        # --- Incremental fast-path: skip groups with no recent points -------
        # When ANY member is newer than since_ts we reprocess the WHOLE group
        # so that old members get re-judged alongside the new one. Groups with
        # no member newer than since_ts are left completely untouched -- their
        # existing lineage payload is preserved as-is.
        if since_ts is not None:
            has_recent = any(rec["created_at"] > since_ts for rec in group)
            if not has_recent:
                groups_skipped_stale += 1
                # Do NOT write any lineage for this group; leave prior values intact.
                continue

        if len(group) < min_group:
            # Singletons and below-threshold groups get uniform current=True
            for rec in group:
                all_lineage[rec["id"]] = singleton_lineage(rec)
            continue

        if len(group) > MAX_LLM_GROUP:
            # Oversized buckets (e.g. the generic 'user' subject) are too large to
            # thread meaningfully in one call and almost never hold real
            # supersession. Mark all current and skip -- do not silently truncate.
            log.warning(
                "subject=%r has %d facts > MAX_LLM_GROUP=%d -- marking all current, skipping judge",
                subject, len(group), MAX_LLM_GROUP,
            )
            for rec in group:
                all_lineage[rec["id"]] = singleton_lineage(rec)
            continue

        log.info("Processing group subject=%r (%d facts)", subject, len(group))

        # --- Call LLM -------------------------------------------------------
        try:
            threads, pt, ct = call_llm(group, api_key)
            total_prompt_tokens += pt
            total_completion_tokens += ct
        except Exception as exc:
            log.warning(
                "LLM call failed for subject=%r -- skipping group. Error: %s",
                subject, exc,
            )
            # Leave group untouched (no lineage fields written for it this run)
            continue

        # Validate coverage: all group ids must appear somewhere in threads
        group_ids = {r["id"] for r in group}
        thread_ids_flat = {tid for t in threads for tid in t}
        extra = thread_ids_flat - group_ids
        if extra:
            log.warning(
                "judge returned unknown ids for subject=%r: %s -- removing from threads",
                subject, extra,
            )
            threads = [[tid for tid in t if tid in group_ids] for t in threads]
            threads = [t for t in threads if t]  # drop empty threads

        # --- Order threads and log ------------------------------------------
        for thread in threads:
            ordered = sort_thread_by_created_at(thread, id_to_record)
            log_thread(subject, ordered, id_to_record)

        # --- Compute lineage ------------------------------------------------
        lineage = compute_lineage_for_group(group, threads, id_to_record)
        all_lineage.update(lineage)

        # Stats
        groups_processed += 1
        total_threads_found += len(threads)
        for fields in lineage.values():
            if not fields["current"]:
                total_superseded += 1

    # --- Write all lineage decisions to Qdrant ------------------------------
    writes = 0
    for point_id, lineage_fields in all_lineage.items():
        existing_payload = id_to_payload.get(point_id, {})
        write_lineage(client, point_id, existing_payload, lineage_fields, dry_run)
        writes += 1

    # --- Telemetry ----------------------------------------------------------
    push_telemetry(total_prompt_tokens, total_completion_tokens)

    # --- Update state file on successful non-dry-run ------------------------
    state_file_updated = False
    if state_file is not None and not dry_run:
        try:
            _write_state_file(state_file, run_start_ts)
            state_file_updated = True
            log.info("State file updated: %s -> %s", state_file, run_start_ts)
        except OSError as exc:
            log.warning("Failed to update state file %s: %s", state_file, exc)

    # --- Summary ------------------------------------------------------------
    log.info(
        "Run complete -- total_points=%d  groups_processed=%d  groups_skipped_stale=%d  "
        "threads_found=%d  superseded=%d  skipped_no_subject=%d  lineage_written=%d  "
        "state_file_updated=%s",
        len(all_points),
        groups_processed,
        groups_skipped_stale,
        total_threads_found,
        total_superseded,
        skipped_no_subject,
        writes,
        state_file_updated,
    )

    if dry_run:
        log.info("DRY RUN -- no writes made")


if __name__ == "__main__":
    main()
