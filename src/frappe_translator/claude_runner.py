"""Claude CLI subprocess management and concurrency pool."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from asyncio.subprocess import PIPE
from typing import TYPE_CHECKING, TypeVar

import click

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Iterable

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Rate limit detection patterns in claude CLI output
_RATE_LIMIT_PATTERNS = ("rate limit", "rate_limit", "too many requests", "429")

_MAX_RETRIES = 3
_INITIAL_BACKOFF = 10.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

_STATUS_INTERVAL = 60.0  # seconds between status lines while no call finishes
_BAR_INTERVAL = 1.0  # seconds between redraws of the progress bar


def _is_rate_limited(text: str) -> bool:
    """Check if claude CLI output or an error message indicates a rate limit error.

    A success envelope holds UUIDs, numbers and the model's text, which often contain "429",
    so only the error fields of an envelope count.
    """
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict):
        if not envelope.get("is_error"):
            return False
        if envelope.get("api_error_status") == 429:
            return True
        text = str(envelope.get("result", ""))
    text_lower = text.lower()
    return any(p in text_lower for p in _RATE_LIMIT_PATTERNS)


class ClaudeRunner:
    """Manages concurrent Claude CLI subprocess calls with automatic backoff."""

    def __init__(self, concurrency: int = 5, model: str | None = None, timeout: int = 120) -> None:
        self.concurrency = concurrency
        self.model = model
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self.total_calls = 0
        self.errors = 0
        self.rate_limit_hits = 0
        self._backoff_lock = asyncio.Lock()
        self._backoff_until = 0.0  # asyncio event loop time when backoff expires
        self.running_since: dict[object, float] = {}  # event loop start time of each running call

    async def _wait_for_backoff(self) -> None:
        """Wait if a global backoff is active (from a recent rate limit)."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._backoff_until > now:
            wait = self._backoff_until - now
            logger.info("Rate limit backoff: waiting %.1fs", wait)
            await asyncio.sleep(wait)

    async def _set_backoff(self, retry: int) -> None:
        """Set a global backoff after a rate limit hit."""
        self.rate_limit_hits += 1
        async with self._backoff_lock:
            delay = _INITIAL_BACKOFF * (_BACKOFF_MULTIPLIER**retry)
            loop = asyncio.get_running_loop()
            new_until = loop.time() + delay
            # Only extend, never shorten
            if new_until > self._backoff_until:
                self._backoff_until = new_until
                logger.warning("Rate limited — backing off %.1fs (retry %d/%d)", delay, retry + 1, _MAX_RETRIES)

    async def run(self, prompt: str, json_schema: str | None = None) -> str:
        """Execute a single Claude CLI call with automatic retry on rate limits."""
        for retry in range(_MAX_RETRIES + 1):
            await self._wait_for_backoff()
            try:
                result = await self._execute(prompt, json_schema=json_schema)
                # Check if the response itself is a rate limit error
                if _is_rate_limited(result):
                    if retry < _MAX_RETRIES:
                        await self._set_backoff(retry)
                        continue
                    raise RuntimeError(f"Rate limited after {_MAX_RETRIES} retries")
                return result
            except RuntimeError as e:
                if _is_rate_limited(str(e)) and retry < _MAX_RETRIES:
                    await self._set_backoff(retry)
                    continue
                raise

        raise RuntimeError(f"Rate limited after {_MAX_RETRIES} retries")

    async def _execute(self, prompt: str, json_schema: str | None = None) -> str:
        """Execute a single Claude CLI call (no retry logic)."""
        async with self._semaphore:
            call = object()
            self.running_since[call] = asyncio.get_running_loop().time()
            try:
                model_args = ["--model", self.model] if self.model else []
                schema_args = ["--json-schema", json_schema] if json_schema else []
                process = await asyncio.create_subprocess_exec(
                    "claude",
                    "-p",
                    "--output-format",
                    "json",
                    "--tools",
                    "",
                    "--disable-slash-commands",
                    "--no-session-persistence",
                    *model_args,
                    *schema_args,
                    stdin=PIPE,
                    stdout=PIPE,
                    stderr=PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(input=prompt.encode("utf-8")),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    self.errors += 1
                    raise RuntimeError(f"Claude CLI timed out after {self.timeout}s") from None
                finally:
                    self.total_calls += 1

                stdout = stdout_bytes.decode("utf-8") if stdout_bytes else ""
                stderr = stderr_bytes.decode("utf-8") if stderr_bytes else ""

                if process.returncode != 0:
                    if stdout:
                        logger.debug(
                            "Claude CLI exited with code %d but produced stdout; using output. stderr: %s",
                            process.returncode,
                            stderr[:200],
                        )
                        return stdout
                    self.errors += 1
                    raise RuntimeError(f"Claude CLI exited with code {process.returncode}. stderr: {stderr[:500]}")

                return stdout
            finally:
                del self.running_since[call]

    async def run_batch(self, prompts: list[str], label: str) -> list[str | None]:
        """Run multiple prompts concurrently and return results in the same order.

        Failed prompts produce None in the result list.
        """

        async def run_one(i: int) -> tuple[int, str | None]:
            try:
                return i, await self.run(prompts[i])
            except Exception as e:
                logger.error("Prompt %d failed: %s", i, e)
                return i, None

        output: list[str | None] = [None] * len(prompts)
        async for i, result in as_completed_logged([run_one(i) for i in range(len(prompts))], label, self):
            output[i] = result
        return output


async def as_completed_logged(aws: Iterable[Awaitable[T]], label: str, runner: ClaudeRunner) -> AsyncIterator[T]:
    """Yield the results of ``aws`` as they finish.

    In a terminal, shows a progress bar on stderr. Otherwise (e.g. cron), logs a status line
    after each result, and every minute while no call finishes.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    pending = {asyncio.ensure_future(aw) for aw in aws}
    total = len(pending)
    if not total:
        return
    # click's own ETA is wrong before the first result, so the status text has ours
    bar = (
        click.progressbar(
            length=total, label=label, file=sys.stderr, show_pos=True, show_percent=True, item_show_func=lambda s: s
        )
        if sys.stderr.isatty()
        else None
    )
    with bar or contextlib.nullcontext():
        while pending:
            interval = _BAR_INTERVAL if bar else _STATUS_INTERVAL
            done, pending = await asyncio.wait(pending, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                yield task.result()
            finished = total - len(pending)
            now = loop.time()
            elapsed = now - start
            parts = []
            if runner.running_since:
                longest = now - min(runner.running_since.values())
                parts.append(f"{len(runner.running_since)} running, longest {format_duration(longest)}")
            parts.append(f"elapsed {format_duration(elapsed)}")
            if 0 < finished < total:
                parts.append(f"ETA ~{format_duration(elapsed / finished * (total - finished))}")
            status = ", ".join(parts)
            if bar:
                bar.update(len(done), status)
            else:
                logger.info("%s: %d/%d batches (%d%%), %s", label, finished, total, finished * 100 // total, status)


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"
