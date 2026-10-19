"""Tests for the status lines of as_completed_logged."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from frappe_translator import claude_runner
from frappe_translator.claude_runner import ClaudeRunner, as_completed_logged

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_logs_progress_and_heartbeat(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_runner, "_STATUS_INTERVAL", 0.01)
    runner = ClaudeRunner()
    slow_done = asyncio.Event()

    async def fast() -> str:
        return "fast"

    async def slow() -> str:
        # Simulates a claude call that is still running
        runner.running_since["call"] = asyncio.get_running_loop().time()
        await slow_done.wait()
        del runner.running_since["call"]
        return "slow"

    results = []
    with caplog.at_level(logging.INFO):
        async for result in as_completed_logged([fast(), slow()], "Glossary frappe", runner):
            results.append(result)
            if result == "fast":
                asyncio.get_running_loop().call_later(0.05, slow_done.set)

    assert sorted(results) == ["fast", "slow"]
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith("Glossary frappe: 1/2 batches (50%), 1 running, longest 0m00s, elapsed 0m00s, ETA ~")
    # Heartbeat while the slow call runs, without a new result
    assert lines[1].startswith("Glossary frappe: 1/2 batches (50%), 1 running")
    assert lines[-1] == "Glossary frappe: 2/2 batches (100%), elapsed 0m00s"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


async def test_shows_progress_bar_in_terminal(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _Terminal()
    monkeypatch.setattr(claude_runner.sys, "stderr", terminal)

    async def call(i: int) -> int:
        return i

    with caplog.at_level(logging.INFO):
        results = [r async for r in as_completed_logged([call(1), call(2)], "Glossary frappe", ClaudeRunner())]

    assert sorted(results) == [1, 2]
    output = terminal.getvalue()
    assert "Glossary frappe" in output
    assert "2/2" in output
    assert "100%" in output
    # No status log lines while the bar is shown
    assert not caplog.records


async def test_success_output_with_rate_limit_words_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Success envelopes hold UUIDs, long floats and the model's text, which can contain "429" or "rate limit"
    output = json.dumps(
        {
            "is_error": False,
            "session_id": "01d798c9-c394-4c70-a429-e8905fe3a56d",
            "total_cost_usd": 0.004291,
            "result": '{"de": "Rate Limit überschritten"}',
        }
    )
    runner = ClaudeRunner()
    calls = []

    async def execute(prompt: str, json_schema: str | None = None) -> str:
        calls.append(prompt)
        return output

    monkeypatch.setattr(runner, "_execute", execute)

    assert await runner.run("prompt") == output
    assert len(calls) == 1
    assert runner._backoff_until == 0.0


async def test_error_envelope_with_status_429_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_runner, "_INITIAL_BACKOFF", 0.0)
    limited = json.dumps({"is_error": True, "api_error_status": 429, "result": "API Error"})
    success = json.dumps({"is_error": False, "result": "ok"})
    outputs = [limited, success]
    runner = ClaudeRunner()

    async def execute(prompt: str, json_schema: str | None = None) -> str:
        return outputs.pop(0)

    monkeypatch.setattr(runner, "_execute", execute)

    assert await runner.run("prompt") == success
    assert not outputs


_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, select, sys
# Like the claude CLI: give up if the prompt is not on stdin soon after the start
ready, _, _ = select.select([sys.stdin], [], [], 0.2)
if not ready:
    print("Error: Input must be provided either through stdin", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"is_error": False, "result": sys.stdin.read()}))
"""


async def test_prompt_is_on_stdin_even_when_the_event_loop_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "claude"
    fake.write_text(_FAKE_CLAUDE)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    create = asyncio.create_subprocess_exec

    async def create_then_block(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        time.sleep(0.5)  # Other work blocks the event loop before the prompt is written
        return process

    monkeypatch.setattr(claude_runner.asyncio, "create_subprocess_exec", create_then_block)

    output = await ClaudeRunner(timeout=10).run("the prompt")

    assert json.loads(output)["result"] == "the prompt"
