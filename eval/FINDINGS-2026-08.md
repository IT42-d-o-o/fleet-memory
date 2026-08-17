# Shadow eval: `classic` vs `per_hit` RRF fusion — 2026-08-13

Production state at time of eval: `MEM0_FUSION_STRATEGY` **unset** on CT356 → `classic`.
Only the dark query capture is live; no retrieval behaviour has been changed.

## Method

Paired, offline, no production restart. For each captured query the semantic
(mem0/Qdrant) and keyword (FTS5 BM25) lists are retrieved **once**, then merged
under both strategies from those identical inputs, resolved through
`_resolve_supersession`, trimmed to top-5. Both arms therefore see the same
corpus at the same instant; strategy is the only variable.

The captured `top5_ids` are **not** used as the classic baseline — they reflect
the corpus as it was at capture time, so comparing them against a per_hit run
today would conflate strategy delta with corpus drift. They are used only to
measure that drift (33% of queries changed answer since capture).

Population: 635 capture records → 348 distinct (query, namespace) pairs.

## Result 1 — `per_hit` is a BM25 kill switch on real traffic

| metric | value |
|---|---|
| mean keyword-hit admission rate | **6.4%** |
| queries admitting **zero** keyword hits | **290/348 (83.3%)** |
| queries admitting everything | 1/348 (0.3%) |
| top-5 changed | **322/348 (92.5%)** |

By query class:

| class | n | mean query len | mean admission | diverged |
|---|---|---|---|---|
| identifier-bearing | 182 | 172 chars | 0.119 | 89% |
| prose (no identifier) | 166 | 63 chars | 0.004 | 96% |

Even among identifier-bearing queries, 127 admitted nothing at all.

**Cause.** `per_hit` assumes queries are identifier-bearing searches. The capture
is dominated by the `UserPromptSubmit` hook forwarding the user's raw prompt, so
the "queries" are conversational: *"i am going to sleep. deploy when ready"*,
*"i can't move ship with one finger"*. Identifiers extracted from those are
artefacts — `'f9'`, `'1:57'`, `'50ms'`, `'4columns'`, `'300'`. Nothing in the
corpus contains them, so every keyword vote is dropped and fusion silently
degenerates to semantic-only.

## Result 2 — the labeled probe set cannot decide this

`recall_probes.json` (50 probes): **classic 50/50, per_hit 50/50.** Fully
saturated, zero discriminating power. It can certify "no labeled regression"
and nothing more. This is the same trap that let `gated_coverage` pass before
external review falsified it.

## Result 3 — blinded judge: the gain comes from the wrong place

Local gemma (non-Claude, same model already trusted as the dedup gate), 60
randomly sampled diverged queries, arms blinded as A/B, assignment randomised,
**every pair judged twice with positions swapped**; only order-consistent
verdicts count.

Headline: per_hit 22, classic 13, tie 3, **inconsistent 22 (37%)**.
Decisive 22/35 = 62.9%, two-sided binomial **p = 0.18 — not significant**.

**Correction (external review).** Reporting only the order-consistent rate is
not neutral: dropping inconsistent pairs selects high-margin, judge-easy
comparisons and inflates separation from 50%. Scoring inconsistent pairs and
ties as half a win gives per_hit `(22 + 22/2 + 3/2)/60` = **57.5%**, materially
weaker than 62.9%. Both numbers belong in any future report.

Conditioning on whether the feature actually fired inverts the story:

| subgroup | n | per_hit | classic | p |
|---|---|---|---|---|
| `kw_admitted == 0` (BM25 fully off) | 31 decisive | **22** | 9 | **0.029** |
| `kw_admitted > 0` (admission actually fires) | 4 decisive | **0** | 4 | 0.125 |

The benefit is concentrated entirely in the cases where `per_hit` degenerates to
semantic-only. In the cases where the novel identifier-admission logic actually
does its job, it lost every decisive comparison.

Caveats, stated plainly: this is a **post-hoc subgroup split**, so it is
hypothesis-generating, not confirmatory. The second cell is n=4. The judge
contradicts itself under order swap 37% of the time, which is high.

## Reading

The measured gain is not evidence for identifier-aware fusion. It is evidence
that **keyword fusion is hurting on this traffic**, and `per_hit` happens to
disable it 83% of the time as a side effect. Shipping `per_hit` would ship
"semantic-only retrieval" under a name that describes something else, keeping a
body of admission code that, where it engages, measured worse than classic.

## Secondary defect (independent of the above)

`query_has_identifier()` and `identifier_tokens()` disagree on what an
identifier is — the former is a loose character-class test, the latter a strict
grammar. Queries therefore report "has identifier" while extracting none.
`per_hit` is unaffected (it uses `identifier_tokens`), but the `gated` and
`gated_coverage` strategies gate on the loose test and are affected.

## External review (sol, GPT-5.x — non-Claude second opinion)

Verdict: **retain `classic`, make no production change.** Additional findings
that changed the conclusion rather than confirming it:

- **Judge-exclusion bias** — see correction above (62.9% → 57.5%).
- **Multiplicity** — even a minimal Bonferroni correction over the two reported
  subgroup tests moves p = 0.029 to **0.058**. True multiplicity is unknown
  because the split was chosen after seeing the result.
- **`per_hit` confounds three mechanisms** — keyword deletion, identifier
  filtering, and rank compaction — so no outcome can be attributed to any one
  of them. Compaction is quantitatively significant on its own: a hit moving
  from rank 12 to rank 0 gains 1/72 → 1/60 (+20%), roughly ten times the gap
  between adjacent top-of-list ranks, enough to move the top-5 boundary.
  Future evals need four arms: classic, semantic-only, filter-without-
  compaction, filter-with-compaction.
- **Cheapest decisive next measurement** is a *human* blind grading of the 60
  pairs already produced — no new retrieval, no deploy. Prioritise the 22
  inconsistent pairs, which are numerous enough to reverse the conclusion.
  Another model-judge run would not resolve the central uncertainty.
- **Conditioning caveats** — results are conditional on divergence (unchanged
  queries must count as ties for a whole-traffic estimate), and the 635 → 348
  dedup estimates performance over *unique* queries, not traffic frequency.
  Both framings should be reported.
- **Instrumentation catch** — the "prose (no identifier)" class shows nonzero
  mean admission (0.004, ≈3 queries), which contradicts the stated rule. Sol
  derived this from the numbers alone; it independently corroborates the
  `query_has_identifier` / `identifier_tokens` defect below and shows the
  disagreement runs in *both* directions.

## Result 4 — human blind grading (the decisive measurement)

Same 60 pairs, graded by the operator. Blinded A/B with a seed **independent**
of the model judge's, so the two graders' layouts are uncorrelated. The 22
model-inconsistent pairs were presented first. One skip.

**Verdicts: per_hit 9, classic 11, tie 39, skip 1.**

| slice | n | per_hit | classic | tie | decisive | p | half-win |
|---|---|---|---|---|---|---|---|
| all | 60 | 9 | 11 | 39 | 45.0% | 0.82 | 48.3% |
| first 22 (judge-inconsistent) | 22 | 2 | 2 | 18 | 50.0% | 1.00 | 50.0% |
| remaining 38 | 38 | 7 | 9 | 21 | 43.8% | 0.80 | 47.3% |
| `kw_admitted == 0` | 54 | 9 | 7 | 37 | 56.2% | 0.80 | 51.9% |
| `kw_admitted > 0` | 6 | **0** | **4** | 2 | 0.0% | 0.125 | 16.7% |

Three conclusions, in descending order of confidence:

1. **`per_hit` has no measurable benefit.** 39 of 60 pairs are ties; the
   decisive split is 9–11 *against* it, p = 0.82. The earlier 57.5–62.9% signal
   does not survive human adjudication.
2. **The model judge's lead was an artifact of forced choice.** Of the 39 pairs
   the human called ties, the model called **14 "per_hit better"** and only 5
   "classic better". Forcing a verdict on genuinely equivalent pairs generated
   the entire apparent advantage.
3. **The one consistent signal is negative.** In the `kw_admitted > 0` cell —
   where the identifier-admission feature actually fires — the human scored
   0–4 for classic, exactly matching the model judge's independent 0–4. Two
   uncorrelated graders agreeing on direction is worth more than either n.

Confirmation of the earlier prediction: the 22 pairs the model contradicted
itself on came back **18 ties and 2–2**. Its instability there was signal, not
position noise — those result sets really are equivalent.

Caveat that cuts against the tie count: grading was set-level with shared rows
dimmed, which is coarser than item-level relevance and plausibly inflates ties.

## Verdict

**Keep `classic`. Do not ship `per_hit`, and delete the admission path rather
than leaving it dormant** — it is the only component with a measured negative
signal, and it confounds three mechanisms (see external review).

The deeper finding is that **65% of top-5 result sets are interchangeable on
this traffic**. Fusion-layer tuning is near the noise floor; the leverage is
upstream, in what gets sent as the query.

## Options

1. **Do nothing.** Keep `classic`. Nothing is live; nothing is at risk.
2. **Test the real hypothesis directly** — semantic-only, or keyword at reduced
   weight — as its own arm. Same harness, one more strategy. This is where the
   measured gain actually lives, and it is far simpler than the admission code.
3. **Fix the root cause: query quality.** Sending a whole conversational prompt
   as a retrieval query is the actual defect; both arms are working on degraded
   input. Extracting salient terms, or skipping retrieval for non-informational
   prompts, would likely dominate any fusion tweak.
4. Fix the `query_has_identifier` / `identifier_tokens` inconsistency regardless.

Harnesses: `shadow_eval.py`, `judge_diverged.py`, `admit_diag.py` (on CT356 at
`/opt/memory-mcp/`, sources in session scratchpad). Per-query rows:
`/tmp/shadow_full.jsonl`, verdicts `/tmp/judge_verdicts.jsonl`.
