#!/usr/bin/env python3
"""
recall_bench.py — nightly scored recall benchmark for fleet-memory (CT356).

Wheel: docs/BACKLOG.md "Next up" item 3 (Scored recall benchmark). Extends the
5-probe retrieval canary in standup_stats.py into a scored, trendable number:
run a probe set of (question, expected substrings) pairs against the LIVE MCP
search path -- exactly the same http://127.0.0.1:8800/mcp search_memory tool a
real session hook calls -- so RRF fusion, namespace merging and supersession
resolution are all exercised, not bypassed. Optionally also scores the SAME
probes against a flat-markdown baseline (naive keyword retrieval over every
CURRENT fact dumped as paragraphs) so "is the pipeline better than grepping
files" has an actual number instead of a vibe.

A probe is a HIT when any of its `expect_any` substrings (case-insensitive)
appears anywhere in the concatenated text of the top-5 results returned for
its `question`.

NO OpenAI. NO Vault. NO mem0 import. Talks to the MCP server over HTTP
(httpx) exactly like any other client, and to Qdrant directly (qdrant_client)
for the optional baseline export -- both imported lazily so this module (and
every pure function below) is importable, and unit-testable, with neither
package installed.

Run on CT356:
  python3 /opt/memory-mcp/recall_bench.py
  python3 /opt/memory-mcp/recall_bench.py --baseline
  python3 /opt/memory-mcp/recall_bench.py --probes /path/to/probes.json --limit 10
  python3 /opt/memory-mcp/recall_bench.py --no-metrics

Probe file format (JSON list, default path via --probes / RECALL_PROBES_PATH):
  [{"id": 1,
    "question": "which container runs the fleet memory MCP server and on what address?",
    "expect_any": ["CT356", "192.168.50.138"],
    "subject": "fleet-memory"}]

Exit code: 0 always, UNLESS a fatal error occurs (probe file unreadable, or
the MCP endpoint unreachable at session-init time) -- a low score is data,
not a failure, and must not turn a nightly cron job red.
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

# ---------------------------------------------------------------------------
# Config -- all via env, sane defaults. Nothing here requires qdrant_client or
# httpx to be importable, so this module (and every pure function below) can
# be imported by unit tests with neither package installed.
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recall_bench")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION = os.environ.get("QDRANT_COLLECTION", "local_ai_cross_agent_memory")

MCP_URL = os.environ.get("RECALL_MCP_URL", "http://127.0.0.1:8800/mcp")
MCP_ACCEPT = "application/json, text/event-stream"
MCP_INIT_TIMEOUT_S = float(os.environ.get("RECALL_MCP_INIT_TIMEOUT", "15"))
MCP_SEARCH_TIMEOUT_S = float(os.environ.get("RECALL_MCP_SEARCH_TIMEOUT", "30"))
MCP_RESULT_LIMIT = 5  # fixed by the probe-set contract: "top-5 results"

PROBES_PATH_DEFAULT = os.environ.get("RECALL_PROBES_PATH", "/opt/memory-mcp/recall_probes.json")
REPORT_PATH = os.environ.get("RECALL_REPORT_PATH", "/var/lib/memory-stats/recall-bench.json")
BASELINE_DUMP_PATH = os.environ.get("RECALL_BASELINE_DUMP_PATH", "/tmp/memory-dump.md")

PUSHGATEWAY = os.environ.get("RECALL_PUSHGATEWAY_URL", "http://192.168.50.223:9091")
METRICS_JOB = "memory_recall"
METRICS_TIMEOUT_S = 2.0

SNIPPET_LEN = 80

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Nightly scored recall benchmark for fleet-memory (docs/BACKLOG.md item 3)."
    )
    parser.add_argument(
        "--probes", metavar="PATH", default=PROBES_PATH_DEFAULT,
        help=f"Probe file path (default {PROBES_PATH_DEFAULT}).",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="Also score probes against a flat-markdown keyword-retrieval baseline "
             "(exports current facts from Qdrant, writes them to a markdown dump, "
             "scores with naive keyword matching -- no LLM, no re-embedding).",
    )
    parser.add_argument(
        "--no-metrics", action="store_true",
        help="Skip the Pushgateway metrics push.",
    )
    parser.add_argument(
        "--limit", metavar="N", type=int, default=None,
        help="Only run the first N probes (for quick manual checks).",
    )
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Probe loading -- stdlib file IO only, no network.
# ---------------------------------------------------------------------------

def load_probes(path: str) -> list[dict]:
    """
    Load + validate the probe file. Raises on missing file, invalid JSON, or
    a probe missing required keys -- callers treat this as fatal (a broken
    probe set is worth failing loudly on, unlike a low score).
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"probe file {path} must contain a JSON list")
    probes: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"probe at index {i} is not an object")
        if "question" not in item or "expect_any" not in item:
            raise ValueError(f"probe at index {i} missing required keys (question, expect_any)")
        probes.append(item)
    return probes

# ---------------------------------------------------------------------------
# Pure logic -- no qdrant_client, no httpx. Importable and unit-testable
# without either package installed.
# ---------------------------------------------------------------------------

# Small English stopword set -- determiners, prepositions, conjunctions,
# pronouns, wh-words and a handful of auxiliary/copula verbs. Kept small and
# generic on purpose: this baseline is meant to be an HONEST simulation of
# "an agent grepping markdown files", not a tuned IR pipeline.
STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "nor",
    "but", "so", "yet", "as", "by", "from", "with", "about", "into", "onto",
    "over", "under", "than", "then", "this", "that", "these", "those", "it",
    "its", "is", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "doing", "have", "has", "had", "having", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "not", "no", "which",
    "what", "who", "whom", "whose", "how", "when", "where", "why", "any",
    "all", "each", "every", "some", "such", "i", "you", "he", "she", "we",
    "they", "them", "his", "her", "our", "your", "their",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, return the raw word tokens (no stopword
    filtering) -- used both for keyword extraction and paragraph scoring so
    the two sides compare like-for-like."""
    return _WORD_RE.findall((text or "").lower())


def extract_keywords(question: str) -> set[str]:
    """Lowercase the question, strip punctuation and stopwords, return the
    distinct remaining terms. Pure; no LLM."""
    return {t for t in _tokenize(question) if t not in STOPWORDS}


def score_paragraph(paragraph: str, terms: set[str]) -> int:
    """Count of DISTINCT terms (from `terms`) present as whole words in
    `paragraph`. Word-level, not substring, so e.g. "cat" doesn't spuriously
    match "category"."""
    if not terms:
        return 0
    paragraph_words = set(_tokenize(paragraph))
    return len(terms & paragraph_words)


def top_k_paragraphs(paragraphs: list[str], terms: set[str], k: int = 5) -> list[str]:
    """
    Rank paragraphs by score_paragraph descending, stable on ties (keeps
    original relative order -- Python's sort is stable), return the top k.
    Pure python, no LLM -- the honest simulation of an agent grepping files.
    """
    ranked = sorted(enumerate(paragraphs), key=lambda iv: -score_paragraph(iv[1], terms))
    return [p for _, p in ranked[:k]]


def is_hit(text: str, expect_any: list[str]) -> bool:
    """True if ANY expect_any substring appears in text, case-insensitive."""
    hay = (text or "").lower()
    return any(str(e).lower() in hay for e in (expect_any or []))


def unescape_tool_result(raw_text: str) -> str:
    """
    Flatten the search_memory tool result into plain matchable text. The raw
    body is a JSON string, so backslash paths and quotes inside fact texts
    arrive escaped (C:\\\\Users, \\") and would false-miss a plain substring
    check. Parse and concatenate the fact texts; fall back to the raw body.
    """
    try:
        data = json.loads(raw_text)
        # FastMCP sometimes wraps the tool return as {"result": "<json string>"}
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            data = json.loads(data["result"])
        results = data.get("results", []) if isinstance(data, dict) else []
        parts = []
        for r in results:
            if isinstance(r, dict):
                parts.append(str(r.get("memory") or r.get("data") or r.get("text") or ""))
        if parts:
            return "\n".join(parts)
    except Exception:  # noqa: BLE001 -- fall through to raw
        pass
    return raw_text


def snippet(text: str, n: int = SNIPPET_LEN) -> str:
    return (text or "")[:n]


def extract_result_snippets(raw_text: str, n: int = SNIPPET_LEN) -> list[str]:
    """
    Given the raw text returned by the search_memory MCP tool call (a JSON
    string shaped {"results": [...]}), return the first `n` chars of each
    result's fact text, for MISS logging/reporting. Falls back to a raw-text
    snippet if the payload isn't the expected shape -- never raises.
    """
    try:
        data = json.loads(raw_text)
        results = data.get("results", []) if isinstance(data, dict) else []
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            fact = r.get("memory") or r.get("data") or r.get("text") or ""
            out.append(snippet(fact, n))
        return out
    except Exception:  # noqa: BLE001 -- reporting helper, never raises
        return [snippet(raw_text, n)]


def shape_arm(hits: int, total: int, misses: list[dict]) -> dict:
    """Shape one arm's (mcp or baseline) results into the report sub-object."""
    hit_rate = (hits / total) if total else 0.0
    return {"hits": hits, "total": total, "hit_rate": hit_rate, "misses": misses}


def build_report(run_at: str, probes_count: int, mcp_arm: dict, baseline_arm: dict | None) -> dict:
    """Shape the full report dict written to RECALL_REPORT_PATH."""
    return {
        "run_at": run_at,
        "probes": probes_count,
        "mcp": mcp_arm,
        "baseline": baseline_arm,
    }


def build_paragraph(subject: str, content: str) -> str:
    """One markdown paragraph: '## <subject>' header + blank line + content."""
    return f"## {subject or 'unknown'}\n\n{content}"

# ---------------------------------------------------------------------------
# MCP client -- streamable-http JSON-RPC, mirrors the working handshake in
# standup_stats.py's run_canary (initialize -> notifications/initialized ->
# tools/call), but over httpx instead of urllib. httpx imported lazily so
# this module stays importable without it.
# ---------------------------------------------------------------------------

def _mcp_parse_sse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body)


def _mcp_post(session_id: str | None, payload_dict: dict, timeout: float = 30) -> tuple[str | None, str]:
    import httpx  # lazy
    headers = {"Content-Type": "application/json", "Accept": MCP_ACCEPT}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    resp = httpx.post(MCP_URL, json=payload_dict, headers=headers, timeout=timeout)
    resp.raise_for_status()
    sid = resp.headers.get("Mcp-Session-Id") or session_id
    return sid, resp.text


def mcp_init_session() -> str | None:
    """initialize -> notifications/initialized. Raises on transport failure
    (connect error, timeout, non-2xx) -- callers treat that as fatal
    ('MCP unreachable')."""
    session_id, _ = _mcp_post(None, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "memory-recall-bench", "version": "1"},
        },
    }, timeout=MCP_INIT_TIMEOUT_S)
    try:
        _mcp_post(session_id, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
    except Exception:  # noqa: BLE001 -- notification is fire-and-forget
        pass
    return session_id


def mcp_search(session_id: str | None, query: str, limit: int = MCP_RESULT_LIMIT,
               project: str | None = None) -> str:
    """
    tools/call search_memory. Returns the raw tool-result text (a JSON string
    shaped {"results": [...]}) -- the "concatenated text of the top-5
    results" the probe contract scores against. Raises on transport failure.
    """
    _, raw = _mcp_post(session_id, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "search_memory", "arguments": (
            {"query": query, "limit": limit, "project": project} if project
            else {"query": query, "limit": limit}
        )},
    }, timeout=MCP_SEARCH_TIMEOUT_S)
    data = _mcp_parse_sse(raw)
    content = data.get("result", {}).get("content", [])
    return content[0].get("text", "") if content else json.dumps(data)

# ---------------------------------------------------------------------------
# MCP arm -- scores the SAME live search path a session hook uses.
# ---------------------------------------------------------------------------

def run_mcp_arm(probes: list[dict]) -> dict:
    """
    Score every probe against the live MCP search_memory tool. Raises only if
    session init fails (transport-level "MCP unreachable"); a per-probe
    search failure after a successful init is logged and counted as a MISS,
    not fatal -- a low score is data.
    """
    session_id = mcp_init_session()

    hits = 0
    misses: list[dict] = []
    for probe in probes:
        pid = probe.get("id")
        question = probe.get("question", "")
        expect_any = probe.get("expect_any") or []
        # A probe whose source fact lives in fleet:{project} carries a
        # "project" key — pass it exactly like the session hook does, so the
        # bench measures the same two-namespace search a real session gets.
        project = probe.get("project") or None
        try:
            raw_text = mcp_search(session_id, question, limit=MCP_RESULT_LIMIT, project=project)
        except Exception as exc:  # noqa: BLE001 -- per-probe failure is a miss, not fatal
            log.warning(
                "MISS probe id=%s question=%r -- search_memory call failed: %s",
                pid, question, exc,
            )
            misses.append({"id": pid, "question": question, "snippets": [f"error: {exc}"]})
            continue

        if is_hit(unescape_tool_result(raw_text), expect_any):
            hits += 1
        else:
            snippets = extract_result_snippets(raw_text)
            log.warning(
                "MISS probe id=%s question=%r expect_any=%s top results: %s",
                pid, question, expect_any, snippets,
            )
            misses.append({"id": pid, "question": question, "snippets": snippets})

    return {"hits": hits, "total": len(probes), "misses": misses}

# ---------------------------------------------------------------------------
# Baseline arm -- flat-markdown export + naive keyword retrieval. qdrant_client
# imported lazily so the module stays importable without it.
# ---------------------------------------------------------------------------

def _get_qdrant_client():
    from qdrant_client import QdrantClient  # noqa: E402 -- lazy
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)


def scroll_all_points(client) -> list:
    """Paginated scroll of every point (payload only -- no vectors needed)."""
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


def build_paragraphs_from_points(points: list) -> list[str]:
    """
    One paragraph per CURRENT fact: skip points whose flat `current` is
    False (missing/True == current, matching dedup_stage.py/supersede.py
    convention). Payload text lives under `data` (fallback `memory`/`text`
    for older points, mirroring extract_records() in dedup_stage.py).
    """
    paragraphs = []
    for pt in points:
        p = pt.payload or {}
        if p.get("current", True) is False:
            continue
        content = p.get("data", "") or p.get("memory", "") or p.get("text", "")
        if not content:
            continue
        subject = p.get("subject") or (p.get("metadata") or {}).get("subject") or "unknown"
        paragraphs.append(build_paragraph(subject, content))
    return paragraphs


def write_markdown_dump(paragraphs: list[str], path: str) -> None:
    """
    Best-effort write of the paragraph list to disk, one fact per paragraph
    separated by a blank line -- simulates "the same knowledge kept as flat
    markdown files" for the baseline arm.
    """
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(paragraphs))
            fh.write("\n")
        log.info("Baseline markdown dump written: %s (%d paragraphs)", path, len(paragraphs))
    except Exception as exc:  # noqa: BLE001 -- dump is observability, not correctness
        log.warning("Baseline markdown dump write failed (non-fatal): %s", exc)


def run_baseline_arm(probes: list[dict], paragraphs: list[str]) -> dict:
    """Score every probe with naive keyword retrieval over `paragraphs`."""
    hits = 0
    misses: list[dict] = []
    for probe in probes:
        pid = probe.get("id")
        question = probe.get("question", "")
        expect_any = probe.get("expect_any") or []

        terms = extract_keywords(question)
        top = top_k_paragraphs(paragraphs, terms, k=5)
        blob = "\n\n".join(top)

        if is_hit(blob, expect_any):
            hits += 1
        else:
            snippets = [snippet(p) for p in top]
            log.warning(
                "MISS(baseline) probe id=%s question=%r expect_any=%s top paragraphs: %s",
                pid, question, expect_any, snippets,
            )
            misses.append({"id": pid, "question": question, "snippets": snippets})

    return {"hits": hits, "total": len(probes), "misses": misses}

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


def push_metrics(mcp_hits: int, mcp_total: int, mcp_hit_rate: float, baseline_hit_rate: float | None) -> None:
    """Best-effort Pushgateway push, job=memory_recall. httpx imported lazily."""
    try:
        import httpx  # lazy
        now_ts = time.time()
        lines = [
            "# TYPE memory_recall_hits gauge",
            f"memory_recall_hits {mcp_hits}",
            "# TYPE memory_recall_total gauge",
            f"memory_recall_total {mcp_total}",
            "# TYPE memory_recall_hit_rate gauge",
            f"memory_recall_hit_rate {mcp_hit_rate}",
        ]
        if baseline_hit_rate is not None:
            lines += [
                "# TYPE memory_recall_baseline_hit_rate gauge",
                f"memory_recall_baseline_hit_rate {baseline_hit_rate}",
            ]
        lines += [
            "# TYPE memory_recall_last_run_timestamp_seconds gauge",
            f"memory_recall_last_run_timestamp_seconds {now_ts}",
        ]
        body = "\n".join(lines) + "\n"
        httpx.put(
            f"{PUSHGATEWAY.rstrip('/')}/metrics/job/{METRICS_JOB}",
            content=body.encode(),
            timeout=METRICS_TIMEOUT_S,
        )
        log.info(
            "Telemetry pushed: hits=%d total=%d hit_rate=%.3f baseline_hit_rate=%s",
            mcp_hits, mcp_total, mcp_hit_rate,
            f"{baseline_hit_rate:.3f}" if baseline_hit_rate is not None else "n/a",
        )
    except Exception as exc:  # noqa: BLE001 -- metrics must never break the run
        log.warning("Pushgateway push failed (non-fatal): %s", exc)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        probes = load_probes(args.probes)
    except Exception as exc:
        log.error("FATAL: could not read probe file %s: %s", args.probes, exc)
        sys.exit(1)

    if args.limit is not None:
        probes = probes[: args.limit]
    log.info("Loaded %d probes from %s", len(probes), args.probes)

    try:
        mcp_raw = run_mcp_arm(probes)
    except Exception as exc:
        log.error("FATAL: MCP unreachable at %s: %s", MCP_URL, exc)
        sys.exit(1)
    mcp_arm = shape_arm(mcp_raw["hits"], mcp_raw["total"], mcp_raw["misses"])

    baseline_arm = None
    if args.baseline:
        try:
            client = _get_qdrant_client()
            points = scroll_all_points(client)
            paragraphs = build_paragraphs_from_points(points)
            write_markdown_dump(paragraphs, BASELINE_DUMP_PATH)
            baseline_raw = run_baseline_arm(probes, paragraphs)
            baseline_arm = shape_arm(baseline_raw["hits"], baseline_raw["total"], baseline_raw["misses"])
        except Exception as exc:  # noqa: BLE001 -- baseline is comparison data, not core to the run
            log.warning("Baseline arm failed (non-fatal, skipped): %s", exc)
            baseline_arm = None

    report = build_report(run_at, len(probes), mcp_arm, baseline_arm)
    write_report(REPORT_PATH, report)

    if not args.no_metrics:
        push_metrics(
            mcp_arm["hits"], mcp_arm["total"], mcp_arm["hit_rate"],
            baseline_arm["hit_rate"] if baseline_arm else None,
        )

    baseline_summary = (
        f"{baseline_arm['hit_rate']:.3f} ({baseline_arm['hits']}/{baseline_arm['total']})"
        if baseline_arm else "n/a (--baseline not run)"
    )
    log.info(
        "Run complete -- probes=%d mcp_hit_rate=%.3f (%d/%d) baseline_hit_rate=%s",
        len(probes), mcp_arm["hit_rate"], mcp_arm["hits"], mcp_arm["total"], baseline_summary,
    )


if __name__ == "__main__":
    main()
