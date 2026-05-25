#!/bin/bash
# provision.sh — install memory-mcp + Qdrant on a fresh Debian/Ubuntu host.
#
# Usage:
#   1. Copy this directory to the target host.
#   2. Set env vars for your LLM backend (see CONFIGURATION below).
#   3. Run: bash provision.sh
#
# CONFIGURATION
# -------------
# Option A — OpenAI (best dedup quality):
#   export LLM_PROVIDER=openai
#   export OPENAI_API_KEY=sk-...
#
# Option B — Local Ollama (no API key):
#   export LLM_PROVIDER=ollama
#   export OLLAMA_URL=http://127.0.0.1:11434   # or remote host
#   # Ensure these models are pulled in Ollama:
#   #   ollama pull qwen3:8b
#   #   ollama pull nomic-embed-text
#
# After install, the MCP endpoint is at http://<host>:8800/mcp
# Test: curl -X POST http://localhost:8800/mcp \
#         -H 'Content-Type: application/json' \
#         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
QDRANT_VERSION="${QDRANT_VERSION:-1.13.4}"
QDRANT_ARCH="${QDRANT_ARCH:-x86_64}"  # or aarch64

LLM_PROVIDER="${LLM_PROVIDER:-openai}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
MEM0_LLM_MODEL="${MEM0_LLM_MODEL:-gpt-4o-mini}"
MEM0_EMBED_MODEL="${MEM0_EMBED_MODEL:-text-embedding-3-small}"

# Validate
if [ "$LLM_PROVIDER" = "openai" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "ERROR: LLM_PROVIDER=openai requires OPENAI_API_KEY to be set" >&2
    exit 1
fi
if [ "$LLM_PROVIDER" = "ollama" ]; then
    MEM0_LLM_MODEL="${MEM0_LLM_MODEL:-qwen3:8b}"
    MEM0_EMBED_MODEL="${MEM0_EMBED_MODEL:-nomic-embed-text}"
fi

echo "=== memory-mcp provision: LLM_PROVIDER=$LLM_PROVIDER ==="

# --- 1. System packages --------------------------------------------------
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl

# --- 2. Qdrant binary ----------------------------------------------------
if [ ! -f /opt/qdrant/bin/qdrant ]; then
    echo "--- installing Qdrant $QDRANT_VERSION ---"
    useradd -r -s /bin/false qdrant 2>/dev/null || true
    mkdir -p /opt/qdrant/bin /opt/qdrant/config /opt/qdrant/storage/snapshots
    QDRANT_URL="https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-${QDRANT_ARCH}-unknown-linux-musl.tar.gz"
    curl -sL "$QDRANT_URL" | tar -xz -C /opt/qdrant/bin/
    chown -R qdrant:qdrant /opt/qdrant
fi
cp "$SCRIPT_DIR/qdrant-config.yaml" /opt/qdrant/config/config.yaml
chown qdrant:qdrant /opt/qdrant/config/config.yaml

# --- 3. memory-mcp Python env -------------------------------------------
echo "--- installing memory-mcp ---"
useradd -r -s /bin/false -d /home/memory-mcp -m memory-mcp 2>/dev/null || true
mkdir -p /opt/memory-mcp
python3 -m venv /opt/memory-mcp/venv
/opt/memory-mcp/venv/bin/pip install -q --upgrade pip
/opt/memory-mcp/venv/bin/pip install -q -r "$SCRIPT_DIR/requirements.txt"

cp "$SCRIPT_DIR/server.py" /opt/memory-mcp/server.py
cp "$SCRIPT_DIR/run.sh"    /opt/memory-mcp/run.sh
chmod +x /opt/memory-mcp/run.sh
chown -R memory-mcp:memory-mcp /opt/memory-mcp

# --- 4. systemd units ----------------------------------------------------
cp "$SCRIPT_DIR/qdrant.service"     /etc/systemd/system/qdrant.service
cp "$SCRIPT_DIR/memory-mcp.service" /etc/systemd/system/memory-mcp.service

# Patch memory-mcp.service with resolved values
sed -i "s|Environment=LLM_PROVIDER=openai|Environment=LLM_PROVIDER=${LLM_PROVIDER}|" \
    /etc/systemd/system/memory-mcp.service
sed -i "s|Environment=MEM0_LLM_MODEL=gpt-4o-mini|Environment=MEM0_LLM_MODEL=${MEM0_LLM_MODEL}|" \
    /etc/systemd/system/memory-mcp.service
sed -i "s|Environment=MEM0_EMBED_MODEL=text-embedding-3-small|Environment=MEM0_EMBED_MODEL=${MEM0_EMBED_MODEL}|" \
    /etc/systemd/system/memory-mcp.service

# Write credentials to a protected env file (not baked into the unit)
mkdir -p /etc/memory-mcp
if [ "$LLM_PROVIDER" = "openai" ]; then
    printf 'OPENAI_API_KEY=%s\n' "${OPENAI_API_KEY}" > /etc/memory-mcp/env
elif [ "$LLM_PROVIDER" = "ollama" ]; then
    printf 'OLLAMA_URL=%s\n' "${OLLAMA_URL}" > /etc/memory-mcp/env
fi
chmod 600 /etc/memory-mcp/env
chown memory-mcp:memory-mcp /etc/memory-mcp/env

# --- 5. Enable and start -------------------------------------------------
systemctl daemon-reload
systemctl enable qdrant memory-mcp
systemctl restart qdrant
sleep 2
systemctl restart memory-mcp

echo ""
echo "=== Done. ==="
echo "MCP endpoint: http://$(hostname -I | awk '{print $1}'):8800/mcp"
echo "Test: curl -s -X POST http://localhost:8800/mcp \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'"
