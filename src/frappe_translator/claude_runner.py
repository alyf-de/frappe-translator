"""Claude CLI subprocess management and concurrency pool."""

from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import PIPE

logger = logging.getLogger(__name__)

# Rate limit detection patterns in claude CLI output
_RATE_LIMIT_PATTERNS = ("rate limit", "rate_limit", "too many requests", "429")

_MAX_RETRIES = 3
_INITIAL_BACKOFF = 10.0  # seconds
_BACKOFF_MULTIPLIER = 2.0


def _is_rate_limited(text: str) -> bool:
    """Check if output indicates a rate limit error."""
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
        self._backoff_lock = asyncio.Lock()
        self._backoff_until = 0.0  # asyncio event loop time when backoff expires

    async def _wait_for_backoff(self) -> None:
        """Wait if a global backoff is active (from a recent rate limit)."""
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._backoff_until > now:
            wait = self._backoff_until - now
            logger.info("Rate limit backoff: waiting %.1fs", wait)
            await asyncio.sleep(wait)

    async def _set_backoff(self, retry: int) -> None:
        """Set a global backoff after a rate limit hit."""
        async with self._backoff_lock:
            delay = _INITIAL_BACKOFF * (_BACKOFF_MULTIPLIER**retry)
            loop = asyncio.get_event_loop()
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

    async def run_batch(self, prompts: list[str], json_schemas: list[str | None] | None = None) -> list[str | None]:
        """Run multiple prompts concurrently and return results in the same order.

        Failed prompts produce None in the result list.
        If json_schemas is provided, each prompt gets its corresponding schema.
        """
        schemas = json_schemas or [None] * len(prompts)
        tasks = [self.run(prompt, json_schema=schema) for prompt, schema in zip(prompts, schemas, strict=True)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: list[str | None] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error("Prompt %d failed: %s", i, result)
                output.append(None)
            else:
                output.append(result)

        return output
