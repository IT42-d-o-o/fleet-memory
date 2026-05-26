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

from miner import (  # noqa: E402
    PARSERS,
    add_to_fleet_memory,
    chunk_text,
    parse_claude_transcript,
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
        import httpx as _httpx

        with patch("miner.httpx.post", side_effect=_httpx.ConnectError("refused")):
            result = add_to_fleet_memory("fact", "project", dry_run=False)

        assert result is False
