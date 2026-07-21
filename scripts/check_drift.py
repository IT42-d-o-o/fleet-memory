#!/usr/bin/env python3
"""
check_drift.py — nightly deploy-drift audit for fleet-memory (runs ON CT356).

Compares every file in /opt/memory-mcp/deploy-manifest.txt against git main
(fetched from the Gitea API) and pushes the count of divergent files to the
Pushgateway (job memory_drift). A Grafana alert on memory_drift_files > 0
notifies Telegram — catching both forgotten deploys (repo ahead of box) and
on-box hand edits (box ahead of repo), the exact failure mode that let
gate.py and subject_alias.py drift historically.

Exit 0 always when the audit itself ran (drift is a metric, not a failure);
exit 1 only when the audit could not run (Gitea unreachable, token missing).

Invoked as step 4 of /opt/memory-mcp/reconcile.sh. Manual run:
  venv/bin/python /opt/memory-mcp/check_drift.py
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, "/opt/memory-mcp/venv/lib/python3.11/site-packages")

import httpx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("check_drift")

GITEA = os.environ.get("DRIFT_GITEA_URL", "http://192.168.50.135:3000")
REPO = os.environ.get("DRIFT_REPO", "ai/fleet-memory")
BRANCH = os.environ.get("DRIFT_BRANCH", "main")
MANIFEST = os.environ.get("DRIFT_MANIFEST", "/opt/memory-mcp/deploy-manifest.txt")
PUSHGATEWAY = os.environ.get("DRIFT_PUSHGATEWAY_URL", "http://192.168.50.223:9091")
REPORT_PATH = os.environ.get("DRIFT_REPORT_PATH", "/var/lib/memory-stats/drift-report.json")


def vault_get(path, field):
    r = subprocess.run(
        ["vault", "kv", "get", f"-field={field}", path],
        capture_output=True, text=True,
        env={**os.environ, "VAULT_ADDR": "http://10.10.10.107:8200",
             "VAULT_TOKEN": open("/etc/memory-mcp/vault-token").read().strip()},
    )
    if r.returncode != 0:
        sys.exit(f"Vault error reading {path}: {r.stderr.strip()[:120]}")
    return r.stdout.strip()


def main() -> None:
    token = vault_get("secret/infra/gitea", "token")
    headers = {"Authorization": f"token {token}"}

    entries = []
    with open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            repo_path, ct_path = line.split()
            entries.append((repo_path, ct_path))

    drifted, errors = [], []
    for repo_path, ct_path in entries:
        url = f"{GITEA}/api/v1/repos/{REPO}/raw/{repo_path}?ref={BRANCH}"
        try:
            resp = httpx.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            git_sha = hashlib.sha256(resp.content).hexdigest()
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": repo_path, "error": str(exc)[:120]})
            continue
        try:
            with open(ct_path, "rb") as fh:
                local_sha = hashlib.sha256(fh.read()).hexdigest()
        except FileNotFoundError:
            drifted.append({"file": ct_path, "kind": "missing on box"})
            continue
        if git_sha != local_sha:
            drifted.append({"file": ct_path, "kind": "content differs from git main"})

    if errors and len(errors) == len(entries):
        sys.exit("audit failed: every Gitea fetch errored — check network/token")

    for d in drifted:
        log.warning("DRIFT %s -- %s", d["file"], d["kind"])

    report = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checked": len(entries),
        "drifted": drifted,
        "fetch_errors": errors,
    }
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    except OSError as exc:
        log.warning("report write failed (non-fatal): %s", exc)

    try:
        body = (
            "# TYPE memory_drift_files gauge\n"
            f"memory_drift_files {len(drifted)}\n"
            "# TYPE memory_drift_fetch_errors gauge\n"
            f"memory_drift_fetch_errors {len(errors)}\n"
            "# TYPE memory_drift_last_run_timestamp_seconds gauge\n"
            f"memory_drift_last_run_timestamp_seconds {time.time()}\n"
        )
        httpx.put(f"{PUSHGATEWAY}/metrics/job/memory_drift",
                  content=body.encode(), timeout=5)
    except Exception as exc:  # noqa: BLE001
        log.warning("pushgateway push failed (non-fatal): %s", exc)

    log.info("drift audit: %d files checked, %d drifted, %d fetch errors",
             len(entries), len(drifted), len(errors))


if __name__ == "__main__":
    main()
