#!/usr/bin/env bash
# Smoke test: docker compose up → tools/list responds → write+search roundtrip → down.
# Requires: docker compose, curl, jq.
# Usage: bash tests/smoke_test.sh [--keep]   (--keep skips teardown for debugging)
set -euo pipefail

KEEP=false
[[ "${1:-}" == "--keep" ]] && KEEP=true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL="http://localhost:8800/mcp"
PASS=0; FAIL=0

_ok()   { echo "PASS: $1"; ((PASS++)); }
_fail() { echo "FAIL: $1"; ((FAIL++)); }

mcp_call() {
    curl -s -X POST "$URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d "$1"
}

echo "=== fleet-memory smoke test ==="

# --- start ---
cd "$ROOT"
cp -n .env.example .env.smoke 2>/dev/null || true
# Use a minimal env: litellm path requires LLM_API_KEY; skip if not set for CI
if [[ -z "${LLM_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
    echo "SKIP: no LLM_API_KEY or OPENAI_API_KEY — set one to run full smoke test"
    exit 0
fi

ENV_FILE=$(mktemp)
if [[ -n "${LLM_API_KEY:-}" ]]; then
    printf 'LLM_PROVIDER=litellm\nLLM_API_KEY=%s\nMEM0_LLM_MODEL=openai/gpt-4o-mini\n' \
        "$LLM_API_KEY" > "$ENV_FILE"
else
    printf 'LLM_PROVIDER=openai\nOPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" > "$ENV_FILE"
fi
trap 'rm -f "$ENV_FILE"' EXIT

docker compose --env-file "$ENV_FILE" up -d --build --quiet-pull

echo "Waiting for server..."
for i in $(seq 1 30); do
    if curl -sf -o /dev/null "$URL" 2>/dev/null; then break; fi
    sleep 2
done

# --- tools/list ---
RESP=$(mcp_call '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
TOOLS=$(echo "$RESP" | jq -r '.result.tools | length' 2>/dev/null || echo 0)
[[ "$TOOLS" -eq 2 ]] && _ok "tools/list returns 2 tools" || _fail "tools/list: got $TOOLS tools"

# --- add_memory ---
RESP=$(mcp_call '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"add_memory","arguments":{"content":"smoke test fact: the answer is 42","agent":"smoke-test"}}}')
ADDED=$(echo "$RESP" | jq -r '.result.content[0].text' | jq -r '.results | length' 2>/dev/null || echo 0)
[[ "$ADDED" -ge 1 ]] && _ok "add_memory wrote $ADDED fact(s)" || _fail "add_memory: no results (got: $RESP)"

# --- search_memory ---
RESP=$(mcp_call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_memory","arguments":{"query":"smoke test answer","limit":3}}}')
FOUND=$(echo "$RESP" | jq -r '.result.content[0].text' | jq -r '.results | length' 2>/dev/null || echo 0)
[[ "$FOUND" -ge 1 ]] && _ok "search_memory found $FOUND result(s)" || _fail "search_memory: nothing found"

# --- teardown ---
if [[ "$KEEP" == "false" ]]; then
    docker compose --env-file "$ENV_FILE" down -v --quiet
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
