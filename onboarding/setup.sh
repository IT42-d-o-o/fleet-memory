#!/bin/bash
# fleet-memory setup.sh — one-shot onboarding for new deployments.
#
# What this does:
#   1. Deploys the fleet-memory server (mem0 + Qdrant) on the current host
#   2. Runs a one-time backfill of existing chat transcripts / git repos
#   3. Prints the MCP config snippet to paste into your Claude Code settings
#
# Requirements:
#   - Debian/Ubuntu host with systemd
#   - Python 3.10+
#   - Git (for repo mining)
#
# Usage:
#   export LLM_PROVIDER=openai          # or: ollama
#   export OPENAI_API_KEY=sk-...        # if openai
#   export OLLAMA_URL=http://127.0.0.1:11434  # if ollama
#   bash setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_DIR="$REPO_DIR/server"
MINER_DIR="$REPO_DIR/miner"

LLM_PROVIDER="${LLM_PROVIDER:-openai}"
FLEET_HOST="${FLEET_HOST:-127.0.0.1}"
FLEET_PORT="${FLEET_PORT:-8800}"

echo "=== fleet-memory setup ==="
echo "Provider: $LLM_PROVIDER"
echo "Endpoint: http://$FLEET_HOST:$FLEET_PORT/mcp"
echo ""

# --- 1. Deploy server -------------------------------------------------------
echo "--- Step 1: deploying server ---"
bash "$SERVER_DIR/provision.sh"

# --- 2. Verify server is up -------------------------------------------------
echo "--- Step 2: verifying server ---"
sleep 3
if curl -s -X POST "http://$FLEET_HOST:$FLEET_PORT/mcp" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK — tools:', [t['name'] for t in d['result']['tools']])" 2>/dev/null; then
    echo "Server healthy."
else
    echo "WARNING: server check failed. Check: journalctl -u memory-mcp -f"
fi

# --- 3. Miner backfill ------------------------------------------------------
echo ""
echo "--- Step 3: backfill ---"
echo "Miner supports:"
echo "  (default)             Claude/Codex/Cursor transcripts — auto-discovered"
echo "  --git-roots <dir>     Scan subdirs for git repos"
echo "  --markdown-roots <d>  Scan subdirs for markdown docs"
echo "  --gitea               Mine Gitea instance (set GITEA_URL env var)"
echo ""
echo "Example (run manually after reviewing):"
echo "  cd $MINER_DIR"
echo "  pip install httpx python-dotenv"
echo "  python miner.py --workers 4"
echo ""

# --- 4. Print MCP config snippet -------------------------------------------
echo "=== MCP config snippet ==="
echo "Add to your Claude Code settings (or mcp.json):"
echo ""
cat << EOF
{
  "mcpServers": {
    "memory-mcp": {
      "type": "http",
      "url": "http://${FLEET_HOST}:${FLEET_PORT}/mcp"
    }
  }
}
EOF
echo ""
echo "=== CLAUDE.md behavioral section ==="
echo "Paste this into your CLAUDE.md:"
echo ""
cat "$REPO_DIR/onboarding/claude-md-snippet.md"
echo ""
echo "=== Done. ==="
