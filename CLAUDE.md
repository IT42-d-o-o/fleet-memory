# Project: fleet-memory

## Stack
Backend: Python — reason: LLM/mem0 ecosystem is Python-native
Frontend: none
Auth tier: none (MCP transport auth is external concern)
Multi-tenant: no

## Key paths
- MCP server: `server/server.py`
- Miner entry point: `miner/miner.py`
- LLM client: `miner/_llm.py`
- Forge clients (GitHub/GitLab/Gitea): `miner/_forge.py`
- Server provisioning: `server/provision.sh`

## Run locally
```bash
cd miner
pip install -r requirements.txt
cp .env.example .env
python miner.py --dry-run
```

## Deploy server
```bash
bash server/provision.sh   # Debian/Ubuntu — installs mem0 + Qdrant as systemd services
```

## Standards
See global: `C:\Users\tomis\.claude\coding-standards.md`

**Exception:** LLM client kept self-contained (`miner/_llm.py`, only `httpx` dep) to avoid
external library coupling and enable standalone distribution.
