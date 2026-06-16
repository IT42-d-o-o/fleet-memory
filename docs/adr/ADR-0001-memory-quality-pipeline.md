# ADR-0001: Memory Quality as a Pipeline with Staged Quality Gates

- **Status:** Accepted (lineage loop shipped; remaining gates planned)
- **Date:** 2026-06-16
- **Deciders:** Tomislav Balaz, Claude (Opus 4.8)
- **Repo:** `ai/fleet-memory`
- **Supersedes/relates:** issue #1 (FTS5 hybrid + write guardrail), PR #2 (supersession lineage)

## Context

fleet-memory is an mem0/Qdrant-backed shared memory exposed as an MCP server
(`memory-mcp`, CT356, streamable-HTTP `:8800/mcp`). It is read by every agent in
the fleet, so a wrong, stale, leaked, or duplicated memory does not fail locally —
it silently misleads every future session.

Investigation (2026-06-16) showed retrieval quality cannot be fixed by tuning
search alone. Memory degrades through a **lifecycle**, and each stage has a
distinct failure mode ("poison"). Fixing only supersession leaves the other
wheels unguarded. The optimal solution treats memory as a **data pipeline with a
quality gate at each stage plus a health gauge**, not as a single search index.

Two foundational facts about the deployment constrain every decision:

1. **The write path is intentionally KEYLESS.** `add_memory` stores content
   verbatim with `infer=False`; no LLM runs at write time (no secret on the box,
   zero per-write cost, deterministic, always-fast). A deterministic non-LLM
   write guardrail (`validate.py`) already screens for vague/context-dependent
   candidates. Any new write-time gate must also be deterministic/cheap.
2. **mem0 stores metadata FLAT.** Qdrant payloads keep metadata keys
   (`category`, `source`, `subject`, `current`, …) as top-level fields; mem0
   reassembles them into a nested `metadata` dict only in search results.
   Maintenance scripts therefore `set_payload` flat keys, never a nested dict.

## Decision

Model fleet-memory as a pipeline:

```
WRITE → NORMALIZE → DEDUP → RECONCILE → RETRIEVE → AGE
```

Each stage carries a quality gate. Writes are guarded synchronously and cheaply
(keyless). Expensive, LLM-assisted quality work (subject judging, supersession,
near-dup clustering) runs **asynchronously** in a background reconcile loop, never
on the write path. A health dashboard observes the whole pipeline.

### Poison taxonomy (the wheels)

| # | Poison | Stage | Status |
|---|--------|-------|--------|
| 1 | Stale/contradicting fact | reconcile | **Shipped** |
| 2 | False/hallucinated fact as truth | write | Open |
| 3 | Secret leaked into a memory | write | Open |
| 4 | Duplication bloat (crowds top-k) | dedup | Partial (manual, text-only) |
| 5 | Subject canonicalization not shared across writers | normalize | Partial |
| 6 | Coarse/junk subjects (`user` 841-bucket) | normalize | Partial |
| 7 | Non-atomic blobs (no triples) | write | Open |
| 8 | No aging/TTL (transient facts live forever) | age | Open |
| 9 | No provenance/confidence weighting | write/retrieve | Open |
| 10 | Mis-scoped namespace | write | Partial (`reclassify.py`) |
| 11 | No health observability | cross-cut | Open |
| 12 | Lazy live writes (missing subject) | write | Partial |

## Shipped and LOCKED (supersession lineage — wheel #1)

Live on CT356 as of 2026-06-16 (PR #2, branch `feat/memory-lineage`):

- **Phase 1 — `subject_backfill.py`:** gpt-4o-mini assigns a canonical,
  alias-normalized `subject` to each point (flat key; `set_payload` merge
  preserves `category`/`source`). Idempotent.
- **Phase 2 — `supersede.py`:** groups points by `subject` **across namespaces**,
  gpt-4o-mini clusters same-attribute claim threads, writes flat
  `current` / `superseded_by` / `supersedes` / `valid_from` ordered by
  `created_at`. Non-destructive (history retained).
- **Phase 3 — `server.py` `search_memory`:** over-fetches 3×, swaps each stale
  hit for its current head, dedupes, trims. `include_superseded=true` returns
  history. `_fetch_record` reassembles the flat payload to the search-hit shape.
- **Phase 4 — nightly timer:** `reconcile.sh` (backfill → supersede) via
  `fleet-memory-reconcile.{service,timer}`, 03:00 nightly, `Persistent` catch-up.

Verified end-to-end: a query that semantically matched the stale fact
`b3bc1ab0` ("DB at /opt/infraatlas/publish") returned its current head
`c184d4d5` with `superseded_from` set; superseded ids absent by default, present
with `include_superseded=true`.

### Locked design decisions (rationale)

- **Keyless write path stays keyless.** No LLM on `add_memory`. Coupling the
  must-always-succeed write op to an external LLM adds latency and a failure mode.
- **Supersession is async, never inline.** The writing session already has its
  fact in context, so the lineage flip only matters for the *next* session.
  Compute it after the writes, not per write.
- **"Dirty" is derived from `created_at`, not tracked.** A reconcile judges only
  subjects with points newer than `last_run` — no write-path bookkeeping.
- **Reconcile cadence is a dial, not a new component.** Session-close (fast path,
  Claude sessions) and nightly (safety net, all writers + drift correction) are
  the same script at different triggers.
- **Judge is conservative (split-on-doubt).** Burying a valid fact is worse than
  keeping a stale one retrievable. Accepts some missed merges
  (`/publish` vs `/infraatlas-data` left unlinked) to avoid false supersession.
- **Oversized subject groups (>80) are skipped, not truncated.** Logged, marked
  all-current. Generic buckets like `user` carry no real supersession.

### In-flight (decided, not yet built) — the capture/reconcile loop

1. **Live write + `subject` mandate** in user `CLAUDE.md` and agent templates —
   capture and tag in real time; shrinks what the loop must do.
2. **`supersede.py --since`** (timestamp state file) + lockfile (serialize
   concurrent reconciles).
3. **Claude Code `SessionEnd` hook** → background-trigger `--since` reconcile on
   CT356 (non-blocking).
4. **Nightly timer retained** as the backstop for non-Claude writers and crashes.

## Roadmap — remaining wheels, in risk×likelihood order

1. **#3 Secret-scan on write (HIGH).** Deterministic regex/entropy detector in
   `add_memory`; reject + self-check on hit. Keyless. Highest security value.
2. **#5 Shared canonicalizer (MED-HIGH).** One subject-normalization module
   imported by `add_memory`, `subject_backfill.py`, `supersede.py`. Kills group
   fragmentation that undermines lineage.
3. **#4 Dedup as a loop stage (MED-HIGH).** Vector-cluster near-dup collapse
   folded into the reconcile loop (not just `SequenceMatcher`).
4. **#11 Health dashboard (MED).** Grafana panel: total, subjectless count,
   dup-rate, %stale, contradictions/day, writes/day by source. Satisfies the
   "if it's not in Prometheus, it doesn't exist" standard.
5. **#9 Provenance/confidence (MED).** Store source + corroboration + confidence;
   weight retrieval and supersession by it. Defense against false facts.
6. **#7 Atomicity/triples (MED).** Extract `(subject, predicate, object,
   qualifier)` at write time to enable aggregation/reverse/taxonomy queries and
   part-level supersession.
7. **#8 Aging/TTL (MED).** Optional expiry for temporally-scoped facts; archive
   (not delete) long-superseded points.

## Consequences

**Positive**
- Single coherent model; every poison maps to a named stage and gate.
- Write path stays fast, keyless, and reliable.
- Quality work is async and idempotent — safe to re-run, observable.
- Incremental: each wheel ships independently behind the same loop + dashboard.

**Negative / costs**
- Background loop carries recurring gpt-4o-mini cost (bounded; cents/day).
- Conservative judge leaves residual un-merged contradictions.
- Session-close fast path only covers Claude Code sessions; others rely on nightly.
- Full pipeline is multi-phase work, not a single change.

## Alternatives considered and rejected

- **Inline write-time supersession judge.** Rejected: poisons the keyless,
  must-always-succeed write path with LLM latency + failure; re-judges per write
  (10–100× cost on hot subjects); greedy/local, less correct than batch; would
  still need a periodic full reconcile to fix drift.
- **Drop the nightly once live-writing exists.** Rejected: live `add_memory`
  handles *capture+tag*, not the *supersession flip* of the older fact (keyless
  server cannot judge a group at single-write time). Nightly remains the backstop
  for non-Claude writers, crashed sessions, and incremental drift.
- **Search-only tuning (RRF weights, more recall).** Rejected: cannot compute
  supersession, aggregation, or taxonomy from cosine similarity; the answer
  ceiling is in the data model, not the ranker.

---

## Addendum (2026-06-16): fleet-memory is ONE lane in a federation — revises scope and #7

**Trigger.** Live-testing attribute/aggregate questions ("what DB does InfraAtlas
use?", "what DBs are used across the ai org?") exposed that we were treating
fleet-memory as the universal knowledge store. It is not. The forward case worked
via query-time LLM over retrieved candidates (SQLite was in the top-5 and
extracted correctly); the aggregate case failed (recall-biased to Postgres, missed
the SQLite outlier) — but the real lesson is that **aggregate question belongs to a
different store entirely.** Sirchmunk (semantic code search) was overlooked.

**Three sources of truth — route by which owns the answer:**

| Question type | Authoritative store |
|---|---|
| What exists / enumerate (repos, orgs) | **Gitea** (API) |
| What a repo uses — DB engine, stack, deps (code-derivable) | **Sirchmunk / the code** (CT336, semantic index over `/opt/source`) |
| Why / decision / runtime / context NOT in code | **fleet-memory** |

**Decisions:**

1. **fleet-memory's lane is narrowed.** It owns the *why* — decisions, runtime/infra
   facts, cross-session context that is NOT derivable from code and NOT enumerable
   from Gitea. Do NOT store code-derivable facts (a repo's DB/stack/deps) as primary
   truth in memory — they drift from the source. Memory may hold the *exception/
   decision* about them ("InfraAtlas deliberately keeps SQLite, do not migrate to
   Postgres") because that *why* is not in the code.
2. **Multi-hop / aggregate questions are FEDERATED orchestration, not a memory
   feature.** "What DB across the ai org" = Gitea enumerate -> Sirchmunk per-repo ->
   LLM compose, with memory supplying only exceptions/decisions.
3. **Revises #7 (triples / attribute dimension): largely OBVIATED.** Code is the
   structured truth and Sirchmunk already indexes it; extracting code-derivable
   triples into memory would duplicate and drift from the real source. Forward/
   reverse attribute questions are served by query-time LLM over retrieved
   candidates (verified live). A stored controlled-vocabulary attribute dimension is
   justified ONLY for enumerate-over-facts-that-exist-solely-in-memory — rare; defer
   until a real, frequent need appears.
4. **New roadmap item supersedes most of #7 — a thin QUERY ROUTER (federation front
   door):** classify the question and dispatch — enumerate -> Gitea; code attribute
   -> Sirchmunk; why/decision -> fleet-memory; multi-hop -> orchestrate the above ->
   LLM compose. Requires Sirchmunk wired into the agent toolset
   (192.168.50.136:8765/mcp).

**Consequence for the remaining wheels.** Because memory's lane is narrower, the
quality wheels shrink: #5 canonicalizer, #4 dedup, #8 TTL only need to be as good as
the narrowed *why/decision* lane requires, not universal-KB quality. #7 is retired
in favour of the query router + Sirchmunk. #11 (health) and the source-type/recency
trust idea (#9-lite) are unchanged and remain the cheapest next steps.
