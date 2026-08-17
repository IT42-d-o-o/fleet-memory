# Retrieval evaluation harness

Tools for answering "does this retrieval change actually help?" against **real
captured traffic** rather than a hand-written probe set.

Built 2026-08-13/14 while evaluating the `per_hit` fusion strategy. That
evaluation rejected the change — see [FINDINGS-2026-08.md](FINDINGS-2026-08.md).
Three separate hypotheses died here cheaply instead of shipping, which is what
this directory is for.

## Where these run

On the memory-service host, against the deployed service in `/opt/memory-mcp`.
They import `server.py` directly (it is import-safe — uvicorn only runs under
`__main__`) to reuse the live `memory`, `fts` and `_resolve_supersession`
objects, so they exercise the same code path production does.

Each script inherits the running server's environment straight from `/proc`
(Vault-sourced API keys included), so **no secret is ever passed as an argument,
written to a file, or printed**. They also unset `MEM0_QUERY_CAPTURE` so an
evaluation run can never pollute the capture it is reading.

```bash
cd /opt/memory-mcp && ./venv/bin/python <script>.py
```

Data sources: the dark query capture (`MEM0_QUERY_CAPTURE`, JSONL) and the
session hook's own `recall-log.jsonl`.

## Method rules — each one learned by getting it wrong

1. **Pair the arms.** Retrieve once per query, then merge those *identical*
   lists under every strategy. Both arms then see the same corpus at the same
   instant, and no production restart is needed. Re-retrieving per arm confounds
   the strategy with corpus drift.

2. **Never use historically captured result ids as the baseline.** They reflect
   the corpus as it was at capture time. Measured drift was 33% in one week —
   easily large enough to swamp the effect under test. Use them to *measure*
   drift, nothing else.

3. **Compare what the caller actually sees.** Post-supersession, post-trim.
   Supersession can collapse two different id sets into the same final answer,
   so comparing raw fusion output inflates the divergence rate with differences
   nobody observes.

4. **An LLM judge forced to pick a winner manufactures signal.** The local judge
   scored one arm at 62.9% of decisive pairs; a human given the same 60 pairs
   called 39 of them ties and the effect vanished (p = 0.82). Of those 39 ties,
   the model had called 14 in favour of one arm. Always:
   - swap A/B positions and count only order-consistent verdicts as decisive;
   - **also** report the half-win score including ties and inconsistents —
     dropping inconsistent pairs keeps only easy, high-margin comparisons and
     inflates separation (62.9% → 57.5% here);
   - treat a high inconsistency rate (37% here) as disqualifying for a
     production decision, and as evidence the pairs are genuinely equivalent.

5. **Split by consumer before believing any number.** The session hook applies a
   score threshold; direct `search_memory` callers read all rows and apply none.
   A metric defined by that threshold is meaningless for the second group.
   Measuring both together overstated one benefit ~3x. Join captured queries
   against `recall-log.jsonl` to classify them instead of guessing at ratios.

Two standing cautions: the labeled probe set saturated at 100% under both arms
and can now only certify "no regression", never justify a change; and a strategy
that bundles several mechanisms (filtering + deletion + rank compaction) cannot
attribute its own result — give each mechanism its own arm.

## Scripts

| script | answers |
|---|---|
| `shadow_eval.py` | Paired A/B of fusion strategies over captured traffic: admission regime, blast radius, direction split. `--probes-only` scores a labeled probe set through the same paired path. |
| `judge_diverged.py` | Blinded, position-swapped LLM adjudication of diverged pairs, plus a query-class split (identifier-bearing vs prose). |
| `export_pairs.py` | Exports the judged pairs for **human** grading, re-blinded with a seed independent of the model judge's, inconsistent pairs first. |
| `make_grader.py` | Generates a self-contained blind grading page (keyboard voting, localStorage persistence, answer key withheld from the browser). |
| `fts_worth.py` | Does a retrieval arm earn its slot? Top-5 occupancy, and injectable rows displaced vs gained against a threshold-filtering consumer. |
| `recovered_quality.py` | Score distribution of rows a change recovers, split hook vs agent traffic. Answers "is the gain real or is it all marginal?" |

## Typical sequence

```
shadow_eval.py           # is there an effect, and how big
judge_diverged.py        # cheap first-pass adjudication (treat as advisory)
export_pairs.py          # → make_grader.py → human grades → decisive answer
recovered_quality.py     # is the surviving gain worth shipping
```

Stop at any point the answer is "no measurable difference". That outcome is
worth as much as a positive one and costs far less than shipping.
