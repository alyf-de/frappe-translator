"""Tests for the status lines of as_completed_logged."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from frappe_translator import claude_runner
from frappe_translator.claude_runner import ClaudeRunner, as_completed_logged

if TYPE_CHECKING:
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
