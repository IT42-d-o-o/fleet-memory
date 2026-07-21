#!/bin/sh
# memory-mcp launcher.
# Vault Agent (vault-agent-mem0.service) handles AppRole login + token renewal
# and writes a live token to the tmpfs sink below. This script reads that token,
# fetches the OpenAI key into the process env only (never to disk), drops the
# token, then execs the server.
set -e

SINK=/run/memory-mcp/vault-token
export VAULT_ADDR=http://10.10.10.107:8200

# wait for Vault Agent to populate the sink (max 60s) - covers boot ordering
i=0
while [ ! -s "$SINK" ]; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "vault-agent sink $SINK not ready after 60s" >&2
    exit 1
  fi
  sleep 1
done

VAULT_TOKEN=$(cat "$SINK")
export VAULT_TOKEN

OPENAI_API_KEY=$(vault kv get -field=openai_api secret/infra/memory-mcp)
export OPENAI_API_KEY

unset VAULT_TOKEN

exec /opt/memory-mcp/venv/bin/python /opt/memory-mcp/server.py
