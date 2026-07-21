#!/usr/bin/env bash
# fleet-memory deploy: push manifest-listed files from the repo to CT356.
#
# The manual scp+pct-push routine is how gate.py and subject_alias.py drifted
# from git historically — this script replaces it: one command, every managed
# file, sha-compared so only changed files move, timestamped backups on the
# box, service restart only when a hot file changed, health check after.
#
# Run from the repo root on the workstation (Git Bash):
#   bash scripts/deploy.sh [--dry-run]
#
# Companion guard: scripts/check_drift.py runs nightly ON CT356 and alarms
# (pushgateway job memory_drift -> Grafana -> Telegram) if the deployed files
# ever diverge from git main — catching both forgotten deploys and on-box edits.
set -euo pipefail

PVE=root@192.168.50.223
SSH_KEY=~/.ssh/overseer
CT=356
MANIFEST="$(dirname "$0")/deploy-manifest.txt"
DRY_RUN="${1:-}"

# Files whose change requires a memory-mcp restart (everything the running
# server imports; aliases JSON is hot-reloaded, scripts run per-invocation).
RESTART_FILES="server/server.py server/gate.py server/fts_index.py server/subject_alias.py"

need_restart=0
pushed=0
skipped=0

while read -r repo_path ct_path; do
  [[ -z "$repo_path" || "$repo_path" == \#* ]] && continue
  if [[ ! -f "$repo_path" ]]; then
    echo "MISSING in repo: $repo_path" >&2
    exit 1
  fi
  local_sha=$(sha256sum "$repo_path" | cut -d' ' -f1)
  remote_sha=$(ssh -n -i "$SSH_KEY" "$PVE" "pct exec $CT -- sha256sum '$ct_path' 2>/dev/null | cut -d' ' -f1" || true)
  if [[ "$local_sha" == "$remote_sha" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  echo "DEPLOY $repo_path -> $ct_path"
  if [[ "$DRY_RUN" == "--dry-run" ]]; then
    continue
  fi
  base=$(basename "$ct_path")
  scp -q -i "$SSH_KEY" "$repo_path" "$PVE:/tmp/$base" < /dev/null
  ssh -n -i "$SSH_KEY" "$PVE" "
    pct exec $CT -- bash -c 'test -f $ct_path && cp $ct_path $ct_path.bak-deploy-\$(date +%s) || true'
    pct push $CT /tmp/$base $ct_path
    rm /tmp/$base"
  pushed=$((pushed + 1))
  if [[ " $RESTART_FILES " == *" $repo_path "* ]]; then
    need_restart=1
  fi
done < "$MANIFEST"

echo "pushed=$pushed skipped(unchanged)=$skipped"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "DRY RUN -- nothing pushed"
  exit 0
fi

if [[ $need_restart -eq 1 ]]; then
  echo "hot file changed -- restarting memory-mcp"
  ssh -i "$SSH_KEY" "$PVE" "pct exec $CT -- systemctl restart memory-mcp"
  sleep 3
  state=$(ssh -i "$SSH_KEY" "$PVE" "pct exec $CT -- systemctl is-active memory-mcp")
  echo "memory-mcp: $state"
  [[ "$state" == "active" ]] || exit 1
fi

# chmod executables
ssh -i "$SSH_KEY" "$PVE" "pct exec $CT -- bash -c 'chmod +x /opt/memory-mcp/*.sh /opt/memory-mcp/*.py 2>/dev/null || true'"

echo "deploy OK"
