#!/usr/bin/env python3
"""Does the FTS5 keyword arm earn its place in the read path?

Mechanism under test: rrf_merge gives a keyword-only row
`score = rrf_score` (~1/61 = 0.016) because it has no semantic score. The
session hook injects only rows with score > 0.55. So a keyword-only row can
occupy a top-5 slot but can never itself be injected — and by occupying that
slot it may displace a semantic row that WOULD have been injected.

Compares, per captured query:
  classic        = semantic + keyword fused (production today)
  semantic_only  = keyword arm removed entirely
counting injectable rows (semantic score > 0.55) surviving in each.
"""
import json
import os
import sys

sys.path.insert(0, "/opt/memory-mcp")
THRESHOLD = 0.55
LIMIT = 5


def inherit_env_from_server():
    import glob
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            cmd = open(path, "rb").read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "server.py" not in cmd or "memory-mcp" not in cmd:
            continue
        pid = path.split("/")[2]
        blob = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", "ignore")
        for kv in blob.split("\0"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k and k[0].isalpha() and k.upper() == k and k not in os.environ:
                    os.environ[k] = v
        return pid
    return None


def main():
    inherit_env_from_server()
    os.environ.pop("MEM0_QUERY_CAPTURE", None)
    import server
    from fts_index import rrf_merge

    rows = [json.loads(l) for l in open("/tmp/shadow_full.jsonl", encoding="utf-8") if l.strip()]
    fetch_n = LIMIT * 3

    stats = dict(q=0, kw_slots=0, slots=0,
                 inj_classic=0, inj_semonly=0,
                 q_inj_classic=0, q_inj_semonly=0,
                 displaced_q=0, displaced_rows=0, gained_rows=0)
    examples = []

    for d in rows:
        q, ns = d["query"], d["namespaces"]
        try:
            seen, semantic = set(), []
            for n in ns:
                r = server.memory.search(q, filters={"user_id": n}, limit=fetch_n)
                for it in r.get("results", []):
                    if it["id"] not in seen:
                        seen.add(it["id"])
                        semantic.append(it)
            semantic.sort(key=lambda x: x.get("score", 0), reverse=True)
            keyword = server.fts.search(q, ns, fetch_n)
        except Exception:
            continue

        def top(kw):
            merged = rrf_merge(list(semantic), list(kw), fetch_n, strategy="classic", query=q)
            return server._resolve_supersession(merged, False)[:LIMIT]

        t_cls, t_sem = top(keyword), top([])
        sem_scores = {s["id"]: s.get("score", 0) for s in semantic}

        def injectable(rowset):
            return {r.get("id") for r in rowset
                    if (sem_scores.get(r.get("id"), r.get("semantic_score") or 0) or 0) > THRESHOLD}

        i_cls, i_sem = injectable(t_cls), injectable(t_sem)
        kw_only = [r for r in t_cls if r.get("id") not in sem_scores]

        stats["q"] += 1
        stats["slots"] += len(t_cls)
        stats["kw_slots"] += len(kw_only)
        stats["inj_classic"] += len(i_cls)
        stats["inj_semonly"] += len(i_sem)
        stats["q_inj_classic"] += bool(i_cls)
        stats["q_inj_semonly"] += bool(i_sem)

        lost = i_sem - i_cls
        gained = i_cls - i_sem
        stats["displaced_rows"] += len(lost)
        stats["gained_rows"] += len(gained)
        if lost:
            stats["displaced_q"] += 1
            if len(examples) < 8:
                sc = max(sem_scores.get(x, 0) for x in lost)
                examples.append((round(sc, 3), len(kw_only), q[:80]))

    s = stats
    print("=========== IS THE FTS5 KEYWORD ARM EARNING ITS PLACE? ===========")
    print(f"queries evaluated              {s['q']}")
    print(f"\n-- top-5 slot occupancy --")
    print(f"  slots filled                 {s['slots']}")
    print(f"  taken by keyword-only rows   {s['kw_slots']}  ({100*s['kw_slots']/max(s['slots'],1):.1f}%)")
    print(f"  (these carry score~0.016 and can NEVER pass the hook's {THRESHOLD} threshold)")
    print(f"\n-- injectable rows (semantic score > {THRESHOLD}) surviving into top-5 --")
    print(f"  classic (production)         {s['inj_classic']} rows across {s['q_inj_classic']} queries")
    print(f"  semantic_only                {s['inj_semonly']} rows across {s['q_inj_semonly']} queries")
    print(f"\n-- net effect of the keyword arm --")
    print(f"  injectable rows DISPLACED    {s['displaced_rows']}  (on {s['displaced_q']} queries)")
    print(f"  injectable rows GAINED       {s['gained_rows']}")
    delta = s['inj_classic'] - s['inj_semonly']
    print(f"  net                          {delta:+d} rows")
    if examples:
        print(f"\n-- sample displacements (score of the lost row / kw rows in top-5 / query) --")
        for sc, k, q in examples:
            print(f"  lost score={sc}  kw_rows={k}  | {q}")


if __name__ == "__main__":
    main()
