#!/usr/bin/env python3
"""Shadow eval: classic vs per_hit fusion on REAL captured traffic.

Paired design. For each captured query we retrieve semantic + keyword ONCE,
then merge those identical lists under both strategies. Both arms therefore
see the same corpus at the same instant -- the strategy is the only variable.

The captured `top5_ids` are NOT used as the classic baseline: they reflect the
corpus as it was at capture time, so comparing them to a per_hit run today
would conflate strategy delta with corpus drift. They are used only to
MEASURE that drift, reported separately.

Comparison happens on the post-supersession, post-trim top-5 -- what a caller
actually sees. Supersession can collapse two differing id sets into the same
final answer; measuring raw rrf_merge output would inflate divergence with
differences nobody observes.

Run on CT356:
  /opt/memory-mcp/venv/bin/python shadow_eval.py --out /tmp/shadow_eval.jsonl
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, "/opt/memory-mcp")

CAPTURE = "/opt/memory-mcp/query-capture.jsonl"
LIMIT = 5


def inherit_env_from_server():
    """Adopt the running server's environment (Vault-sourced OPENAI_API_KEY etc).

    Read straight from /proc into os.environ: no secret is ever passed as a
    shell argument, written to a file, or printed. Only names are reported.
    """
    import glob
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            cmd = open(path, "rb").read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "server.py" not in cmd or "memory-mcp" not in cmd:
            continue
        pid = path.split("/")[2]
        try:
            blob = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", "ignore")
        except Exception:
            continue
        names = []
        for kv in blob.split("\0"):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            if k and k[0].isalpha() and k.upper() == k and k not in os.environ:
                os.environ[k] = v
                names.append(k)
        return pid, names
    return None, []


def load_queries(path, limit=None):
    """Distinct (query, namespaces) pairs, most recent occurrence wins."""
    seen = {}
    order = []
    bad = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            bad += 1
            continue
        q = (rec.get("query") or "").strip()
        if not q:
            continue
        key = (q, tuple(rec.get("namespaces") or ["fleet"]))
        if key not in seen:
            order.append(key)
        seen[key] = rec  # keep latest capture for drift comparison
    out = [(k, seen[k]) for k in order]
    if limit:
        out = out[:limit]
    return out, bad


def retrieve(server, query, ns, fetch_n):
    """Semantic + keyword lists, exactly as search_memory builds them."""
    seen_ids, semantic = set(), []
    for n in ns:
        r = server.memory.search(query, filters={"user_id": n}, limit=fetch_n)
        for item in r.get("results", []):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                semantic.append(item)
    semantic.sort(key=lambda x: x.get("score", 0), reverse=True)
    return semantic, server.fts.search(query, ns, fetch_n)


def score_probes(server, path):
    """Paired labeled bench: same retrieval, both merges. Regression floor only.

    A probe HITS when any expect_any substring appears in the concatenated
    text of the top-5 -- identical rule to recall_bench.py, but computed
    offline on both arms from one retrieval so production is never restarted.
    """
    from fts_index import rrf_merge
    probes = json.load(open(path, encoding="utf-8"))
    fetch_n = LIMIT * 3
    res = {"classic": 0, "per_hit": 0}
    flips = []
    for p in probes:
        q = p.get("question") or ""
        ns = ["fleet"]
        if p.get("project"):
            # same slug normalization search_memory applies before namespacing
            slug = server.subject_alias.slugify(p["project"])
            ns = [f"fleet:{slug}", "fleet"]
        try:
            semantic, keyword = retrieve(server, q, ns, fetch_n)
        except Exception as exc:
            print(f"  probe {p.get('id')} retrieval fail: {type(exc).__name__}")
            continue
        hit = {}
        for strat in ("classic", "per_hit"):
            merged = rrf_merge(list(semantic), list(keyword), fetch_n,
                               strategy=strat, query=q)
            rows = server._resolve_supersession(merged, False)[:LIMIT]
            blob = " ".join(str(r.get("memory") or "") for r in rows).lower()
            hit[strat] = any(s.lower() in blob for s in (p.get("expect_any") or []))
            res[strat] += hit[strat]
        if hit["classic"] != hit["per_hit"]:
            flips.append({"id": p.get("id"), "q": q,
                          "classic": hit["classic"], "per_hit": hit["per_hit"]})
    n = len(probes) or 1
    print("\n================ LABELED PROBE BENCH (regression floor) ================")
    print(f"probes {len(probes)}")
    print(f"  classic  hit@5  {res['classic']}/{len(probes)}  ({100*res['classic']/n:.1f}%)")
    print(f"  per_hit  hit@5  {res['per_hit']}/{len(probes)}  ({100*res['per_hit']/n:.1f}%)")
    for f in flips:
        d = "WON" if f["per_hit"] else "LOST"
        print(f"  {d} probe {f['id']}: {f['q'][:80]}")
    return res, flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/shadow_eval.jsonl")
    ap.add_argument("--capture", default=CAPTURE)
    ap.add_argument("--limit-queries", type=int, default=0)
    ap.add_argument("--probes-only", default="")
    args = ap.parse_args()

    if args.probes_only:
        pid, names = inherit_env_from_server()
        print(f"env inherited from server pid={pid}: {len(names)} vars", flush=True)
        os.environ.pop("MEM0_QUERY_CAPTURE", None)
        import server
        score_probes(server, args.probes_only)
        return

    pid, names = inherit_env_from_server()
    print(f"env inherited from server pid={pid}: {len(names)} vars "
          f"({'OPENAI_API_KEY' in names and 'key present' or 'NO KEY'})", flush=True)
    os.environ.pop("MEM0_QUERY_CAPTURE", None)  # never append to the live capture

    pairs, bad = load_queries(args.capture, args.limit_queries or None)
    print(f"distinct (query,ns) pairs: {len(pairs)}  unparseable lines: {bad}", flush=True)

    import server  # module-level `memory` + `fts`; uvicorn only under __main__
    from fts_index import admit_keyword_hits, rrf_merge

    fetch_n = LIMIT * 3
    rows = []
    stats = collections.Counter()
    admit_ratios = []

    for i, ((query, namespaces), rec) in enumerate(pairs, 1):
        ns = list(namespaces)
        try:
            # --- retrieve ONCE, exactly as search_memory does -------------
            seen_ids, semantic = set(), []
            for n in ns:
                r = server.memory.search(query, filters={"user_id": n}, limit=fetch_n)
                for item in r.get("results", []):
                    if item["id"] not in seen_ids:
                        seen_ids.add(item["id"])
                        semantic.append(item)
            semantic.sort(key=lambda x: x.get("score", 0), reverse=True)
            keyword = server.fts.search(query, ns, fetch_n)
        except Exception as exc:
            stats["retrieval_error"] += 1
            print(f"  [{i}] RETRIEVAL FAIL {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            continue

        admitted = admit_keyword_hits(query, keyword) if keyword else []
        n_kw, n_adm = len(keyword), len(admitted)
        if n_kw:
            admit_ratios.append(n_adm / n_kw)

        # --- merge the SAME lists under both strategies ------------------
        def final(strategy):
            merged = rrf_merge(list(semantic), list(keyword), fetch_n,
                               strategy=strategy, query=query)
            resolved = server._resolve_supersession(merged, False)
            return [h.get("id") for h in resolved[:LIMIT]]

        try:
            top_classic = final("classic")
            top_per_hit = final("per_hit")
        except Exception as exc:
            stats["merge_error"] += 1
            print(f"  [{i}] MERGE FAIL {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            continue

        sem_ids = {s["id"] for s in semantic}
        kw_ids = {k["id"] for k in keyword if k.get("id")}
        adm_ids = {k["id"] for k in admitted if k.get("id")}

        promoted = [x for x in top_per_hit if x not in top_classic]
        demoted = [x for x in top_classic if x not in top_per_hit]
        diverged = bool(promoted or demoted)

        stats["total"] += 1
        stats["diverged"] += diverged
        stats["kw_zero"] += (n_kw == 0)
        stats["admit_all"] += (n_kw > 0 and n_adm == n_kw)
        stats["admit_none"] += (n_kw > 0 and n_adm == 0)

        # direction split: what KIND of row moved
        for pid in promoted:
            stats["promoted_semantic_backed" if pid in sem_ids
                  else "promoted_keyword_only"] += 1
        for did in demoted:
            if did in kw_ids and did not in sem_ids:
                # keyword-only row that lost its vote -- the intended target
                stats["demoted_keyword_only_dropped" if did not in adm_ids
                      else "demoted_keyword_only_admitted"] += 1
            else:
                stats["demoted_semantic_backed"] += 1

        # corpus drift: captured (then) vs classic (now) -- NOT a strategy signal
        cap_top = rec.get("top5_ids") or []
        if cap_top:
            stats["drift_checked"] += 1
            stats["drift_same"] += (cap_top[:LIMIT] == top_classic)

        rows.append({
            "query": query, "namespaces": ns,
            "kw_total": n_kw, "kw_admitted": n_adm,
            "n_semantic": len(semantic),
            "top_classic": top_classic, "top_per_hit": top_per_hit,
            "promoted": promoted, "demoted": demoted, "diverged": diverged,
            "captured_top5": cap_top,
        })
        if i % 25 == 0:
            print(f"  ...{i}/{len(pairs)} diverged={stats['diverged']}", flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    t = stats["total"] or 1
    kw_q = t - stats["kw_zero"]
    print("\n================ SHADOW EVAL: classic vs per_hit ================")
    print(f"queries evaluated      {stats['total']}   (retrieval_err={stats['retrieval_error']} merge_err={stats['merge_error']})")
    print(f"queries w/ 0 kw hits   {stats['kw_zero']}  -> per_hit is a strict no-op for these")
    print(f"\n-- ADMISSION REGIME (of {kw_q} queries that had keyword hits) --")
    if kw_q > 0:
        print(f"  all hits admitted    {stats['admit_all']}  ({100*stats['admit_all']/kw_q:.1f}%)  -> per_hit == classic")
        print(f"  no hits admitted     {stats['admit_none']}  ({100*stats['admit_none']/kw_q:.1f}%)  -> per_hit == semantic-only")
        if admit_ratios:
            print(f"  mean admit ratio     {sum(admit_ratios)/len(admit_ratios):.3f}")
    print(f"\n-- BLAST RADIUS --")
    print(f"  top-5 diverged       {stats['diverged']}/{stats['total']}  ({100*stats['diverged']/t:.1f}%)")
    print(f"\n-- DIRECTION SPLIT (rows that moved) --")
    print(f"  promoted, semantic-backed        {stats['promoted_semantic_backed']}   <- intended win")
    print(f"  promoted, keyword-only           {stats['promoted_keyword_only']}")
    print(f"  demoted, keyword-only DROPPED    {stats['demoted_keyword_only_dropped']}   <- junk vote killed (verify sample)")
    print(f"  demoted, keyword-only admitted   {stats['demoted_keyword_only_admitted']}")
    print(f"  demoted, semantic-backed         {stats['demoted_semantic_backed']}   <- FEARED LOSS, inspect each")
    if stats["drift_checked"]:
        d = stats["drift_checked"]
        print(f"\n-- CORPUS DRIFT since capture (context only, not a strategy signal) --")
        print(f"  classic-now == captured-then   {stats['drift_same']}/{d}  ({100*stats['drift_same']/d:.1f}%)")
    print(f"\nper-query rows -> {args.out}")


if __name__ == "__main__":
    main()
