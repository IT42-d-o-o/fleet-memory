#!/usr/bin/env python3
"""Non-Claude adjudication of the diverged shadow-eval queries.

The labeled probe set scores 100% under both arms, so it cannot decide this.
The decision therefore rests on real-traffic divergence, and relevance must be
judged by something outside the Claude family -- otherwise the artifact that
governs what Claude retrieves is being graded by Claude. Uses the local gemma
already trusted as the dedup gate judge.

Bias controls:
  * blinded  -- arms are shown as "A"/"B", never named
  * randomized -- seeded coin decides which arm is A for each query
  * order-swapped -- every pair is judged twice with A/B exchanged; only
    verdicts that survive the swap count as decisive. Position bias shows up
    as an inconsistent pair and is reported, not silently averaged away.

Also splits the whole capture by identifier-bearing vs prose queries, which is
what actually explains the admission regime.
"""
import argparse
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


PROMPT = """You are ranking two candidate result sets from a memory search system.

SEARCH QUERY:
{query}

RESULT SET A:
{a}

RESULT SET B:
{b}

Which result set is more relevant and useful as an answer to the SEARCH QUERY?
Judge only relevance to the query. Ignore ordering, formatting and length.
Answer with ONLY one line and no other text, exactly of the form:
FINAL: A
(or FINAL: B, or FINAL: TIE)"""


def ask(url, model, prompt, timeout=180):
    import httpx
    r = httpx.post(f"{url}/api/generate",
                   json={"model": model, "prompt": prompt, "stream": False,
                         # think=False: this gemma variant routes all output to a
                         # separate reasoning channel, leaving `response` empty.
                         "think": False,
                         "options": {"temperature": 0, "num_predict": 24}},
                   timeout=timeout)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def verdict_of(text):
    """Prefer the explicit FINAL: marker; else the LAST standalone token.

    Taking the first match would catch letters cited inside the reasoning
    ("Set A mentions..."), which is the opposite of the conclusion.
    """
    import re
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).upper()
    m = re.findall(r"FINAL\s*:\s*(A|B|TIE)\b", t)
    if m:
        return m[-1]
    toks = re.findall(r"\b(A|B|TIE)\b", t)
    return toks[-1] if toks else "?"


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001 — one bad judgement must not end the run
        print(f"  judge error {type(exc).__name__}: {str(exc)[:90]}", flush=True)
        return None


def render(rows):
    out = []
    for i, r in enumerate(rows, 1):
        txt = str(r.get("memory") or "").replace("\n", " ").strip()
        out.append(f"{i}. {txt[:200]}")
    return "\n".join(out) or "(no results)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default="/tmp/shadow_full.jsonl")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--out", default="/tmp/judge_verdicts.jsonl")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    inherit_env_from_server()
    os.environ.pop("MEM0_QUERY_CAPTURE", None)
    import server
    from fts_index import query_has_identifier

    data = [json.loads(l) for l in open(args.rows, encoding="utf-8") if l.strip()]

    # ---- class split: what kind of query is this traffic, really? ----------
    print("========== QUERY CLASS SPLIT (all captured traffic) ==========")
    for label, pred in (("identifier-bearing", lambda q: query_has_identifier(q)),
                        ("prose (no identifier)", lambda q: not query_has_identifier(q))):
        grp = [d for d in data if pred(d["query"])]
        if not grp:
            continue
        ratios = [d["kw_admitted"] / d["kw_total"] for d in grp if d["kw_total"]]
        div = sum(1 for d in grp if d["diverged"])
        mlen = sum(len(d["query"]) for d in grp) / len(grp)
        print(f"  {label:22s} n={len(grp):4d}  mean_query_len={mlen:6.0f}  "
              f"mean_admit={sum(ratios)/len(ratios) if ratios else 0:.3f}  "
              f"diverged={div}/{len(grp)} ({100*div/len(grp):.0f}%)")

    diverged = [d for d in data if d["diverged"]]
    rng = random.Random(1337)
    sample = rng.sample(diverged, min(args.sample, len(diverged)))
    print(f"\n========== BLINDED JUDGE on {len(sample)} of {len(diverged)} diverged ==========")

    # No hardcoded default — the judge backend is site-specific. It is normally
    # inherited from the running server's environment (see inherit_env_from_server).
    url = os.environ.get("GATE_OLLAMA_URL")
    model = os.environ.get("GATE_LLM_MODEL", "gemma4-12b-qat-opencode:latest")
    if not url:
        sys.exit("GATE_OLLAMA_URL not set — export it, or run where the server's "
                 "environment can be inherited.")
    print(f"judge: {model} @ {url}\n", flush=True)

    cache, results = {}, []
    tally = {"per_hit": 0, "classic": 0, "tie": 0, "inconsistent": 0, "error": 0}

    def texts(ids):
        rows = []
        for i in ids:
            if i not in cache:
                cache[i] = server._fetch_record(i)
            if cache[i]:
                rows.append(cache[i])
        return render(rows)

    # Qdrant fetches first (serial, cheap) so the judged payloads are ready
    # before any concurrency starts touching the shared cache.
    jobs = []
    for d in sample:
        jobs.append((d, texts(d["top_classic"]), texts(d["top_per_hit"]),
                     rng.random() < 0.5))

    def to_arm(v, is_a_per_hit):
        if v == "TIE":
            return "tie"
        if v == "A":
            return "per_hit" if is_a_per_hit else "classic"
        if v == "B":
            return "classic" if is_a_per_hit else "per_hit"
        return "?"

    def judge(job):
        d, c_txt, p_txt, per_hit_is_a = job
        a, b = (p_txt, c_txt) if per_hit_is_a else (c_txt, p_txt)
        q = d["query"][:700]
        v1 = verdict_of(ask(url, model, PROMPT.format(query=q, a=a, b=b)))
        # swap positions; a stable judgement must survive the exchange
        v2 = verdict_of(ask(url, model, PROMPT.format(query=q, a=b, b=a)))
        w1, w2 = to_arm(v1, per_hit_is_a), to_arm(v2, not per_hit_is_a)
        return d, w1, w2

    from concurrent.futures import ThreadPoolExecutor
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(lambda j: _safe(judge, j), jobs):
            done += 1
            if res is None:
                tally["error"] += 1
                continue
            d, w1, w2 = res
            final = w1 if w1 == w2 else "inconsistent"
            tally[final if final in tally else "error"] += 1
            results.append({"query": d["query"], "v1": w1, "v2": w2, "final": final,
                            "kw_total": d["kw_total"], "kw_admitted": d["kw_admitted"]})
            if done % 5 == 0:
                print(f"  ...{done}/{len(jobs)}  {tally}", flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    dec = tally["per_hit"] + tally["classic"]
    print("\n================ VERDICT (order-swap-consistent only) ================")
    print(f"  per_hit better   {tally['per_hit']}")
    print(f"  classic better   {tally['classic']}")
    print(f"  tie              {tally['tie']}")
    print(f"  inconsistent     {tally['inconsistent']}  (position bias — not counted)")
    print(f"  errors           {tally['error']}")
    if dec:
        print(f"\n  decisive: per_hit wins {tally['per_hit']}/{dec} ({100*tally['per_hit']/dec:.0f}%)")
    print(f"\nrows -> {args.out}")


if __name__ == "__main__":
    main()
