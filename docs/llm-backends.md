# LLM Backend Options

fleet-memory works with any LLM backend. Quality vs cost tradeoff is real — documented honestly below.

## Option A — Universal (LiteLLM + fastembed)

One API key, any provider. Embeddings run locally via fastembed — no second key needed.

```bash
export LLM_PROVIDER=litellm
export LLM_API_KEY=<your key>
export MEM0_LLM_MODEL=openai/gpt-4o-mini           # OpenAI
# export MEM0_LLM_MODEL=anthropic/claude-3-5-haiku-20241022  # Anthropic
# export MEM0_LLM_MODEL=openrouter/anthropic/claude-3-5-haiku  # OpenRouter
```

LiteLLM model strings: `<provider>/<model>` — [full list](https://docs.litellm.ai/docs/providers).

**Tradeoff**: local fastembed embeddings (`BAAI/bge-small-en-v1.5`, ~130MB downloaded on first start) are slightly lower quality than OpenAI's `text-embedding-3-small` for deduplication. Acceptable for most use cases.

## Option B — OpenAI (recommended for quality)

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

## Option B — Anthropic

mem0 uses Claude for LLM reasoning. Anthropic has no embeddings API, so you must also provide an embedder — OpenAI or Ollama.

```bash
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export MEM0_LLM_MODEL=claude-3-5-haiku-20241022

# embedder — pick one:
export OPENAI_API_KEY=sk-...          # OpenAI text-embedding-3-small
# OR
export OLLAMA_URL=http://127.0.0.1:11434  # Ollama nomic-embed-text (free)
```

## Option C — OpenRouter (or any OpenAI-compatible endpoint)

Set `OPENAI_API_BASE` to the provider's base URL. OpenRouter has no embeddings endpoint, so add Ollama for embeddings (or drop `OLLAMA_URL` to fall back to OpenAI embeddings).

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-or-...
export OPENAI_API_BASE=https://openrouter.ai/api/v1
export MEM0_LLM_MODEL=anthropic/claude-3-5-haiku
export OLLAMA_URL=http://127.0.0.1:11434   # embeddings via Ollama
```

Works with Together AI, Groq, Mistral, or any OpenAI-compatible endpoint — just change `OPENAI_API_BASE` and `MEM0_LLM_MODEL`.

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
