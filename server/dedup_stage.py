#!/usr/bin/env python3
"""
dedup_stage.py — non-destructive near-duplicate collapse for fleet-memory (CT356).

Wheel #4 (docs/BACKLOG.md). Runs as step 3 of the nightly reconcile loop, after
subject_backfill.py and supersede.py. Groups current Qdrant points by
(user_id, canonical subject), finds near-duplicate pairs via cosine similarity
on the vectors already stored in Qdrant (no re-embedding, no API cost), applies
a NUMBER GUARD to keep value-sequence facts (e.g. "VERSION_CODE bumped to N")
out of the judge entirely, sends survivors to a LOCAL Ollama judge, and
collapses each resulting cluster non-destructively via set_payload — exactly
the supersede.py mechanism (current=False / superseded_by / supersedes),
never a delete.

NO OpenAI. NO Vault. NO mem0. The judge is local Ollama only (same
GATE_OLLAMA_URL / GATE_LLM_MODEL / *_FALLBACK env vars the write-path gate
uses, values delivered via a systemd drop-in) so this script needs no
credentials of its own.

Run on CT356:
  python3 /opt/memory-mcp/dedup_stage.py [--dry-run] [--subject SLUG] [--threshold F]
  python3 /opt/memory-mcp/dedup_stage.py --since-state          # incremental fast-path
  python3 /opt/memory-mcp/dedup_stage.py --since 2026-06-15T00:00:00Z

Incremental mode (--since / --since-state): mirrors supersede.py exactly. Only
(user_id, subject) groups containing at least one point with created_at newer
than the given timestamp are reprocessed; other groups are left untouched (no
LLM call, no payload writes, prior lineage/dedup fields preserved).

Concurrency lock (--lock-file, default /opt/memory-mcp/.dedup.lock): atomic
lockfile, same pattern as supersede.py. Already held -> exit 0, no work done.
Bypass with --no-lock for manual / dry-run invocations.

Head selection is by COMPLETENESS (has `why`, has `claim_type`, longer text),
never by created_at — transcript-miner-era created_at is inverted vs real
event order (see docs/BACKLOG.md), so newest-wins would silently pick the
worse-documented fact as the survivor.

Safety property: nothing is ever deleted. A wrong collapse costs one recall
miss and is reversible by flipping `current` back to true by hand.
"""
import argparse
import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

# Shared with server.py / supersede.py / subject_backfill.py -- the ONLY alias
# table. Pure-python module (json/re/time/os only), safe to import eagerly.
import subject_alias  # noqa: E402 — must come after sys.path insert; script dir is on sys.path[0]

# ---------------------------------------------------------------------------
# Config -- all via env, sane defaults. Nothing here requires qdrant_client or
# httpx to be importable, so this module (and every pure function below) can
# be imported by unit tests with neither package installed.
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dedup_stage")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("QDRANT_COLLECTION", "local_ai_cross_agent_memory")

COSINE_THRESHOLD_DEFAULT = float(os.environ.get("DEDUP_COSINE_THRESHOLD", "0.90"))
MAX_GROUP = int(os.environ.get("DEDUP_MAX_GROUP", "80"))
REPORT_PATH = os.environ.get("DEDUP_REPORT_PATH", "/var/lib/memory-stats/dedup-report.json")
STATE_PATH_DEFAULT = os.environ.get("DEDUP_STATE_PATH", "/opt/memory-mcp/.dedup.state")
LOCK_PATH_DEFAULT = "/opt/memory-mcp/.dedup.lock"

PUSHGATEWAY = os.environ.get("DEDUP_PUSHGATEWAY_URL", "http://192.168.50.223:9091")
METRICS_JOB = "memory_dedup"
METRICS_TIMEOUT_S = 2.0

# --- Judge backend config -- SAME env var names server/gate.py uses, values
# delivered to both processes via the same systemd drop-in. ---------------
GATE_OLLAMA_URL = os.environ.get("GATE_OLLAMA_URL", "")
GATE_LLM_MODEL = os.environ.get("GATE_LLM_MODEL", "qwen3:8b")
GATE_OLLAMA_FALLBACK_URL = os.environ.get("GATE_OLLAMA_FALLBACK_URL", "")
GATE_LLM_MODEL_FALLBACK = os.environ.get("GATE_LLM_MODEL_FALLBACK", "")
# Nightly batch, not an inline write path: the timeout must absorb a cold
# model load (~90s for the 12B primary), unlike the gate's tight 5s budget.
JUDGE_TIMEOUT_S = float(os.environ.get("DEDUP_JUDGE_TIMEOUT", "180"))
PREFLIGHT_TIMEOUT_S = float(os.environ.get("GATE_PREFLIGHT_TIMEOUT", "2"))
_KEEP_ALIVE = "10m"

# Benchmarked 2026-07-21 (tests/bench/run_dedup_bench.py): 99/100 on 100
# hand-labeled pairs, zero false collapses. Do NOT reword -- copied verbatim
# from the benchmark harness.
JUDGE_SYSTEM_PROMPT = (
    "You judge whether two stored memory facts are duplicates of each other.\n"
    "duplicate = both state the SAME claim about the same entity; one may be a "
    "reworded, shorter, or less detailed version of the other.\n"
    "distinct = they state DIFFERENT claims: a different attribute, a different "
    "event, opposite or changed behavior, or the same attribute with a different "
    "number, version, date, or value.\n"
    'Answer ONLY with JSON: {"verdict":"duplicate"} or {"verdict":"distinct"}'
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Non-destructive near-duplicate collapse for fleet-memory."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Full computation and report, zero writes (no set_payload, "
                             "no state file, no metrics push).")
    parser.add_argument("--subject", metavar="SLUG", default=None,
                        help="Restrict to one canonical subject slug (e.g. infraatlas).")
    parser.add_argument("--threshold", metavar="FLOAT", type=float, default=None,
                        help=f"Cosine threshold override (default {COSINE_THRESHOLD_DEFAULT}).")

    since_grp = parser.add_mutually_exclusive_group()
    since_grp.add_argument(
        "--since", metavar="TIMESTAMP",
        help=(
            "ISO8601 UTC timestamp. Only process (user_id, subject) groups that "
            "contain at least one point with created_at > TIMESTAMP. Groups with "
            "no recent point are left completely untouched."
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
        "--state-file", metavar="PATH", default=STATE_PATH_DEFAULT,
        help=f"State file path for --since-state (default: {STATE_PATH_DEFAULT}).",
    )

    parser.add_argument(
        "--lock-file", metavar="PATH", default=LOCK_PATH_DEFAULT,
        help=f"Atomic lockfile path (default: {LOCK_PATH_DEFAULT}).",
    )
    parser.add_argument(
        "--no-lock", action="store_true",
        help="Bypass the concurrency lock (for manual or dry-run invocations).",
    )

    return parser.parse_args()

# ---------------------------------------------------------------------------
# Pure logic -- no qdrant_client, no httpx. Importable and unit-testable
# without either package installed.
# ---------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"\d+")
_WS_RUN = re.compile(r"\s+")


def skeleton(text: str) -> str:
    """Lowercase, replace every digit run with '#', collapse whitespace."""
    s = (text or "").lower()
    s = _DIGIT_RUN.sub("#", s)
    s = _WS_RUN.sub(" ", s).strip()
    return s


def digit_sequences(text: str) -> list[str]:
    """Extracted digit runs, in order, exactly as they appear in text."""
    return _DIGIT_RUN.findall(text or "")


def is_number_guard_quarantine(a: str, b: str) -> bool:
    """
    True when a and b are the SAME skeleton (same words/structure once digits
    are blanked out) but their digit sequences differ -- a value that changed,
    not a duplicate fact. These pairs must never reach the judge or be
    collapsed (2026-05-28 sweep lesson: 14 "VERSION_CODE bumped to N" entries
    clustered as dupes by pure text similarity; auto-deleting would have kept
    stale values).
    """
    if skeleton(a) != skeleton(b):
        return False
    return digit_sequences(a) != digit_sequences(b)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Pure-python cosine similarity. 0.0 for empty/mismatched/zero vectors."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class UnionFind:
    """Minimal union-find over an arbitrary hashable id space."""

    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # path compression
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_pairs(ids: list[str], pairs: list[tuple[str, str]]) -> list[list[str]]:
    """
    Union-find clustering. ids is every point id in the group (so singletons
    that share no duplicate pair with anyone still appear as their own
    single-member cluster); pairs is the list of (id_a, id_b) judged
    "duplicate". Returns all resulting groups, sorted ids within each,
    including singletons -- callers filter len(cluster) > 1 for actual
    collapses.
    """
    uf = UnionFind(ids)
    for a, b in pairs:
        uf.union(a, b)
    groups: dict[str, list[str]] = defaultdict(list)
    for i in ids:
        groups[uf.find(i)].append(i)
    return [sorted(v) for v in groups.values()]


def completeness_score(record: dict) -> tuple[int, int]:
    """
    (score, content_len) -- higher score wins; content_len is the tiebreak.
    +2 for a non-empty `why`, +1 for a non-empty `claim_type`. created_at is
    NEVER consulted here (transcript-miner timestamps are inverted -- see
    docs/BACKLOG.md wheel #4).
    """
    score = 0
    why = record.get("why")
    if why and str(why).strip():
        score += 2
    claim_type = record.get("claim_type")
    if claim_type and str(claim_type).strip():
        score += 1
    content = record.get("content") or ""
    return score, len(content)


def choose_head(cluster_ids: list[str], id_to_record: dict) -> str:
    """
    Pick the cluster head by completeness score, tiebreak by longer content,
    final tiebreak by id (determinism only). created_at is never consulted.
    """
    def key(rid):
        score, length = completeness_score(id_to_record[rid])
        return (score, length, rid)
    return max(cluster_ids, key=key)


def candidate_pairs_for_group(
    records: list[dict], threshold: float
) -> list[tuple[str, str, float]]:
    """
    Pairwise cosine similarity over records carrying an "id", "content" and
    "vector" key. Returns (id_a, id_b, similarity) for every pair whose
    cosine similarity is >= threshold. O(n^2) -- fine given MAX_GROUP caps n.
    """
    pairs: list[tuple[str, str, float]] = []
    n = len(records)
    for i in range(n):
        a = records[i]
        va = a.get("vector")
        if not va:
            continue
        for j in range(i + 1, n):
            b = records[j]
            vb = b.get("vector")
            if not vb:
                continue
            sim = cosine_similarity(va, vb)
            if sim >= threshold:
                pairs.append((a["id"], b["id"], sim))
    return pairs

# ---------------------------------------------------------------------------
# Qdrant access -- lazy import so the module (and everything above) stays
# importable without qdrant_client installed.
# ---------------------------------------------------------------------------

def _get_qdrant_client():
    from qdrant_client import QdrantClient  # noqa: E402 -- lazy, after sys.path insert
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)


def scroll_all_with_vectors(client) -> list:
    """Paginated scroll of every point, WITH vectors (needed for cosine sim)."""
    all_points = []
    offset = None
    while True:
        res, offset = client.scroll(
            COLLECTION,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        all_points.extend(res)
        if offset is None:
            break
    return all_points


def extract_records(all_points) -> list[dict]:
    """
    Shape raw Qdrant points into the record dicts used throughout this
    module. Mirrors supersede.py's extract_records, plus vector/why/claim_type/
    current/supersedes needed for dedup.
    """
    records = []
    for pt in all_points:
        p = pt.payload or {}
        meta = p.get("metadata") or {}

        raw_subj = p.get("subject") or meta.get("subject")
        canonical_subj, _ = subject_alias.canonicalize(raw_subj)

        why = p.get("why") or meta.get("why")
        claim_type = p.get("claim_type") or meta.get("claim_type")
        current = p.get("current", True)
        supersedes = p.get("supersedes") or []

        vector = getattr(pt, "vector", None)
        if isinstance(vector, dict) and vector:
            # Named-vector collections return {name: [...]}; we use whatever
            # single vector is present.
            vector = next(iter(vector.values()))

        records.append({
            "id": str(pt.id),
            "content": p.get("data", "") or p.get("memory", "") or p.get("text", ""),
            "user_id": p.get("user_id", ""),
            "created_at": p.get("created_at", "") or meta.get("created_at", ""),
            "subject": canonical_subj,
            "current": current,
            "why": why,
            "claim_type": claim_type,
            "supersedes": supersedes,
            "vector": vector,
            "_payload": p,
        })
    return records

# ---------------------------------------------------------------------------
# Local Ollama judge -- self-contained (does NOT import gate._call_backend,
# which hardcodes the gate's own write-contract prompt). Routing mirrors
# server/gate.py exactly: pre-flight /api/ps, never evict a resident foreign
# model, transport failure falls back, both-fail = fail-open (pair skipped).
# ---------------------------------------------------------------------------

def _resident_models(url: str, timeout: float) -> list[str] | None:
    """Names of all models resident at url per /api/ps, or None if unreachable."""
    import httpx  # lazy
    try:
        resp = httpx.get(url.rstrip("/") + "/api/ps", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 -- treat any failure as "nothing resident"
        return None
    return [m.get("name") for m in (data.get("models") or []) if m.get("name")]


def _call_judge_backend(
    url: str, model: str, timeout: float, text_a: str, text_b: str
) -> tuple[str | None, str, bool]:
    """
    Call one Ollama /api/chat backend with the dedup judge prompt. Returns
    (verdict, raw_or_reason, transport_failed) -- verdict is "duplicate" /
    "distinct" on a parseable answer, else None. transport_failed is True
    only for connect-error/timeout (triggers fallback); False for a received
    but unparseable answer (fails open directly, no fallback attempt).
    """
    import httpx  # lazy
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Fact 1: {text_a}\nFact 2: {text_b}"},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "keep_alive": _KEEP_ALIVE,
    }
    api_url = url.rstrip("/") + "/api/chat"
    try:
        resp = httpx.post(api_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return None, "timeout", True
    except Exception as exc:  # noqa: BLE001 -- fail-open on any transport/parse error
        return None, f"error: {exc}", True

    raw = (data.get("message") or {}).get("content") or ""
    try:
        parsed = json.loads(raw)
        verdict = str(parsed.get("verdict", "")).strip().lower()
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, f"unparseable answer: {raw[:120]!r}", False
    if verdict in ("duplicate", "distinct"):
        return verdict, raw, False
    return None, f"unparseable verdict: {raw[:120]!r}", False


def judge_pair(text_a: str, text_b: str) -> tuple[str | None, str]:
    """
    Returns (verdict, backend). verdict is "duplicate"/"distinct"/None
    (None == skipped, pair not judged, not collapsed). backend is
    "primary"/"fallback"/"skipped". Never raises.
    """
    if not GATE_OLLAMA_URL:
        return None, "skipped"

    resident = _resident_models(GATE_OLLAMA_URL, PREFLIGHT_TIMEOUT_S)
    if resident and GATE_LLM_MODEL not in resident:
        log.info(
            "dedup: pre-flight found %r resident on primary (want %r) -- "
            "skipping primary to avoid thrash",
            resident, GATE_LLM_MODEL,
        )
        verdict, raw, transport_failed = None, f"pre-flight: primary busy with {resident!r}", True
    else:
        verdict, raw, transport_failed = _call_judge_backend(
            GATE_OLLAMA_URL, GATE_LLM_MODEL, JUDGE_TIMEOUT_S, text_a, text_b
        )

    if verdict is not None:
        return verdict, "primary"
    if not transport_failed:
        log.warning("dedup: unparseable answer from primary %r -- pair skipped", raw[:120])
        return None, "skipped"

    log.warning(
        "dedup: primary backend (%s) failed (%s)%s",
        GATE_OLLAMA_URL, raw,
        " -- trying fallback" if GATE_OLLAMA_FALLBACK_URL else " -- pair skipped",
    )
    if not GATE_OLLAMA_FALLBACK_URL:
        return None, "skipped"

    fb_verdict, fb_raw, _fb_transport_failed = _call_judge_backend(
        GATE_OLLAMA_FALLBACK_URL, GATE_LLM_MODEL_FALLBACK or GATE_LLM_MODEL,
        JUDGE_TIMEOUT_S, text_a, text_b,
    )
    if fb_verdict is not None:
        return fb_verdict, "fallback"

    log.warning(
        "dedup: fallback backend (%s) also failed (%s) -- pair skipped",
        GATE_OLLAMA_FALLBACK_URL, fb_raw,
    )
    return None, "skipped"

# ---------------------------------------------------------------------------
# Non-destructive write -- FLAT top-level keys via set_payload, same
# mechanism as supersede.py. NO deletes anywhere in this file.
# ---------------------------------------------------------------------------

def write_dedup_fields(client, point_id: str, fields: dict, dry_run: bool) -> None:
    if not dry_run:
        client.set_payload(
            collection_name=COLLECTION,
            payload=dict(fields),
            points=[point_id],
        )

# ---------------------------------------------------------------------------
# Report + metrics
# ---------------------------------------------------------------------------

def write_report(path: str, report: dict) -> None:
    """Best-effort report write -- a failure here must never fail the run."""
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        os.replace(tmp_path, path)
        log.info("Report written: %s", path)
    except Exception as exc:  # noqa: BLE001 -- report is observability, not correctness
        log.warning("Report write failed (non-fatal): %s", exc)


def push_metrics(collapsed: int, quarantined: int, judged: int, judge_skipped: int) -> None:
    """Best-effort Pushgateway push, job=memory_dedup. Skipped entirely in dry-run."""
    try:
        import httpx  # lazy
        now_ts = time.time()
        body = (
            "# TYPE memory_dedup_collapsed_total counter\n"
            f"memory_dedup_collapsed_total {collapsed}\n"
            "# TYPE memory_dedup_quarantined_total counter\n"
            f"memory_dedup_quarantined_total {quarantined}\n"
            "# TYPE memory_dedup_judged_total counter\n"
            f"memory_dedup_judged_total {judged}\n"
            "# TYPE memory_dedup_judge_skipped_total counter\n"
            f"memory_dedup_judge_skipped_total {judge_skipped}\n"
            "# TYPE memory_dedup_last_run_timestamp_seconds gauge\n"
            f"memory_dedup_last_run_timestamp_seconds {now_ts}\n"
        )
        httpx.put(
            f"{PUSHGATEWAY.rstrip('/')}/metrics/job/{METRICS_JOB}",
            content=body.encode(),
            timeout=METRICS_TIMEOUT_S,
        )
        log.info(
            "Telemetry pushed: collapsed=%d quarantined=%d judged=%d judge_skipped=%d",
            collapsed, quarantined, judged, judge_skipped,
        )
    except Exception as exc:  # noqa: BLE001 -- metrics must never break the run
        log.warning("Pushgateway push failed (non-fatal): %s", exc)

# ---------------------------------------------------------------------------
# Lock + state file helpers -- verbatim pattern from supersede.py
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: str) -> int:
    """Atomically create the lockfile. Returns an fd, or raises FileExistsError."""
    return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def _release_lock(fd: int, lock_path: str) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def _read_state_file(path: str) -> str | None:
    try:
        with open(path) as fh:
            ts = fh.read().strip()
        return ts if ts else None
    except FileNotFoundError:
        return None


def _write_state_file(path: str, ts: str) -> None:
    with open(path, "w") as fh:
        fh.write(ts + "\n")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    dry_run: bool = args.dry_run
    subject_filter: str | None = args.subject
    threshold: float = args.threshold if args.threshold is not None else COSINE_THRESHOLD_DEFAULT

    run_start_ts: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    since_ts: str | None = None
    if args.since:
        since_ts = args.since
        log.info("Incremental mode: --since %s", since_ts)
    elif args.since_state:
        since_ts = _read_state_file(args.state_file)
        if since_ts:
            log.info("Incremental mode: --since-state resolved to %s (from %s)", since_ts, args.state_file)
        else:
            log.info("--since-state: state file absent or empty -- running full pass")

    lock_fd: int | None = None
    if not args.no_lock:
        try:
            lock_fd = _acquire_lock(args.lock_file)
            log.info("Lock acquired: %s", args.lock_file)
        except FileExistsError:
            log.info("Another reconcile is running -- exiting (lock held at %s)", args.lock_file)
            sys.exit(0)

    try:
        _run(
            dry_run=dry_run,
            subject_filter=subject_filter,
            threshold=threshold,
            since_ts=since_ts,
            run_start_ts=run_start_ts,
            state_file=args.state_file if args.since_state else None,
        )
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, args.lock_file)
            log.info("Lock released: %s", args.lock_file)


def _run(
    dry_run: bool,
    subject_filter: str | None,
    threshold: float,
    since_ts: str | None,
    run_start_ts: str,
    state_file: str | None,
) -> None:
    client = _get_qdrant_client()

    log.info("Scrolling all points (with vectors) from %s ...", COLLECTION)
    all_points = scroll_all_with_vectors(client)
    log.info("Total points: %d", len(all_points))

    records = extract_records(all_points)

    # --- Keep only current points with a subject ----------------------------
    valid_records: list[dict] = []
    skipped_not_current = 0
    skipped_no_subject = 0
    for rec in records:
        if not rec["current"]:
            skipped_not_current += 1
            continue
        if not rec["subject"]:
            skipped_no_subject += 1
            continue
        if subject_filter and rec["subject"] != subject_filter:
            continue
        valid_records.append(rec)

    log.info(
        "Valid (current + have subject) records for processing: %d "
        "(skipped_not_current=%d skipped_no_subject=%d)",
        len(valid_records), skipped_not_current, skipped_no_subject,
    )

    id_to_record: dict[str, dict] = {r["id"]: r for r in valid_records}

    # --- Group by (user_id, canonical subject) ------------------------------
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in valid_records:
        groups[(rec["user_id"], rec["subject"])].append(rec)

    # --- Run accumulators ----------------------------------------------------
    groups_seen = 0
    groups_skipped_too_big = 0
    total_candidate_pairs = 0
    total_judged = 0
    total_judge_skipped = 0
    quarantined_report: list[dict] = []
    collapsed_clusters_report: list[dict] = []
    backend_counts: dict[str, int] = defaultdict(int)

    all_writes: dict[str, dict] = {}  # point_id -> fields to set_payload

    for (user_id, subject), group in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if since_ts is not None:
            has_recent = any(rec["created_at"] > since_ts for rec in group)
            if not has_recent:
                continue

        if len(group) < 2:
            continue  # nothing to compare

        if len(group) > MAX_GROUP:
            groups_skipped_too_big += 1
            log.warning(
                "user=%r subject=%r has %d facts > DEDUP_MAX_GROUP=%d -- skipping group",
                user_id, subject, len(group), MAX_GROUP,
            )
            continue

        groups_seen += 1
        group_ids = [r["id"] for r in group]
        group_id_to_record = {r["id"]: r for r in group}

        candidates = candidate_pairs_for_group(group, threshold)
        total_candidate_pairs += len(candidates)

        dup_pairs: list[tuple[str, str]] = []
        for id_a, id_b, sim in candidates:
            rec_a, rec_b = group_id_to_record[id_a], group_id_to_record[id_b]
            text_a, text_b = rec_a["content"], rec_b["content"]

            if is_number_guard_quarantine(text_a, text_b):
                quarantined_report.append({
                    "ids": [id_a, id_b],
                    "subject": subject,
                    "texts": [text_a[:120], text_b[:120]],
                })
                log.info(
                    "quarantine subject=%r <%s> vs <%s> -- equal skeleton, different numbers",
                    subject, id_a[:8], id_b[:8],
                )
                continue

            verdict, backend = judge_pair(text_a, text_b)
            backend_counts[backend] += 1
            if verdict is None:
                total_judge_skipped += 1
                continue

            total_judged += 1
            if verdict == "duplicate":
                dup_pairs.append((id_a, id_b))

        clusters = cluster_pairs(group_ids, dup_pairs)
        for cluster in clusters:
            if len(cluster) < 2:
                continue  # singleton -- nothing to collapse

            head_id = choose_head(cluster, group_id_to_record)
            losers = [rid for rid in cluster if rid != head_id]

            log.info(
                "collapse subject=%r head=%s losers=%s",
                subject, head_id[:8], [rid[:8] for rid in losers],
            )

            for loser_id in losers:
                all_writes[loser_id] = {
                    "current": False,
                    "superseded_by": head_id,
                    "dedup_collapsed": True,
                }

            head_rec = group_id_to_record[head_id]
            existing_supersedes = head_rec.get("supersedes") or []
            new_supersedes = list(dict.fromkeys(list(existing_supersedes) + losers))
            all_writes[head_id] = {"supersedes": new_supersedes}

            collapsed_clusters_report.append({
                "head": head_id,
                "losers": losers,
                "subject": subject,
            })

    # --- Write all collapse decisions to Qdrant -----------------------------
    for point_id, fields in all_writes.items():
        write_dedup_fields(client, point_id, fields, dry_run)

    total_collapsed = sum(len(c["losers"]) for c in collapsed_clusters_report)

    # --- Report (best-effort, written even on --dry-run) --------------------
    report = {
        "run_at": run_start_ts,
        "dry_run": dry_run,
        "groups_seen": groups_seen,
        "groups_skipped_too_big": groups_skipped_too_big,
        "candidate_pairs": total_candidate_pairs,
        "quarantined": quarantined_report,
        "judged": total_judged,
        "collapsed_clusters": collapsed_clusters_report,
        "judge_skipped_pairs": total_judge_skipped,
        "backend_counts": dict(backend_counts),
    }
    write_report(REPORT_PATH, report)

    # --- Metrics (best-effort, skipped in dry-run) --------------------------
    if not dry_run:
        push_metrics(
            collapsed=total_collapsed,
            quarantined=len(quarantined_report),
            judged=total_judged,
            judge_skipped=total_judge_skipped,
        )

    # --- Update state file on successful non-dry-run ------------------------
    state_file_updated = False
    if state_file is not None and not dry_run:
        try:
            _write_state_file(state_file, run_start_ts)
            state_file_updated = True
            log.info("State file updated: %s -> %s", state_file, run_start_ts)
        except OSError as exc:
            log.warning("Failed to update state file %s: %s", state_file, exc)

    # --- Summary --------------------------------------------------------------
    log.info(
        "Run complete -- total_points=%d groups_seen=%d groups_skipped_too_big=%d "
        "candidate_pairs=%d quarantined=%d judged=%d judge_skipped=%d collapsed=%d "
        "clusters=%d state_file_updated=%s",
        len(all_points), groups_seen, groups_skipped_too_big,
        total_candidate_pairs, len(quarantined_report), total_judged,
        total_judge_skipped, total_collapsed, len(collapsed_clusters_report),
        state_file_updated,
    )

    if dry_run:
        log.info("DRY RUN -- no writes made")


if __name__ == "__main__":
    main()
