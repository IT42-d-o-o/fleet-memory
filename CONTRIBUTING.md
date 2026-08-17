# Contributing to fleet-memory

## Ways to contribute

- Bug reports — open an issue with reproduction steps
- New transcript source parsers (new AI tools)
- New forge clients (GitHub Enterprise, Bitbucket, etc.)
- LLM backend integrations
- Documentation fixes

## Development setup

```bash
git clone https://github.com/<your-fork>/fleet-memory.git
cd fleet-memory/miner
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set at minimum: LLM_API_KEY + LLM_MODEL (or OLLAMA_URL)
```

Run the miner in dry-run mode (no server needed):
```bash
python miner.py --dry-run --limit 5
```

Run with a local server:
```bash
cd ..
cp .env.example .env
docker compose up -d
cd miner
python miner.py --dry-run --limit 5
```

## Pull requests

- One logical change per PR
- Include a `--dry-run` test showing extraction works
- New transcript parsers: add a sample `.jsonl` snippet in the PR description
- New forge clients: implement `list_repos`, `repo_fingerprint`, `fetch_issues`, `fetch_commits` on the base class in `_forge.py`
- Run `python -m pyflakes miner/miner.py miner/_llm.py miner/_forge.py` — zero warnings required

## Adding a transcript source

1. Add a `parse_<toolname>_transcript(path: Path) -> str` function in `miner/miner.py`
2. Add the source root constant (e.g. `TOOLNAME_SESSIONS_ROOT`)
3. Add discovery logic in `find_all_transcripts()`
4. Add an entry to the `PARSERS` dict
5. Update the source table in `README.md`

## Security

Do not include plaintext credentials, tokens, or personal data in PRs. See `SECURITY.md`.
