"""Source file reading and snippet extraction."""

from __future__ import annotations

import logging
from pathlib import Path

from frappe_translator.models import SourceSnippet, TranslationEntry

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB


def _read_file_lines(abs_path: Path) -> list[str] | None:
    """Read a file and return its lines, or None if the file should be skipped."""
    try:
        file_size = abs_path.stat().st_size
    except OSError:
        logger.warning("Cannot stat file, skipping: %s", abs_path)
        return None

    if file_size > MAX_FILE_SIZE:
        logger.warning("File too large (>1MB), skipping: %s", abs_path)
        return None

    try:
        raw = abs_path.read_bytes()
        if b"\x00" in raw[:8192]:
            logger.warning("Binary file detected, skipping: %s", abs_path)
            return None
        return raw.decode("utf-8", errors="replace").splitlines()
    except OSError:
        logger.warning("Cannot read file, skipping: %s", abs_path)
        return None


def extract_snippets(
    entry: TranslationEntry,
    app_path: Path,
    file_cache: dict[str, list[str] | None] | None = None,
) -> list[SourceSnippet]:
    """Extract source code snippets for each source reference in an entry.

    For each source_ref of the form "file_path:line_number", reads the file at
    app_path / file_path and extracts lines max(1, line-5) to line+5.

    Args:
        entry: The translation entry with source_refs.
        app_path: Root path of the app.
        file_cache: Optional dict mapping file_part -> lines (or None for unreadable).
            If provided, files are read once and cached for reuse across entries.
    """
    snippets: list[SourceSnippet] = []
    for ref in entry.source_refs:
        if ":" not in ref:
            logger.warning("Skipping malformed source ref (no colon): %r", ref)
            continue
        file_part, _, line_part = ref.rpartition(":")
        try:
            line_number = int(line_part) if line_part else 0
        except ValueError:
            logger.debug("Skipping source ref with non-integer line number: %r", ref)
            continue
        if line_number == 0:
            logger.debug("Skipping source ref with line_number=0: %r", ref)
            continue

        # Use cache if provided
        if file_cache is not None and file_part in file_cache:
            lines = file_cache[file_part]
        else:
            abs_path = (app_path / file_part).resolve()
            if not abs_path.is_relative_to(app_path.resolve()):
                logger.warning("Path traversal attempt blocked: %s", file_part)
                if file_cache is not None:
                    file_cache[file_part] = None
                continue
            if not abs_path.exists():
                logger.warning("Source file not found, skipping: %s", abs_path)
                if file_cache is not None:
                    file_cache[file_part] = None
                continue

            lines = _read_file_lines(abs_path)
            if file_cache is not None:
                file_cache[file_part] = lines

        if not lines:
            continue

        start = max(0, line_number - 6)  # 0-indexed, 5 lines before
        end = min(len(lines), line_number + 5)  # 5 lines after (line_number is 1-indexed)
        content = "\n".join(lines[start:end])

        snippets.append(SourceSnippet(file_path=file_part, line_number=line_number, content=content))

    return snippets


def format_snippets(snippets: list[SourceSnippet]) -> str:
    """Format source snippets for inclusion in a translation prompt."""
    parts: list[str] = []
    for snippet in snippets:
        ext = Path(snippet.file_path).suffix.lstrip(".")
        block = f"File: {snippet.file_path}, line {snippet.line_number}:\n```{ext}\n{snippet.content}\n```"
        parts.append(block)
    return "\n\n".join(parts)
