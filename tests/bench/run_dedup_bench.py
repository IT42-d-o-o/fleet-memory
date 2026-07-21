"""Benchmark gemma4-12b-qat as the dedup judge: 100 labeled pairs, temperature 0."""
import json
import sys
import time
import urllib.request

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemma4-12b-qat:latest"
TESTSET = sys.argv[2] if len(sys.argv) > 2 else "dedup_testset.json"
OLLAMA = "http://127.0.0.1:11434/api/chat"

SYSTEM = (
    "You judge whether two stored memory facts are duplicates of each other.\n"
    "duplicate = both state the SAME claim about the same entity; one may be a "
    "reworded, shorter, or less detailed version of the other.\n"
    "distinct = they state DIFFERENT claims: a different attribute, a different "
    "event, opposite or changed behavior, or the same attribute with a different "
    "number, version, date, or value.\n"
    'Answer ONLY with JSON: {"verdict":"duplicate"} or {"verdict":"distinct"}'
)

def judge(a: str, b: str) -> tuple[str, float]:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Fact 1: {a}\nFact 2: {b}"},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "keep_alive": "10m",
    }).encode()
    t0 = time.time()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    last_err = None
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            break
        except Exception as e:  # connect can time out while the model is loading/busy
            last_err = e
            time.sleep(min(15 * (attempt + 1), 60))
    else:
        raise last_err
    dt = time.time() - t0
    raw = resp["message"]["content"]
    try:
        verdict = json.loads(raw).get("verdict", "").strip().lower()
    except json.JSONDecodeError:
        verdict = "parse_error:" + raw[:80]
    return verdict, dt


def main():
    cases = json.load(open(TESTSET, encoding="utf-8"))
    results = []
    try:
        with open("dedup_bench_partial.jsonl", encoding="utf-8") as fh:
            results = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        pass
    done_ids = {r["id"] for r in results}
    if done_ids:
        print(f"resuming, {len(done_ids)} cases already done", flush=True)

    part = open("dedup_bench_partial.jsonl", "a", encoding="utf-8")
    for i, c in enumerate(cases, 1):
        if c["id"] in done_ids:
            continue
        verdict, dt = judge(c["a"], c["b"])
        ok = verdict == c["label"]
        rec = {**c, "verdict": verdict, "ok": ok, "sec": round(dt, 2)}
        results.append(rec)
        part.write(json.dumps(rec) + "\n")
        part.flush()
        if len(results) % 10 == 0:
            acc = sum(r["ok"] for r in results) / len(results)
            print(f"{len(results)}/100 done, running accuracy {acc:.0%}", flush=True)
    part.close()

    times = [r["sec"] for r in results]
    json.dump(results, open("dedup_bench_results.json", "w", encoding="utf-8"), indent=1)

    total = len(results)
    correct = sum(r["ok"] for r in results)
    print(f"\n=== {MODEL} ===")
    print(f"Overall: {correct}/{total} = {correct/total:.0%}")
    print(f"Latency: avg {sum(times)/total:.1f}s  max {max(times):.1f}s")

    cats = sorted({r["cat"] for r in results})
    print("\nPer category:")
    for cat in cats:
        sub = [r for r in results if r["cat"] == cat]
        print(f"  {cat:10s} {sum(r['ok'] for r in sub):3d}/{len(sub)}")

    fp = [r for r in results if r["label"] == "distinct" and r["verdict"] == "duplicate"]
    fn = [r for r in results if r["label"] == "duplicate" and r["verdict"] == "distinct"]
    other = [r for r in results if not r["ok"] and r not in fp and r not in fn]
    print(f"\nFALSE POSITIVES (judge would wrongly collapse — dangerous): {len(fp)}")
    for r in fp:
        print(f"  #{r['id']} [{r['cat']}] {r['a'][:70]} || {r['b'][:70]}")
    print(f"FALSE NEGATIVES (missed dup — harmless, stays duplicated): {len(fn)}")
    for r in fn:
        print(f"  #{r['id']} [{r['cat']}] {r['a'][:70]}")
    if other:
        print(f"PARSE ERRORS: {len(other)}")
        for r in other:
            print(f"  #{r['id']} verdict={r['verdict']}")


if __name__ == "__main__":
    main()
