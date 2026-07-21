#!/usr/bin/env bash
# fleet-memory nightly reconciliation: tag new points with a subject, recompute
# supersession lineage across all subject groups, then collapse near-duplicates
# within each group. subject_backfill.py and supersede.py fetch the OpenAI key
# from Vault themselves (/etc/memory-mcp/vault-token); dedup_stage.py needs NO
# Vault and NO OpenAI -- its judge is local Ollama only (GATE_OLLAMA_URL etc.,
# same env the write-path gate uses, delivered via systemd drop-in). All three
# scripts are idempotent -- a run with no new data changes nothing.
#
# Invoked by fleet-memory-reconcile.service (systemd timer, nightly).
set -uo pipefail

APP_DIR=/opt/memory-mcp
PY="${APP_DIR}/venv/bin/python"

echo "[reconcile] $(date -Is) start"

# 0. Namespace self-heal: collapse any case-fragmented fleet:{project}
#    namespaces (idempotent, no LLM, no Vault). server.py normalizes project
#    at both entry points since 2026-07-22, but a future direct-DB writer
#    could re-fragment -- this guard means fragmentation can never survive
#    more than one night. Output is the audit log of anything it moved.
"${PY}" "${APP_DIR}/heal_namespaces.py"
hn=$?
echo "[reconcile] $(date -Is) heal_namespaces exit=${hn}"

# 1. Backfill subject on any points written since the last run (already-tagged
#    points are skipped, so this only spends LLM calls on new memories).
"${PY}" "${APP_DIR}/subject_backfill.py"
bf=$?
echo "[reconcile] $(date -Is) subject_backfill exit=${bf}"

# 2. Recompute supersession lineage (non-destructive, overwrites prior values).
"${PY}" "${APP_DIR}/supersede.py"
ss=$?
echo "[reconcile] $(date -Is) supersede exit=${ss}"

# 3. Collapse near-duplicates within each (user_id, subject) group
#    (non-destructive, overwrites prior dedup lineage). Incremental fast-path
#    via --since-state -- only groups touched since the last successful run
#    are re-judged.
"${PY}" "${APP_DIR}/dedup_stage.py" --since-state
dd=$?
echo "[reconcile] $(date -Is) dedup_stage exit=${dd}"

if [[ $hn -eq 0 && $bf -eq 0 && $ss -eq 0 && $dd -eq 0 ]]; then
  echo "[reconcile] $(date -Is) all stages OK"
  exit 0
else
  echo "[reconcile] $(date -Is) FAILURE (heal_namespaces=${hn} subject_backfill=${bf} supersede=${ss} dedup_stage=${dd})"
  exit 1
fi
