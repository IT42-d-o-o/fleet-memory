# Security Policy

## Scope

fleet-memory is a self-hosted tool. The attack surface is:

- The MCP HTTP server (`memory-mcp`, port 8800) — unauthenticated by default
- The miner, which reads local transcript files and sends content to a remote LLM API
- Qdrant — unauthenticated by default, bound to localhost inside Docker

**Out of scope:** vulnerabilities in mem0, Qdrant, or FastMCP themselves — report those upstream.

## Supported versions

The latest commit on `main` is the supported version. No versioned releases yet.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: `security@it42.hr`

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment (what an attacker can achieve)
- Suggested fix if you have one

You will receive a response within 5 business days. Fixes for confirmed vulnerabilities are
released as soon as practical; you will be credited in the commit unless you prefer otherwise.

## Deployment hardening

Before exposing fleet-memory outside your local network:

1. Set `MCP_AUTH_TOKEN` in `.env` — the server requires `Authorization: Bearer <token>` on all requests
2. Put the MCP endpoint behind a reverse proxy with TLS
3. Bind Qdrant to localhost only (default Docker config already does this)
4. Do not store plaintext secrets in fleet memory — store Vault paths or references only
