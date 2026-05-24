#!/bin/sh
# memory-mcp launcher.
#
# IT42 production: fetches OPENAI_API_KEY from Vault, never written to disk.
# Standalone install: set OPENAI_API_KEY directly in memory-mcp.service or .env.
# Ollama mode: set LLM_PROVIDER=ollama in unit — no API key needed.
set -e

if [ "${LLM_PROVIDER:-openai}" = "openai" ] && [ -z "$OPENAI_API_KEY" ]; then
    # Try Vault if token file exists
    if [ -f /etc/memory-mcp/vault-token ]; then
        export VAULT_ADDR=${VAULT_ADDR:-http://10.10.10.107:8200}
        VAULT_TOKEN=$(cat /etc/memory-mcp/vault-token)
        export VAULT_TOKEN
        OPENAI_API_KEY=$(vault kv get -field=openai_api secret/Mem0)
        export OPENAI_API_KEY
        unset VAULT_TOKEN
    else
        echo "ERROR: LLM_PROVIDER=openai but OPENAI_API_KEY not set and no vault-token found" >&2
        exit 1
    fi
fi

exec /opt/memory-mcp/venv/bin/python /opt/memory-mcp/server.py
