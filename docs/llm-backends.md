# LLM Backend Options

fleet-memory works with any LLM backend. Quality vs cost tradeoff is real — documented honestly below.

## Option A — OpenAI (recommended for quality)

Best deduplication, best extraction quality from noisy transcripts.

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
```

Models: `gpt-4o-mini` (embeddings: `text-embedding-3-small`) — set via:
```bash
export MEM0_LLM_MODEL=gpt-4o-mini
export MEM0_EMBED_MODEL=text-embedding-3-small
```

**Cost**: ~$0.01-0.05 per backfill session. Ongoing session writes are trivial.

## Option B — OpenRouter

Any model via OpenRouter API. Use `LLM_API_KEY` + `LLM_BASE_URL`:

```bash
export LLM_API_KEY=sk-or-...
export LLM_BASE_URL=https://openrouter.ai/api/v1
export LLM_MODEL=anthropic/claude-3-haiku
```

## Option C — Local Ollama (no API key)

Free, private, runs on your own hardware. Quality lower than GPT-4o-mini.

```bash
export LLM_PROVIDER=ollama
export OLLAMA_URL=http://127.0.0.1:11434
# Pull these models first:
#   ollama pull qwen3:8b          (fast, acceptable quality)
#   ollama pull nomic-embed-text  (embeddings)
```

**Quality note**: 8b models miss ~40-60% of facts from terse structured content (git issues, commit
messages). Use `qwen3:30b` or larger for backfill if quality matters. 8b fine for transcript mining.

## Docker + Ollama

`host.docker.internal` resolves to the host machine in both Docker Desktop (Windows/Mac) and Linux — the compose file wires it via `extra_hosts: host-gateway` automatically.

```bash
# .env
LLM_PROVIDER=ollama
OLLAMA_URL=http://host.docker.internal:11434
```

Pull required models on the host **before** `docker compose up`:
```bash
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

## Miner LLM (separate from server LLM)

The miner (`miner.py`) uses a separate LLM for extraction. Same env vars apply.
For backfill, `--model` flag overrides the model:

```bash
python miner.py --model qwen3-coder:30b --workers 4 --gitea
```

Recommended:
- Backfill: `qwen3-coder:30b` or `gpt-4o-mini`
- Live session monitoring: `qwen3:8b` (speed > quality for incremental)
