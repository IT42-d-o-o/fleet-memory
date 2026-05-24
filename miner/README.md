# transcript-miner

Mines Claude Code and Codex session transcripts into fleet memory (mem0 CT356).

## What it does
- Reads all `.jsonl` from `~/.claude/projects/` and `~/.codex/sessions/`
- Sends each session to `qwen3-coder:30b` via local Ollama
- Extracts: facts, decisions, projects, lessons, tools, user preferences
- Writes atomic items to fleet memory at `http://192.168.50.138:8800/mcp`
- Resumable: `checkpoint.json` tracks processed files by path+hash

## Requirements
```
pip install httpx
```
Ollama must be running with `qwen3-coder:30b` pulled.

## Usage
```bash
# Dry run — extract but don't write
python miner.py --dry-run

# Full backfill (825 files, ~2-4 hours)
python miner.py

# Only files since a date
python miner.py --since 2026-04-01

# Test with first 5 files
python miner.py --limit 5 --dry-run

# Resume after crash — just re-run, checkpoint handles skip
python miner.py
```

## Checkpoint
`checkpoint.json` persists after every file. Safe to kill with Ctrl+C and resume.

## Logs
`miner.log` in the same directory.
