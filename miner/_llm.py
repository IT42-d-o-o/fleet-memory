"""
Provider-agnostic LLM client for fleet-memory miner.

Self-contained (no external lib) to enable standalone distribution.
Supports Ollama, any OpenAI-compatible endpoint, and Anthropic direct API.
Priority: ANTHROPIC_API_KEY → LLM_BASE_URL → OLLAMA_URL → localhost:11434
"""

import json
import os
import re
from dataclasses import dataclass

import httpx

PUSHGATEWAY_URL = os.getenv("PUSHGATEWAY_URL", "")
LLM_MODEL_ENV = os.getenv("LLM_MODEL", "")


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int


class _OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def extract_json(self, system: str, user: str) -> tuple[list[dict], LLMResponse]:
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        response = LLMResponse(
            content=content,
            model=self.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
        return _parse_json_array(content), response


class _AnthropicClient:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def extract_json(self, system: str, user: str) -> tuple[list[dict], LLMResponse]:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["content"][0]["text"]
        usage = data.get("usage", {})
        response = LLMResponse(
            content=content,
            model=self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        return _parse_json_array(content), response


def create_llm_client(model: str | None = None) -> _OpenAICompatClient | _AnthropicClient:
    """Return the appropriate LLM client based on environment configuration."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        m = model or LLM_MODEL_ENV or "claude-haiku-4-5-20251001"
        return _AnthropicClient(api_key=anthropic_key, model=m)

    llm_base_url = os.getenv("LLM_BASE_URL", "")
    ollama_url = os.getenv("OLLAMA_URL", "")
    api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or "ollama"
    )

    if llm_base_url:
        base = llm_base_url.rstrip("/")
        default_model = "gpt-4o-mini"
    elif ollama_url:
        base = f"{ollama_url.rstrip('/')}/v1"
        default_model = "qwen3:8b"
    else:
        base = "http://localhost:11434/v1"
        default_model = "qwen3:8b"

    m = model or LLM_MODEL_ENV or default_model
    return _OpenAICompatClient(base_url=base, api_key=api_key, model=m)


def push_tokens(model: str, input_tokens: int, output_tokens: int, app: str = "miner") -> None:
    """Push token usage to Prometheus Pushgateway. No-op if PUSHGATEWAY_URL is unset."""
    if not PUSHGATEWAY_URL:
        return
    total = input_tokens + output_tokens
    body = (
        f'# HELP llm_tokens_total Total LLM tokens used\n'
        f'# TYPE llm_tokens_total counter\n'
        f'llm_tokens_total{{model="{model}",app="{app}"}} {total}\n'
    )
    try:
        httpx.post(
            f"{PUSHGATEWAY_URL.rstrip('/')}/metrics/job/llm_usage/app/{app}",
            content=body,
            headers={"Content-Type": "text/plain"},
            timeout=5,
        )
    except Exception:
        pass


def _parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array from LLM output, tolerating markdown fences."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return []
