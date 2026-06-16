#!/usr/bin/env bash
# fleet-memory nightly reconciliation: tag new points with a subject, then
# recompute supersession lineage across all subject groups. Both scripts fetch
# the OpenAI key from Vault themselves (/etc/memory-mcp/vault-token) and are
# idempotent — a run with no new data changes nothing.
#
# Invoked by fleet-memory-reconcile.service (systemd timer, nightly).
set -uo pipefail

APP_DIR=/opt/memory-mcp
PY="${APP_DIR}/venv/bin/python"

echo "[reconcile] $(date -Is) start"

# 1. Backfill subject on any points written since the last run (already-tagged
#    points are skipped, so this only spends LLM calls on new memories).
"${PY}" "${APP_DIR}/subject_backfill.py"
bf=$?
echo "[reconcile] $(date -Is) subject_backfill exit=${bf}"

# 2. Recompute supersession lineage (non-destructive, overwrites prior values).
"${PY}" "${APP_DIR}/supersede.py"
ss=$?
echo "[reconcile] $(date -Is) supersede exit=${ss}"

echo "[reconcile] $(date -Is) done"
# Non-zero exit if either step failed, so the systemd unit is marked failed.
[ "${bf}" -eq 0 ] && [ "${ss}" -eq 0 ]
