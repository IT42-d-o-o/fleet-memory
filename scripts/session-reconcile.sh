#!/usr/bin/env bash
# session-reconcile.sh -- SessionEnd fast-path for fleet-memory supersession.
#
# Invoked by the Claude Code SessionEnd hook on the workstation. Triggers an
# INCREMENTAL supersession reconcile on CT356 in the background so the hook
# returns immediately and never blocks session close.
#
# The remote command runs under nohup & so SSH itself returns in milliseconds.
# A missed session-close reconcile is not an error -- the nightly systemd timer
# (fleet-memory-reconcile.service) is the authoritative backstop. This script
# exits 0 even if SSH fails for exactly that reason.
#
# Incremental mode (--since-state):
#   Reads /opt/memory-mcp/.supersede_last_run for the last successful run
#   timestamp. Only subject groups with a point newer than that timestamp are
#   re-judged. First run (no state file) falls back to a full pass.

set -uo pipefail

REMOTE_HOST="root@192.168.50.223"
SSH_KEY="${HOME}/.ssh/overseer"
CT_ID=356
APP_DIR=/opt/memory-mcp
LOG_FILE="${APP_DIR}/reconcile.log"
PY="${APP_DIR}/venv/bin/python"

echo "[session-reconcile] $(date -Is) triggering incremental supersede on CT${CT_ID}"

ssh \
  -i "${SSH_KEY}" \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=10 \
  "${REMOTE_HOST}" \
  "pct exec ${CT_ID} -- bash -lc 'nohup ${PY} ${APP_DIR}/supersede.py --since-state >> ${LOG_FILE} 2>&1 &'" \
  && echo "[session-reconcile] $(date -Is) dispatched OK" \
  || echo "[session-reconcile] $(date -Is) SSH failed -- nightly timer is backstop, continuing"

# Always exit 0: a missed session-close reconcile is covered by the nightly timer.
exit 0
