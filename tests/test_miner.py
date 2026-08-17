"""
Unit tests for fleet-memory miner.

Run: cd miner && pip install pytest && pytest ../tests/test_miner.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# miner/ is not a package — add it to path so imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent / "miner"))

import httpx

from miner import (  # noqa: E402
    LLMRateLimitError,
    PARSERS,
    add_to_fleet_memory,
    chunk_text,
    extract_facts,
    parse_claude_transcript,
    process_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseClaudeTranscript:
    def test_extracts_user_and_assistant_turns(self):
        path = FIXTURES / "claude_sample.jsonl"
        result = parse_claude_transcript(path)
        assert "USER: Deploy the new auth service to production." in result
        assert "CLAUDE: I'll deploy the auth service now." in result

    def test_handles_list_content_blocks(self):
        path = FIXTURES / "claude_sample.jsonl"
        result = parse_claude_transcript(path)
        assert "USER: Use JWT tokens, not session cookies." in result

    def test_skips_empty_user_messages(self):
        path = FIXTURES / "claude_sample.jsonl"
        result = parse_claude_transcript(path)
        lines = [l for l in result.splitlines() if l.startswith("USER: ")]
        assert all(l.strip() != "USER:" for l in lines)

    def test_returns_empty_string_for_missing_file(self):
        result = parse_claude_transcript(Path("/nonexistent/path.jsonl"))
        assert result == ""

    def test_truncates_assistant_content_at_500_chars(self, tmp_path):
        long_text = "x" * 1000
        line = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": long_text}})
        p = tmp_path / "t.jsonl"
        p.write_text(line, encoding="utf-8")
        result = parse_claude_transcript(p)
        assert len(result) <= 510  # "CLAUDE: " + 500 chars


class TestChunkText:
    def test_short_text_returns_single_chunk(self):
        text = "hello world"
        assert chunk_text(text) == ["hello world"]

    def test_empty_text_returns_empty_list(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_long_text_splits_into_multiple_chunks(self):
        text = ("line\n" * 3000)  # well over CHUNK_CHARS
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert chunk.strip()


class TestParsers:
    def test_all_expected_sources_present(self):
        expected = {"claude", "codex", "antigravity", "cursor", "openclaw", "markdown", "git"}
        assert set(PARSERS.keys()) == expected

    def test_each_entry_is_callable_fn_and_optional_prompt(self):
        for source, (fn, prompt) in PARSERS.items():
            assert callable(fn), f"PARSERS[{source!r}] fn is not callable"
            assert prompt is None or isinstance(prompt, str), \
                f"PARSERS[{source!r}] prompt must be str or None"


class TestAddToFleetMemory:
    def test_dry_run_returns_true_without_http(self):
        result = add_to_fleet_memory("test fact", "lesson", dry_run=True)
        assert result is True

    def test_real_write_posts_correct_payload(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"result": {}}

        with patch("miner.httpx.post", return_value=mock_resp) as mock_post:
            result = add_to_fleet_memory("SQLite WAL mode required for concurrent reads", "lesson", dry_run=False)

        assert result is True
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["json"]
        assert payload["method"] == "tools/call"
        assert payload["params"]["arguments"]["content"] == "SQLite WAL mode required for concurrent reads"
        assert payload["params"]["arguments"]["metadata"]["category"] == "lesson"

    def test_http_error_returns_false(self):
        with patch("miner.httpx.post", side_effect=httpx.ConnectError("refused")):
            result = add_to_fleet_memory("fact", "project", dry_run=False)

        assert result is False


def _make_429_response():
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429 Too Many Requests", request=MagicMock(), response=mock_resp
    )
    return mock_resp


def _make_200_response(facts_json='[{"category": "lesson", "content": "SQLite WAL mode prevents deadlocks"}]'):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": facts_json}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return mock_resp


class TestExtractFactsRetry:
    def test_succeeds_after_two_429s(self):
        responses = [_make_429_response(), _make_429_response(), _make_200_response()]
        with patch("_llm.httpx.post", side_effect=responses), patch("miner.time.sleep"):
            result = extract_facts("SQLite WAL mode prevents deadlocks under load", "gpt-4o-mini")
        assert len(result) == 1
        assert result[0]["category"] == "lesson"

    def test_raises_llm_rate_limit_error_after_three_429s(self):
        responses = [_make_429_response(), _make_429_response(), _make_429_response()]
        with patch("_llm.httpx.post", side_effect=responses), patch("miner.time.sleep"):
            with pytest.raises(LLMRateLimitError):
                extract_facts("some text", "gpt-4o-mini")

    def test_non_429_http_error_returns_empty_no_raise(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_resp
        )
        with patch("_llm.httpx.post", return_value=mock_resp):
            result = extract_facts("some text", "gpt-4o-mini")
        assert result == []


class TestCheckpointOnRateLimit:
    def test_rate_limited_file_not_checkpointed(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            ('{"type":"user","message":{"role":"user","content":"Deploy auth service to prod."}}\n' * 60),
            encoding="utf-8",
        )
        responses = [_make_429_response(), _make_429_response(), _make_429_response()]
        with patch("_llm.httpx.post", side_effect=responses), patch("miner.time.sleep"):
            n_facts, checkpoint_entry = process_file(transcript, "claude", None, True, "gpt-4o-mini")

        assert checkpoint_entry is None
        assert n_facts == 0

    def test_successful_file_is_checkpointed(self, tmp_path):
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            ('{"type":"user","message":{"role":"user","content":"Deploy auth service to prod."}}\n' * 60),
            encoding="utf-8",
        )
        with patch("_llm.httpx.post", return_value=_make_200_response()), \
             patch("miner.add_to_fleet_memory", return_value=True):
            n_facts, checkpoint_entry = process_file(transcript, "claude", None, True, "gpt-4o-mini")

        assert checkpoint_entry is not None
        assert "file_hash" in checkpoint_entry
