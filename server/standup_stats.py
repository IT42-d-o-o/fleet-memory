#!/usr/bin/env python3
"""
Fleet-memory health collector for CT356 (memory-mcp).

Read-only against the memory service. Does NOT import or touch server.py
(another agent may be editing it concurrently) -- talks to Qdrant directly
over HTTP on 127.0.0.1:6333 and to the MCP servers search_memory tool over
its JSON-RPC/streamable-http endpoint on 127.0.0.1:8800, exactly like a
normal client would.

Computes:
  - total point count
  - writes in the last 24h grouped by payload.source
  - secret-gate / contract-gate outcome counts (from /opt/memory-mcp/gate.log,
    last 24h) -- the file may not exist yet; handled gracefully
  - LLM gate backend mix (primary/fallback/skipped) in the last 24h, from the
    "backend" field added to gate.log lines by the 2026-07-13 primary/fallback
    gate upgrade -- absent on older lines, handled gracefully (not counted)
  - writes in the last 24h with payload.gate in (skipped, bypassed) -- the
    field may not exist on any point yet; handled gracefully
  - write-flood tripwire: any single source with >30 writes in 24h
  - a 5-probe retrieval canary against search_memory

Output: JSON to /var/lib/memory-stats/standup.json

Cron: 45 7 * * * root (see /etc/cron.d/memory-standup-stats)
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta, date

QDRANT_URL = "http://127.0.0.1:6333"
COLLECTION = "local_ai_cross_agent_memory"
MCP_URL = "http://127.0.0.1:8800/mcp"
GATE_LOG = "/opt/memory-mcp/gate.log"
OUT_DIR = "/var/lib/memory-stats"
OUT_PATH = os.path.join(OUT_DIR, "standup.json")
FLOOD_THRESHOLD = 30

LOG = sys.stderr.write


# ---------------------------------------------------------------- qdrant ---

def scroll_all_points():
    """Yield every points payload from the collection via the Qdrant HTTP API."""
    offset = None
    url = QDRANT_URL + "/collections/" + COLLECTION + "/points/scroll"
    while True:
        body = {"limit": 200, "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result") or {}
        for p in result.get("points", []):
            yield p.get("payload") or {}
        offset = result.get("next_page_offset")
        if not offset:
            break


def get_total_count():
    url = QDRANT_URL + "/collections/" + COLLECTION + "/points/count"
    req = urllib.request.Request(
        url, data=json.dumps({}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", {}).get("count")


def parse_iso(ts):
    """Parse an ISO-8601 timestamp (payload created_at) into an aware UTC
    datetime. Returns None on missing/unparseable input rather than raising."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def compute_qdrant_stats():
    """Payload is FLAT (verified live 2026-07-12): source, created_at, subject,
    category, gate (not yet present on any point), etc. are all top-level keys
    -- there is NO nested metadata object in this collections payloads."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    total = None
    writes_24h_by_source = {}
    gate_field_24h = {"skipped": 0, "bypassed": 0}
    gate_field_seen_any = False
    scanned = 0
    errors = []

    try:
        for payload in scroll_all_points():
            scanned += 1
            created = parse_iso(payload.get("created_at"))
            if created is None or created < cutoff:
                continue
            source = payload.get("source") or "(unknown)"
            writes_24h_by_source[source] = writes_24h_by_source.get(source, 0) + 1
            gate_val = payload.get("gate")
            if gate_val:
                gate_field_seen_any = True
                if gate_val in gate_field_24h:
                    gate_field_24h[gate_val] += 1
                else:
                    gate_field_24h[gate_val] = gate_field_24h.get(gate_val, 0) + 1
    except Exception as e:
        errors.append("qdrant scroll failed: " + str(e))

    try:
        total = get_total_count()
    except Exception as e:
        errors.append("qdrant count failed: " + str(e))
        total = scanned or None

    floods = [
        {"source": src, "count": cnt}
        for src, cnt in sorted(writes_24h_by_source.items(), key=lambda kv: -kv[1])
        if cnt > FLOOD_THRESHOLD
    ]

    return {
        "total_points": total,
        "scanned_for_24h_window": scanned,
        "writes_24h_by_source": writes_24h_by_source,
        "point_metadata_gate_24h": gate_field_24h if gate_field_seen_any else "no data (gate field not present on any point yet)",
        "floods": floods,
        "flood_threshold": FLOOD_THRESHOLD,
        "errors": errors,
    }


# -------------------------------------------------------------- predictions

def compute_prediction_stats():
    """Predictions live as ordinary points with payload.claim_type ==
    "prediction" (payload is FLAT, same as compute_qdrant_stats() above -- no
    nested metadata object). Tallies by status: open (not yet expired),
    expired_open (status still "open" but payload.expires_on is in the past),
    hit, miss. Absent-safe: if no prediction points exist yet, or the
    collection can't be scrolled, returns zero counts plus an errors list
    rather than raising or omitting the key -- mirrors the gate.log
    'available' pattern used above so an older/newer collector schema never
    breaks the standup card."""
    today = datetime.now(timezone.utc).date()
    counts = {"open": 0, "expired_open": 0, "hit": 0, "miss": 0}
    scanned = 0
    errors = []

    try:
        for payload in scroll_all_points():
            if payload.get("claim_type") != "prediction":
                continue
            scanned += 1
            status = payload.get("status") or "open"
            if status == "open":
                expires_on = payload.get("expires_on")
                is_expired = False
                if expires_on:
                    try:
                        is_expired = date.fromisoformat(str(expires_on)) < today
                    except Exception:
                        is_expired = False
                if is_expired:
                    counts["expired_open"] += 1
                else:
                    counts["open"] += 1
            else:
                counts[status] = counts.get(status, 0) + 1
    except Exception as e:
        errors.append("qdrant scroll (predictions) failed: " + str(e))

    return {
        "counts": counts,
        "scanned": scanned,
        "errors": errors,
    }


# ------------------------------------------------------------- gate.log ---

def compute_gate_log_stats(path=GATE_LOG):
    """gate.log format assumed to be one JSON object per line (or a simple
    plaintext outcome token per line) with an ISO timestamp. Handles total
    absence (another agent is adding this file today) and unknown/mixed
    formats gracefully -- never raises."""
    if not os.path.exists(path):
        return {"available": False, "reason": "gate.log does not exist yet", "outcomes_24h": {}}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    outcomes = {}
    backend_mix = {}
    backend_field_seen_any = False
    lines_seen = 0
    lines_in_window = 0
    parse_errors = 0

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                lines_seen += 1
                ts = None
                outcome = None
                backend = None
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        ts = parse_iso(obj.get("timestamp") or obj.get("ts") or obj.get("created_at"))
                        outcome = obj.get("outcome") or obj.get("result") or obj.get("gate") or obj.get("status")
                        backend = obj.get("backend")
                except Exception:
                    obj = None
                if outcome is None:
                    m = re.match(r"^(\S+)\s+(\S+)", line)
                    if m:
                        ts = ts or parse_iso(m.group(1))
                        outcome = m.group(2)
                if outcome is None:
                    parse_errors += 1
                    continue
                if ts is not None and ts < cutoff:
                    continue
                lines_in_window += 1
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                if backend:
                    backend_field_seen_any = True
                    backend_mix[backend] = backend_mix.get(backend, 0) + 1
    except Exception as e:
        return {"available": False, "reason": "gate.log read failed: " + str(e), "outcomes_24h": {}}

    return {
        "available": True,
        "lines_seen_total": lines_seen,
        "lines_in_24h_window": lines_in_window,
        "parse_errors": parse_errors,
        "outcomes_24h": outcomes,
        "backend_mix_24h": backend_mix if backend_field_seen_any else "no data (backend field not present on any line yet)",
    }


# ------------------------------------------------------------------ MCP ---

ACCEPT = "application/json, text/event-stream"


def _mcp_parse_sse(body):
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body)


def _mcp_post(session_id, payload_dict, timeout=30):
    headers = {"Content-Type": "application/json", "Accept": ACCEPT}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload_dict).encode(),
        headers=headers, method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    sid = resp.headers.get("Mcp-Session-Id") or session_id
    raw = resp.read().decode("utf-8", errors="replace")
    return sid, raw


CANARY_PROBES = [
    # (query, expected substring in ANY of top-5 result texts, case-insensitive)
    # NOTE: probe 3 was fully substituted at development time (2026-07-12) --
    # the originally-specified probe (query: vault approle memory-mcp
    # auto-auth, expect: approle) does not exist anywhere in the fleet pool
    # (scrolled all 1774 points to confirm zero matches for approle /
    # vault-agent-mem0 / "vault agent" text). That fact lives only in local
    # workstation memory, not fleet mem0. Replaced with a stable, dated fact
    # describing the fleet-memory servers own API signature, verified 5/5 at
    # deploy time (see final report for the raw test transcript).
    ("gitea push write token", "write token"),
    ("iredmail smtp mail server", "iredmail"),
    ("fleet-memory server exposes add_memory search_memory backend qdrant mem0 sqlite", "qdrant"),
    ("transcript miner retired decision", "retired"),
    ("phase c cleanup deleted miner facts", "1,494"),
]


def run_canary():
    results = []
    session_id = None
    init_error = None
    try:
        session_id, _ = _mcp_post(None, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "memory-standup-canary", "version": "1"},
            },
        }, timeout=15)
        try:
            _mcp_post(session_id, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
        except Exception:
            pass
    except Exception as e:
        init_error = str(e)

    for query, expect in CANARY_PROBES:
        entry = {"query": query, "expect": expect, "pass": False, "top_score": None, "error": None}
        if init_error:
            entry["error"] = "mcp initialize failed: " + init_error
            results.append(entry)
            continue
        try:
            _, raw = _mcp_post(session_id, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search_memory", "arguments": {"query": query, "limit": 5}},
            }, timeout=30)
            data = _mcp_parse_sse(raw)
            content = data.get("result", {}).get("content", [])
            text = content[0].get("text", "") if content else json.dumps(data)
            entry["pass"] = expect.lower() in text.lower()
            try:
                parsed = json.loads(text)
                hits = parsed.get("results", [])
                if hits:
                    entry["top_score"] = hits[0].get("score")
            except Exception:
                pass
        except Exception as e:
            entry["error"] = str(e)
        results.append(entry)

    passed = sum(1 for r in results if r["pass"])
    return {"probes": results, "passed": passed, "total": len(CANARY_PROBES)}


# ----------------------------------------------------------------- main ---

def main():
    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "qdrant": compute_qdrant_stats(),
        "gate_log": compute_gate_log_stats(),
        "predictions": compute_prediction_stats(),
        "canary": run_canary(),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp_path, OUT_PATH)

    LOG("[standup_stats] wrote " + OUT_PATH + ": total=" + str(out["qdrant"].get("total_points")) +
        " floods=" + str(len(out["qdrant"].get("floods", []))) +
        " canary=" + str(out["canary"]["passed"]) + "/" + str(out["canary"]["total"]) + "\n")


if __name__ == "__main__":
    main()

