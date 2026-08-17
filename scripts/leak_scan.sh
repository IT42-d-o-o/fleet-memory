#!/bin/sh
# leak_scan.sh — fail if site-specific topology appears in tracked files.
#
# Guards the public mirror: internal addresses, SSH targets and other
# site-specific values must live in the environment (.env / EnvironmentFile),
# never in the repo. Documentation examples use RFC 5737 / RFC 3849 ranges
# (192.0.2.x, 198.51.100.x, 203.0.113.x) and the 10.9x.x.x stand-ins, which
# this scan deliberately allows.
#
# Usage:  sh scripts/leak_scan.sh        (run from the repo root; exit 1 = leak)
# Wire it as a pre-push hook or CI step.
set -u

PATTERN='192\.168\.[0-9]+\.[0-9]+|10\.10\.10\.[0-9]+|10\.20\.0\.[0-9]+|10\.6\.0\.[0-9]+|88\.99\.146\.[0-9]+|root@[0-9]'

# Allowlist: the sanitized onboarding example placeholder.
ALLOW='192\.168\.1\.10'

hits=$(git grep -nE "$PATTERN" -- . ':!scripts/leak_scan.sh' | grep -vE "$ALLOW")

if [ -n "$hits" ]; then
    echo "LEAK SCAN FAILED — site-specific topology in tracked files:" >&2
    echo "$hits" >&2
    exit 1
fi
echo "leak scan clean"
