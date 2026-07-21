# fleet-memory — BACKLOG

Prioritized work queue. Wheel numbers refer to the failure modes in
[ADR-0001](adr/ADR-0001-memory-quality-pipeline.md). Ranked by daily impact
(recall quality and tokens spent per session), not by architectural elegance.

Last reviewed: 2026-07-21

---

## Context: where the system stands (2026-07)

Independent benchmarking of memory systems is immature — the LoCoMo/LongMemEval
numbers vendors advertise are disputed (Zep 84% → Mem0 recomputed 58.44% → Zep
counter-claimed 75.14%; an independent audit found 6.4% of the LoCoMo answer key
wrong and the LLM judge accepting up to 63% of deliberately wrong answers). Treat
published scores as marketing. The relevant consequence for us is item 3 below:
we have no score of our own either.

Against the 2026 field, fleet-memory is ahead on write-side governance (typed
claims, mandatory `why`, LLM gate, secret scan, authority check) and on
operability (Prometheus metrics, Grafana alerts, standup card, retrieval canary),
and level on supersession — our non-destructive bi-temporal lineage is the same
model Zep/Graphiti and the 2026 temporal-validity literature converged on. It is
behind on relations/graph, dedup, and aging.

mem0 itself is a replaceable storage/extraction layer here — hybrid FTS+RRF
search, lineage, gate, and namespaces are all our own code beside it. Swapping it
is not on this backlog; it would only be justified if extraction quality became
the bottleneck.

---

## Done

### Wheel #5 — shared subject canonicalizer (2026-07-21, commit 9624ab5)
`server/subject_alias.py` + `server/subject_aliases.json` are now in the repo and
are the single alias table for every writer: `server.py` (write path),
`supersede.py` (lineage grouping), `subject_backfill.py` (whose private
`CANONICAL_ALIASES` dict was deleted and merged in, ~30 entries).

Why it mattered: lineage groups facts by subject, so facts filed under `CT356`,
`memory-mcp` and `fleet-memory` formed separate groups and a contradicting fact
never met its predecessor — the stale fact stayed `current=true` forever. Two
independently maintained alias maps guaranteed that fragmentation would creep
back.

Design decisions worth keeping:
- **Flat JSON in the repo, not a DB.** Table is tiny, changes rarely, and a wrong
  alias silently merges two entities' histories — that needs git review and
  rollback, not a write API.
- Module resolves the JSON relative to its own directory (`SUBJECT_ALIASES_PATH`
  overrides); hot-reloaded every 60s, so editing aliases needs no restart.
- Lookups fall back to slugified keys, so `Sirchmunk MCP` and `sirchmunk-mcp`
  resolve identically.
- Deferred: auto-deriving the infra half of the table from `fleet.yaml` (it
  already knows `fleet-memory` = CT356 = memory-mcp). Manual entries would remain
  for non-infra entities (people, projects, tools). Worth doing when hand-editing
  becomes a chore.

Deployed to CT356 `/opt/memory-mcp` with `.bak-alias-*` backups; verified live.

---

## Next up

### 1. Wheel #4 — dedup as a reconcile-loop stage — DONE 2026-07-21

Shipped as `server/dedup_stage.py`, wired as step 3 of `reconcile.sh` (nightly
03:00 UTC timer), deployed to CT356. Judge env arrives via the
`fleet-memory-reconcile.service.d/dedup-env.conf` drop-in (mirrors the gate's
backend config). First full pass on live data: 2174 points, 243 groups, 4
candidate pairs, 4 judged (all on the local primary), 4 collapsed — all four
verified by eye as genuine rewords. 0 quarantined, 0 skipped. Metrics under
pushgateway job `memory_dedup`; per-run report at
`/var/lib/memory-stats/dedup-report.json`. Judge timeout is 180s (nightly batch
must absorb a ~90s cold model load — unlike the gate's inline 5s budget). The
`user` bucket (248 facts) exceeds DEDUP_MAX_GROUP and is skipped nightly —
wheel #6 below unblocks it. Design notes below kept for reference.

**Problem.** The store is append-only. Near-duplicate facts accumulate and crowd
the top-5 recall slots the session hook injects on every prompt, so a session
pays tokens for three phrasings of one fact instead of five distinct ones.

**Current state.** `server/dedup.py` exists in the repo but is **not deployed on
CT356 and not in `reconcile.sh`** (the loop is `subject_backfill.py` →
`supersede.py`). It is manual, destructive (real deletes), and text-similarity
only. Effectively dead code — this item is a rewrite, not a first build.

**Design.**
0. Run inside the reconcile loop, after supersede. Compare only within one
   `(user_id, canonical subject)` group — reliable now that wheel #5 landed, and
   it makes cross-subject false merges structurally impossible.
1. **Candidate pairs from existing vectors.** Nearest neighbours inside the group
   via Qdrant; pairs above ~0.90 cosine become candidates. No re-embedding, no
   API cost.
2. **Number guard.** Strip digits from both texts; if the skeletons match but the
   numbers differ, it is a value sequence, not a duplicate — quarantine, never
   touch. This encodes the 2026-05-28 sweep lesson (14 "VERSION_CODE bumped to N"
   entries clustered as dupes; auto-deleting would have kept stale values).
3. **LLM judge on survivors only** — local gemma4-12b-qat via the gate's backend
   routing, $0/year. Benchmarked 2026-07-21 (`tests/bench/`): 99/100 on 100
   hand-labeled pairs, **zero false collapses** across all value-sequence and
   near-miss traps; the one error was a missed subset-duplicate (harmless).
   gpt-4o-mini not needed.
4. **Collapse non-destructively.** Pick the cluster head by completeness (full
   text, has `why`, has `claim_type`) — explicitly **not** by newest timestamp,
   because transcript-miner-era `created_at` is inverted vs real event order.
   Mark the rest `current=false`, `superseded_by=<head id>`. Same mechanism as
   supersession: zero deletes, reversible, history intact.
5. **Report.** `memory_dedup_collapsed_total` / `_quarantined_total` to
   Pushgateway; quarantined clusters listed on the standup card so value-sequence
   cases reach human eyes.

**Safety property.** Because nothing is deleted, a wrong collapse costs one
recall miss and is undone by flipping a flag. First run ships `--dry-run` with a
report to eyeball before it joins the loop.

### 2. Wheel #6 — coarse-subject cleanup — DONE 2026-07-21 (commit 58306e0)

The `user` bucket held 266 current facts (244 from the retired
transcript-miner). `server/resubject_user_bucket.py` classified each: 17
resubjected to real entities, 26 preferences → `tomislav`, 223 retired
non-destructively (`retired_reason=wheel6-vague-subject`). A known-entity guard
proved essential: unguarded, the classifier invented ~200 singleton
micro-subjects ("gradlew", "ensuretables") — a resubject target must already
exist as a store subject or alias canonical, else retire. Prevention:
`server.py` now rejects vague subjects at write time (`MEMORY_VAGUE_SUBJECT`,
deterministic, post-canonicalization, not bypassable).

### 3. Scored recall benchmark — DONE 2026-07-21 (commit e967271)

`server/recall_bench.py` + `tests/bench/recall_probes.json` (50 probes
generated from live facts, curated). Nightly 05:00 UTC cron, pushgateway job
`memory_recall`, report `/var/lib/memory-stats/recall-bench.json`. Optional
`--baseline` arm: same facts exported to one markdown file, naive keyword
retrieval — isolates the retrieval layer.

**First scored numbers: MCP 84% hit@5, markdown baseline 90%.** Read
carefully: the baseline inherits the whole write pipeline (gate curation,
dedup, supersession resolution — it exports only resolved current facts), so
the 6-point gap is purely the retrieval layer on keyword-heavy probes, not
"markdown files are as good as the memory system". Methodology trap fixed on
first run: probes from `fleet:{project}` facts must pass `project=` like the
session hook — without it the score was 60%, a pure namespace artifact.

**Retrieval gap RESOLVED 2026-07-22 (commit 4d24011):** per-channel
instrumentation traced all 8 misses to case-fragmented project namespaces
(`project=` used raw at write+search; 152 facts invisible to their own
sessions). Slug-normalized both entry points, healed the store
(`scripts/heal_namespaces.py`). **Final: MCP 100% hit@5 vs baseline 90%.**
The suspected retrieval-layer defects (FTS sanitation, RRF bias, semantic
drift) were all red herrings — none needed fixing.

---

## Deferred / rejected

### Wheel #7 — triples / knowledge graph — REJECTED for this system
The ADR-0001 addendum already marked triples "largely OBVIATED", and re-examining
it in 2026-07 confirms the call. In this federation the structured truth lives
elsewhere: placement and edges in `fleet.yaml` (already a triple store in YAML),
code in Sirchmunk, repos in Gitea. Memory's lane is decisions, lessons and
exceptions — facts that exist *only* in memory and rarely need graph traversal.
Extracting code-derivable triples would duplicate those sources and drift.

The one benefit a graph would give that the federation does not cover is
**part-level supersession**: today one changed detail flips a whole paragraph
stale, dragging still-true details with it. If that becomes painful, build the
thin slice — have the gate (which already reads every write) emit a
`(subject, predicate)` key and reconcile on that instead of subject alone. No
graph storage, no traversal, no new retrieval channel.

If a full temporal knowledge graph is ever wanted, evaluate Graphiti (Zep's
open-source temporal KG library, self-hostable) rather than building one.

### Local embedder (fastembed)
Rejected 2026-07-19: forces a full collection reindex and changes recall quality
to address a 4–5% latency tail that parallelizing the namespace searches already
mitigated. Revisit only if OpenAI embed latency becomes a chronic problem.

### Other ADR-0001 wheels, unchanged
- **#8 aging/TTL** — transient facts live forever. Low urgency.
- **#9 provenance/confidence weighting** — source-type/recency trust ("#9-lite")
  remains one of the cheapest next steps after the items above.
- **#11 health dashboard** — partially covered by the standup card and gate
  metrics.

---

## Known drift / operational

- **GitHub mirror is behind.** `secret/saas/github api_token` is an expired classic
  PAT; pushes are rejected. Gitea is canonical. Needs a new PAT.
- **CT356 holds `.bak-*` clutter** in `/opt/memory-mcp` from successive
  hand-deploys (gate, prediction, alias). Harmless, worth a sweep.
- **Deploy path is manual** (`scp` → `pct push` → `systemctl restart`). Fine at
  this cadence; a `server/deploy.sh` would remove the chance of pushing a subset
  of files.
