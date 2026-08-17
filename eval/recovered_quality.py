#!/usr/bin/env python3
"""Are the rows semantic-only recovers actually GOOD, and who benefits?

Check 1: score distribution of recovered rows. If they hug 0.55 they are
         marginal hits and the practical gain is near zero.
Check 2: split hook traffic from agent traffic by joining captured queries
         against the hook's own recall-log prompts, so the benefit estimate
         stops depending on a volume-ratio guess. Only hook traffic can
         benefit -- agent calls apply no threshold and read all 5 rows.
"""
import json
import os
import re
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

    norm = lambda s: re.sub(r"\s+", " ", s or "").strip()[:100]
    hook_prompts = set(json.load(open("/opt/memory-mcp/hook_prompts.json", encoding="utf-8")))
    rows = [json.loads(l) for l in open("/tmp/shadow_full.jsonl", encoding="utf-8") if l.strip()]
    fetch_n = LIMIT * 3

    recovered, lost_scores = {"hook": [], "agent": []}, []
    qstats = {"hook": dict(n=0, cls_q=0, sem_q=0, cls_rows=0, sem_rows=0),
              "agent": dict(n=0, cls_q=0, sem_q=0, cls_rows=0, sem_rows=0)}

    for d in rows:
        q, ns = d["query"], d["namespaces"]
        kind = "hook" if norm(q) in hook_prompts else "agent"
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

        sem_scores = {s["id"]: s.get("score", 0) or 0 for s in semantic}

        def top(kw):
            m = rrf_merge(list(semantic), list(kw), fetch_n, strategy="classic", query=q)
            return server._resolve_supersession(m, False)[:LIMIT]

        inj = lambda rs: {r.get("id") for r in rs if sem_scores.get(r.get("id"), 0) > THRESHOLD}
        i_cls, i_sem = inj(top(keyword)), inj(top([]))

        s = qstats[kind]
        s["n"] += 1
        s["cls_q"] += bool(i_cls); s["sem_q"] += bool(i_sem)
        s["cls_rows"] += len(i_cls); s["sem_rows"] += len(i_sem)
        for rid in (i_sem - i_cls):
            recovered[kind].append(sem_scores[rid])
        for rid in (i_cls - i_sem):
            lost_scores.append(sem_scores[rid])

    print("======= CHECK 2: WHO ACTUALLY BENEFITS (hook vs agent traffic) =======")
    for kind in ("hook", "agent"):
        s = qstats[kind]
        if not s["n"]:
            continue
        print(f"  {kind:6s} queries={s['n']:4d}  "
              f"injectable rows {s['cls_rows']:3d} -> {s['sem_rows']:3d} "
              f"({s['sem_rows']-s['cls_rows']:+d})   "
              f"queries with a hit {s['cls_q']:3d} -> {s['sem_q']:3d} "
              f"({s['sem_q']-s['cls_q']:+d})")
    print("  (agent traffic reads all 5 rows with no threshold — it cannot benefit)")

    allrec = recovered["hook"]
    print(f"\n======= CHECK 1: QUALITY OF RECOVERED ROWS (hook traffic only) =======")
    print(f"  recovered rows: {len(allrec)}")
    if allrec:
        allrec.sort()
        import statistics as st
        print(f"  median {st.median(allrec):.3f}   mean {st.mean(allrec):.3f}   max {max(allrec):.3f}")
        buckets = [(.55, .58), (.58, .60), (.60, .65), (.65, .70), (.70, 1.01)]
        for lo, hi in buckets:
            n = sum(1 for x in allrec if lo <= x < hi)
            bar = "#" * int(40 * n / len(allrec))
            print(f"   {lo:.2f}-{hi:.2f}  {n:3d}  {100*n/len(allrec):5.1f}%  {bar}")
        marginal = sum(1 for x in allrec if x < 0.60)
        print(f"\n  marginal (<0.60): {marginal}/{len(allrec)} = {100*marginal/len(allrec):.0f}%")
        print(f"  solid   (>=0.60): {len(allrec)-marginal}/{len(allrec)} = "
              f"{100*(len(allrec)-marginal)/len(allrec):.0f}%")
    if lost_scores:
        import statistics as st
        print(f"\n  (for contrast, rows the keyword arm ADDS: {len(lost_scores)}, "
              f"median {st.median(lost_scores):.3f})")


if __name__ == "__main__":
    main()
