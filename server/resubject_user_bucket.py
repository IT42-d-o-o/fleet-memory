#!/usr/bin/env python3
"""
resubject_user_bucket.py — one-off wheel #6 cleanup: dissolve the 'user' bucket.

The retired transcript-miner filed ~250 facts under the catch-all subject
'user'. A bucket that coarse can never be reconciled: supersession and dedup
group by subject, so nothing in it participates in lineage, and it exceeds
every group-size cap. This script classifies each 'user' fact with gpt-4o-mini
into one of three actions and applies them NON-DESTRUCTIVELY via set_payload:

  resubject   fact is really about a nameable project/host/service/tool ->
              subject becomes that entity (alias-canonicalized), raw_subject
              keeps 'user' for audit
  preference  durable personal preference / working style of the operator ->
              subject 'tomislav'
  retire      generic engineering narration, session snapshots, test writes —
              no recoverable entity -> current=False,
              retired_reason='wheel6-vague-subject' (text kept, no delete)

Going forward the server rejects vague subjects at write time
(MEMORY_VAGUE_SUBJECT, server.py wheel-6 denylist), so this bucket cannot
re-form; this script exists for the historical backlog and is safe to re-run
(points already moved out of subject=='user' are simply not selected).

Run on CT356:
  python3 /opt/memory-mcp/resubject_user_bucket.py --dry-run   # classify + report only
  python3 /opt/memory-mcp/resubject_user_bucket.py             # apply

Vault path: secret/infra/memory-mcp field openai_api
Vault token: /etc/memory-mcp/vault-token
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

import subject_alias  # noqa: E402 — script dir on sys.path[0]


# ---------------------------------------------------------------------------
# Vault bootstrap — identical pattern to subject_backfill.py
# ---------------------------------------------------------------------------

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


os.environ["OPENAI_API_KEY"] = vault_get("secret/infra/memory-mcp", "openai_api")

from qdrant_client import QdrantClient  # noqa: E402
import openai  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("resubject_user_bucket")

COLLECTION = "local_ai_cross_agent_memory"
QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
LLM_MODEL = "gpt-4o-mini"
REPORT_PATH = os.environ.get(
    "RESUBJECT_REPORT_PATH", "/var/lib/memory-stats/resubject-report.json"
)

CLASSIFY_PROMPT = """\
A shared engineering memory store filed the fact below under the useless \
catch-all subject "user". Decide what to do with it. Return JSON with exactly \
one of:

{"action":"resubject","subject":"<entity>"} — the fact is really about a \
concrete nameable thing: a project, service, host, container (CT###), tool, \
or script. Phrases like "User's miner script" or "User's rate limiter" mean \
the fact is about the miner script / rate limiter's project, not the person. \
Prefer a name that appears in the fact; a well-known short slug is fine.

{"action":"preference"} — a durable personal preference or working style of \
the human operator (how they like output formatted, tools they prefer, \
communication style). Not one-off actions.

{"action":"retire"} — none of the above: generic engineering truisms with no \
recoverable entity, one-off session narration ("User fixed X then Y"), test \
writes, or facts too vague to ever be useful.

When torn between resubject and retire: resubject ONLY if a concrete entity \
is actually recoverable from the text; otherwise retire.

Fact: """


def classify(client: openai.OpenAI, text: str) -> dict:
    """Returns {"action": ..., "subject": ...?}; on any error returns {"action": "error"}."""
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": CLASSIFY_PROMPT + text}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        action = str(data.get("action", "")).strip().lower()
        if action == "resubject":
            subj = str(data.get("subject", "")).strip()
            if not subj:
                return {"action": "retire"}  # resubject with no entity = not recoverable
            return {"action": "resubject", "subject": subj}
        if action in ("preference", "retire"):
            return {"action": action}
        log.warning("Unparseable action %r for: %s", action, text[:80])
        return {"action": "error"}
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM call failed for %r: %s", text[:60], exc)
        return {"action": "error"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dissolve the 'user' subject bucket (wheel #6).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and report only — no set_payload writes.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N points (0 = all). For sampling the classifier.")
    args = parser.parse_args()

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, check_compatibility=False)
    oai = openai.OpenAI()

    # ---- Select the bucket -------------------------------------------------
    points, offset = [], None
    while True:
        res, offset = client.scroll(COLLECTION, limit=500, offset=offset,
                                    with_payload=True, with_vectors=False)
        points.extend(res)
        if offset is None:
            break
    bucket = [p for p in points if (p.payload or {}).get("subject") == "user"]
    log.info("'user' bucket: %d of %d total points", len(bucket), len(points))
    if args.limit:
        bucket = bucket[: args.limit]

    # Known-entity guard: a resubject target must be an entity that already
    # exists — a subject some non-'user' fact carries, or a canonical from the
    # shared alias table. Otherwise the classifier just invents singleton
    # micro-subjects ("gradlew", "ensuretables") and we trade one
    # unreconcilable bucket for two hundred: those facts lost their project
    # context in the miner era and the honest action is retire.
    known_subjects = {
        (p.payload or {}).get("subject")
        for p in points
        if (p.payload or {}).get("subject") not in (None, "", "user")
    }
    known_subjects |= set(subject_alias._load(force=True).values())
    log.info("Known-entity set: %d subjects", len(known_subjects))

    counts: Counter = Counter()
    decisions = []
    for i, pt in enumerate(bucket, 1):
        payload = pt.payload or {}
        text = payload.get("data", "") or payload.get("memory", "") or ""
        verdict = classify(oai, text)
        action = verdict["action"]
        counts[action] += 1

        new_fields: dict | None = None
        target_subject = None
        if action == "resubject":
            slug = subject_alias.slugify(verdict["subject"])
            canonical, _ = subject_alias.canonicalize(slug)
            target_subject = canonical or slug
            # Guards: never a vague name, and only KNOWN entities — an invented
            # micro-subject means the entity is not actually recoverable.
            if (target_subject in ("user", "system", "me", "unknown", "none", "agent", "")
                    or target_subject not in known_subjects):
                log.info("demote resubject->%r not a known entity -- retiring", target_subject)
                action = "retire"
                counts["resubject"] -= 1
                counts["retire"] += 1
        if action == "resubject":
            new_fields = {"subject": target_subject, "raw_subject": "user"}
        elif action == "preference":
            new_fields = {"subject": "tomislav", "raw_subject": "user"}
        elif action == "retire":
            new_fields = {"current": False, "retired_reason": "wheel6-vague-subject"}

        decisions.append({
            "id": str(pt.id),
            "action": action,
            "subject": target_subject if action == "resubject" else
                       ("tomislav" if action == "preference" else None),
            "text": text[:120],
        })
        log.info("%-10s %s %s", action,
                 (target_subject or "") if action == "resubject" else "",
                 text[:90].replace("\n", " "))

        if new_fields and not args.dry_run:
            client.set_payload(collection_name=COLLECTION,
                               payload=new_fields, points=[pt.id])

        if i % 25 == 0:
            log.info("progress %d/%d %s", i, len(bucket), dict(counts))

    report = {
        "dry_run": args.dry_run,
        "bucket_size": len(bucket),
        "counts": dict(counts),
        "decisions": decisions,
    }
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log.info("Report written: %s", REPORT_PATH)
    except OSError as exc:
        log.warning("Report write failed (non-fatal): %s", exc)

    log.info("Done: %s%s", dict(counts), "  (DRY RUN — no writes)" if args.dry_run else "")


if __name__ == "__main__":
    main()
