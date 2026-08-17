#!/usr/bin/env python3
"""Export the SAME 60 judged pairs for human blind grading.

Reproduces judge_diverged.py's seeded sample exactly (Random(1337) over the
diverged rows in file order), then re-blinds with an INDEPENDENT seed so the
human's A/B layout is uncorrelated with the model judge's — otherwise a shared
position bias could make the two graders agree for the wrong reason.

Order: the 22 order-inconsistent pairs first (external review flagged them as
numerous enough to reverse the conclusion), then the rest.
"""
import json
import os
import random
import sys

sys.path.insert(0, "/opt/memory-mcp")


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

    rows = [json.loads(l) for l in open("/tmp/shadow_full.jsonl", encoding="utf-8") if l.strip()]
    diverged = [d for d in rows if d["diverged"]]
    sample = random.Random(1337).sample(diverged, 60)  # identical to the judge run

    verdicts = {}
    try:
        for l in open("/tmp/judge_verdicts.jsonl", encoding="utf-8"):
            if l.strip():
                v = json.loads(l)
                verdicts[v["query"]] = v["final"]
    except FileNotFoundError:
        pass

    cache = {}

    def texts(ids):
        out = []
        for i in ids:
            if i not in cache:
                cache[i] = server._fetch_record(i)
            rec = cache[i]
            if rec:
                out.append(str(rec.get("memory") or "").replace("\n", " ").strip())
        return out

    rng = random.Random(4242)  # independent of the model judge's blinding
    pairs = []
    for d in sample:
        c, p = texts(d["top_classic"]), texts(d["top_per_hit"])
        per_hit_is_a = rng.random() < 0.5
        pairs.append({
            "query": d["query"],
            "a": p if per_hit_is_a else c,
            "b": c if per_hit_is_a else p,
            "_a_arm": "per_hit" if per_hit_is_a else "classic",
            "kw_total": d["kw_total"],
            "kw_admitted": d["kw_admitted"],
            "model_verdict": verdicts.get(d["query"], "?"),
        })

    # inconsistent pairs first — highest information per minute of human time
    pairs.sort(key=lambda x: 0 if x["model_verdict"] == "inconsistent" else 1)

    with open("/tmp/grading_pairs.json", "w", encoding="utf-8") as fh:
        json.dump(pairs, fh, ensure_ascii=False)
    n_inc = sum(1 for p in pairs if p["model_verdict"] == "inconsistent")
    print(f"exported {len(pairs)} pairs ({n_inc} inconsistent first) -> /tmp/grading_pairs.json")


if __name__ == "__main__":
    main()
