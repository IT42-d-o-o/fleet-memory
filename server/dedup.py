"""
Deduplication maintenance tool for fleet-memory (CT356).

Scrolls all Qdrant points, groups per user_id, deletes:
  - exact hash duplicates (keeps oldest)
  - near-duplicates above SIM_THRESHOLD via SequenceMatcher

Usage (run on CT356):
  python dedup.py [--dry-run]

Vault path: secret/Mem0 field openai_api
Vault token: /etc/memory-mcp/vault-token
"""
import os, sys, subprocess, warnings
from collections import defaultdict
from difflib import SequenceMatcher

warnings.filterwarnings("ignore")

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

def vault_get(path, field):
    r = subprocess.run(
        ["vault", "kv", "get", f"-field={field}", path],
        capture_output=True, text=True,
        env={**os.environ, "VAULT_ADDR": "http://10.10.10.107:8200",
             "VAULT_TOKEN": open("/etc/memory-mcp/vault-token").read().strip()}
    )
    if r.returncode != 0:
        sys.exit(f"Vault error: {r.stderr}")
    return r.stdout.strip()

os.environ["OPENAI_API_KEY"] = vault_get("secret/Mem0", "openai_api")

from qdrant_client import QdrantClient
from mem0 import Memory

COLLECTION = "local_ai_cross_agent_memory"
SIM_THRESHOLD = 0.87
DRY_RUN = "--dry-run" in sys.argv

# ---- scroll all from Qdrant --------------------------------------------
qclient = QdrantClient(host="127.0.0.1", port=6333, check_compatibility=False)
all_points = []
offset = None
while True:
    res, offset = qclient.scroll(COLLECTION, limit=500, offset=offset,
                                  with_payload=True, with_vectors=False)
    all_points.extend(res)
    if offset is None:
        break

print(f"Total memories: {len(all_points)}", flush=True)

records = []
for pt in all_points:
    p = pt.payload or {}
    records.append({
        "id": str(pt.id),
        "memory": p.get("data", "") or p.get("memory", "") or p.get("text", ""),
        "hash": p.get("hash", ""),
        "user_id": p.get("user_id", ""),
        "created_at": p.get("created_at", ""),
    })

# ---- group by user_id, then find dupes ---------------------------------
by_uid = defaultdict(list)
for r in records:
    by_uid[r["user_id"]].append(r)

to_delete = []

def sim(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

for uid, mems in by_uid.items():
    by_hash = defaultdict(list)
    for m in mems:
        by_hash[m["hash"]].append(m)

    exact_del = []
    for h, group in by_hash.items():
        if h and len(group) > 1:
            sorted_g = sorted(group, key=lambda x: x["created_at"])
            exact_del.extend(sorted_g[1:])

    representatives = [sorted(g, key=lambda x: x["created_at"])[0] if len(g) > 1 else g[0]
                       for g in by_hash.values()]
    exact_del_ids = {m["id"] for m in exact_del}

    visited = set()
    near_del = []
    for i, m in enumerate(representatives):
        if m["id"] in visited:
            continue
        visited.add(m["id"])
        for n in representatives[i+1:]:
            if n["id"] in visited:
                continue
            if m["memory"] and n["memory"] and sim(m["memory"], n["memory"]) >= SIM_THRESHOLD:
                near_del.append(n)
                visited.add(n["id"])

    all_del = exact_del + near_del
    if all_del:
        print(f"\n[{uid}] dropping {len(all_del)}/{len(mems)}:", flush=True)
        for m in all_del:
            tag = "EXACT" if m["id"] in exact_del_ids else "NEAR"
            print(f"  [{tag}] {m['id'][:8]} {m['memory'][:90]}", flush=True)
    to_delete.extend(all_del)

to_delete_ids = list({m["id"] for m in to_delete})
print(f"\nTotal to delete: {len(to_delete_ids)} / {len(records)}", flush=True)

if DRY_RUN:
    print("DRY RUN — no deletions.", flush=True)
    sys.exit(0)

# ---- init mem0 client for deletions ------------------------------------
apikey = os.environ["OPENAI_API_KEY"]
config = {
    "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini", "api_key": apikey}},
    "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small", "api_key": apikey}},
    "vector_store": {"provider": "qdrant", "config": {
        "collection_name": COLLECTION,
        "host": "127.0.0.1", "port": 6333, "embedding_model_dims": 1536,
    }},
    "history_db_path": "/opt/memory-mcp/history.db",
}
mem = Memory.from_config(config)

ok = fail = 0
for mid in to_delete_ids:
    try:
        mem.delete(mid)
        ok += 1
    except Exception as e:
        print(f"  FAIL {mid[:8]}: {e}", flush=True)
        fail += 1

print(f"\nDone. deleted={ok} failed={fail}", flush=True)
