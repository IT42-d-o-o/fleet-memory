"""
gate.py -- LLM write-contract gate (Feature 2, semantic, advisory-strict).

Calls an Ollama chat endpoint to judge whether a candidate memory is worth
storing long-term. Runs in add_memory AFTER all deterministic checks (secret
detector, vagueness guardrail, Rule-8 authority check) pass.

Primary/fallback (2026-07-13): the primary backend (GATE_OLLAMA_URL, e.g. the
user's workstation running a stronger model) is tried first. On a connect
error or timeout, the fallback backend (GATE_OLLAMA_FALLBACK_URL, e.g. an
always-on infra CT running a weaker but reliable model) is tried. If
GATE_OLLAMA_FALLBACK_URL is unset, behavior is single-backend, same as before
this change. Every verdict records which backend produced it ("primary",
"fallback", or "skipped" if both failed).

Pre-flight routing (2026-07-13 addendum): before calling primary, GET
{GATE_OLLAMA_URL}/api/ps to see what model (if any) is already resident on
that host. (a) our own model (GATE_LLM_MODEL) is resident -> call primary as
usual. (b) a DIFFERENT model is resident -> skip primary entirely and go
straight to fallback, so the gate never evicts whatever the user is running
for their own work (no GPU thrash). (c) nothing resident, or /api/ps itself
is unreachable/times out -> try primary as usual (pays the cold-load cost at
most once; GATE keep_alive then holds the model warm for subsequent calls).

Metrics (2026-07-13 addendum): every gate decision increments a per-backend
counter persisted at /var/lib/memory-stats/gate-counters.json and pushes
memory_gate_{primary,fallback,skipped}_total counters plus
memory_gate_last_backend and memory_gate_last_run_timestamp_seconds gauges to
the Prometheus Pushgateway (job=memory_gate). Best-effort and isolated: any
metrics failure is caught and logged, never affects the gate's return value.

FAIL-OPEN by design: any failure mode -- GATE_OLLAMA_URL unset, connection
refused, timeout, non-200, unparseable answer, and now exhaustion of both
backends -- returns outcome="skipped" and the caller proceeds to store the
memory. A missing/unreachable gate must never block writes; it is a quality
filter, not a safety gate (unlike the secret detector, which IS unbypassable
and fails CLOSED).

Unlike the secret gate, a REJECT here IS bypassable: the caller resubmits
with self_checked=true and server.py records outcome="bypassed".
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re

import httpx

log = logging.getLogger("memory-mcp.gate")

GATE_OLLAMA_URL = os.environ.get("GATE_OLLAMA_URL", "")
GATE_LLM_MODEL = os.environ.get("GATE_LLM_MODEL", "qwen3:8b")
GATE_OLLAMA_FALLBACK_URL = os.environ.get("GATE_OLLAMA_FALLBACK_URL", "")
GATE_LLM_MODEL_FALLBACK = os.environ.get("GATE_LLM_MODEL_FALLBACK", "")
GATE_LOG_PATH = os.environ.get("GATE_LOG_PATH", "/opt/memory-mcp/gate.log")
_TIMEOUT_S = float(os.environ.get("GATE_TIMEOUT", "10"))
_FALLBACK_TIMEOUT_S = float(os.environ.get("GATE_FALLBACK_TIMEOUT", "10"))
_PREFLIGHT_TIMEOUT_S = float(os.environ.get("GATE_PREFLIGHT_TIMEOUT", "2"))
_KEEP_ALIVE_PRIMARY = "-1m"  # primary is a personal workstation -- avoid cold reload between calls

_METRICS_PUSHGATEWAY_URL = os.environ.get("GATE_METRICS_PUSHGATEWAY_URL", "http://192.168.50.223:9091")
_METRICS_JOB = "memory_gate"
_METRICS_TIMEOUT_S = float(os.environ.get("GATE_METRICS_TIMEOUT", "2"))
_METRICS_STATE_DIR = "/var/lib/memory-stats"
_METRICS_STATE_PATH = os.path.join(_METRICS_STATE_DIR, "gate-counters.json")
_BACKEND_CODE = {"primary": 0, "fallback": 1, "skipped": 2}

_SYSTEM_PROMPT = (
    "You judge whether a candidate memory fact is worth storing in a long-term "
    "infrastructure memory. STORE only if it is at least one of: (a) a decision "
    "WITH its reason, (b) a lesson: failure/success with root cause, non-obvious "
    "and reusable, (c) a durable non-derivable fact: vendor quirks, credential "
    "vault path references, hardware gotchas, naming conventions, (d) a stable "
    "user preference or constraint, (e) a dated falsifiable prediction about a "
    "named system, with an expiry date and a verification hint. REJECT if it "
    "is: session narration or "
    "in-progress steps; a moment-in-time count/version/status; restating what "
    "code, config files, documentation, or an infrastructure registry already "
    "record (file paths, CLI flags, service placement, IPs, ports); generic "
    "software knowledge; or garbled/incoherent. Answer with EXACTLY one word: "
    "STORE or REJECT."
)

_RE_THINK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_think(text: str) -> str:
    return _RE_THINK.sub("", text or "").strip()


def _call_backend(
    url: str, model: str, timeout: float, content: str, keep_alive: str | None = None
) -> tuple[str | None, str, bool]:
    """Call one Ollama chat backend. Returns (verdict, raw_or_reason, transport_failed).

    verdict is "store"/"reject" on a parseable answer, else None.
    transport_failed is True only for connect-error/timeout (the cases that
    should trigger a fallback attempt), False for an unparseable-but-received
    answer (which should fail open directly, matching pre-fallback behavior).
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content or ""},
        ],
        "think": False,
        "stream": False,
        "options": {"temperature": 0},
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    api_url = url.rstrip("/") + "/api/chat"
    try:
        resp = httpx.post(api_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return None, "timeout", True
    except Exception as exc:  # noqa: BLE001 -- fail-open on any transport/parse error
        return None, f"error: {exc}", True

    raw = _strip_think((data.get("message") or {}).get("content") or "")
    answer = raw.strip().upper()
    if answer.startswith("STORE"):
        return "store", raw, False
    if answer.startswith("REJECT"):
        return "reject", raw, False
    return None, f"unparseable answer: {raw[:120]!r}", False


def _resident_models(url: str, timeout: float) -> list[str] | None:
    """Return the names of ALL models currently resident (loaded) at url, per
    Ollama's /api/ps, or None if /api/ps itself is unreachable/times out or
    the response is unparseable.

    Deliberately returns every resident name, not just the first: on hosts
    with enough VRAM/RAM, Ollama can hold more than one model resident at
    once (observed live on the workstation primary -- gemma4-12b-qat and a
    smaller model coexisting), so checking only models[0] would wrongly
    treat "our model is resident alongside someone else's" as "someone
    else's model is resident", and skip primary for no reason.

    None and [] both mean "couldn't confirm anything is resident" to the
    caller and are treated the same (fall through to trying primary, case (c)
    of the pre-flight design) -- only a non-empty list that does NOT contain
    our model means "someone else is using primary, don't evict them" (case
    (b)). Never raises."""
    try:
        resp = httpx.get(url.rstrip("/") + "/api/ps", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 -- treat any failure as "nothing resident"
        return None
    return [m.get("name") for m in (data.get("models") or []) if m.get("name")]


def _record_metric(backend: str) -> None:
    """Increment the persisted per-backend counter and push gate metrics to
    the Prometheus Pushgateway (job=memory_gate). Best-effort: any failure
    (corrupt state file, unreachable pushgateway, timeout) is caught and
    logged -- metrics are an observability side-channel and must never affect
    the gate's return value.
    """
    try:
        os.makedirs(_METRICS_STATE_DIR, exist_ok=True)
        counters = {"primary": 0, "fallback": 0, "skipped": 0}
        try:
            with open(_METRICS_STATE_PATH, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            for key in counters:
                counters[key] = int(loaded.get(key, 0))
        except Exception:
            pass  # missing/corrupt state file -- start from zero
        counters[backend] = counters.get(backend, 0) + 1

        tmp_path = _METRICS_STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(counters, fh)
        os.replace(tmp_path, _METRICS_STATE_PATH)

        now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
        body = (
            "# TYPE memory_gate_primary_total counter\n"
            f"memory_gate_primary_total {counters['primary']}\n"
            "# TYPE memory_gate_fallback_total counter\n"
            f"memory_gate_fallback_total {counters['fallback']}\n"
            "# TYPE memory_gate_skipped_total counter\n"
            f"memory_gate_skipped_total {counters['skipped']}\n"
            "# TYPE memory_gate_last_backend gauge\n"
            f"memory_gate_last_backend {_BACKEND_CODE.get(backend, 2)}\n"
            "# TYPE memory_gate_last_run_timestamp_seconds gauge\n"
            f"memory_gate_last_run_timestamp_seconds {now_ts}\n"
        )
        httpx.put(
            f"{_METRICS_PUSHGATEWAY_URL.rstrip('/')}/metrics/job/{_METRICS_JOB}",
            content=body,
            timeout=_METRICS_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 -- metrics must never break the gate
        log.warning("gate: metrics push failed: %s", exc)


def evaluate(content: str) -> tuple[str, str, str]:
    """Return (verdict, raw_answer, backend).

    verdict is one of: "store", "reject", "skipped".
    backend is one of: "primary", "fallback", "skipped" -- which backend
    produced the verdict, or "skipped" if neither backend was usable.
    raw_answer is the model's (think-stripped) text, or a short reason string
    when verdict == "skipped" (e.g. "GATE_OLLAMA_URL unset", "timeout").
    Never raises -- every failure mode maps to ("skipped", <reason>, "skipped").
    """
    if not GATE_OLLAMA_URL:
        _record_metric("skipped")
        return "skipped", "GATE_OLLAMA_URL unset", "skipped"

    resident = _resident_models(GATE_OLLAMA_URL, _PREFLIGHT_TIMEOUT_S)
    if resident and GATE_LLM_MODEL not in resident:
        # (b) primary is busy serving a different model (and not also ours)
        # -- do not evict it.
        log.info(
            "gate: pre-flight found %r resident on primary (want %r) -- skipping primary to avoid thrash",
            resident, GATE_LLM_MODEL,
        )
        verdict, raw, transport_failed = None, f"pre-flight: primary busy with {resident!r}", True
    else:
        # (a) our model is resident (possibly alongside others), or (c)
        # nothing resident / /api/ps unreachable -- try primary as usual.
        verdict, raw, transport_failed = _call_backend(
            GATE_OLLAMA_URL, GATE_LLM_MODEL, _TIMEOUT_S, content, keep_alive=_KEEP_ALIVE_PRIMARY
        )

    if verdict is not None:
        _record_metric("primary")
        return verdict, raw, "primary"
    if not transport_failed:
        log.warning("gate: unparseable answer from primary %r -- fail-open (skipped)", raw[:120])
        _record_metric("skipped")
        return "skipped", raw, "skipped"

    log.warning(
        "gate: primary backend (%s) failed (%s)%s",
        GATE_OLLAMA_URL, raw,
        " -- trying fallback" if GATE_OLLAMA_FALLBACK_URL else " -- fail-open (skipped)",
    )
    if not GATE_OLLAMA_FALLBACK_URL:
        _record_metric("skipped")
        return "skipped", raw, "skipped"

    fb_verdict, fb_raw, _fb_transport_failed = _call_backend(
        GATE_OLLAMA_FALLBACK_URL, GATE_LLM_MODEL_FALLBACK or GATE_LLM_MODEL, _FALLBACK_TIMEOUT_S, content
    )
    if fb_verdict is not None:
        _record_metric("fallback")
        return fb_verdict, fb_raw, "fallback"

    log.warning(
        "gate: fallback backend (%s) also failed (%s) -- fail-open (skipped)",
        GATE_OLLAMA_FALLBACK_URL, fb_raw,
    )
    _record_metric("skipped")
    return "skipped", f"primary: {raw}; fallback: {fb_raw}", "skipped"


def append_log(outcome: str, subject: str | None, content: str, backend: str = "skipped") -> None:
    """Append one JSON line per gated write to gate.log (Feature 3).

    outcome: one of passed|rejected|bypassed|skipped -- the LLM gate's outcome
      enum only. Authority conflicts and secret-gate blocks are NOT logged here
      (secret content must never be logged; authority conflicts are rejected
      before the LLM gate even runs). Content is always truncated to 80 chars,
      even for rejected writes -- never log the full candidate. Best-effort:
      a logging failure must never break add_memory.
    backend: one of primary|fallback|skipped (2026-07-13 primary/fallback
      design) -- which Ollama backend produced the gate's verdict.
    """
    try:
        line = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "outcome": outcome,
            "backend": backend,
            "subject": subject,
            "content_first80chars": (content or "")[:80],
        }
        with open(GATE_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never break add_memory
        log.warning("gate.log append failed: %s", exc)
