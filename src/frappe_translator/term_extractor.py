"""Pass 1: Batch term extraction via claude CLI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from frappe_translator.models import TermGlossary, TranslationEntry
from frappe_translator.prompts import build_term_extraction_prompt
from frappe_translator.validation import parse_claude_json

if TYPE_CHECKING:
    from frappe_translator.claude_runner import ClaudeRunner

logger = logging.getLogger(__name__)


async def extract_terms(
    entries: list[TranslationEntry],
    runner: ClaudeRunner,
    batch_size: int = 50,
) -> TermGlossary:
    """Extract key terms from entries in batches using claude CLI.

    Entries are deduplicated by msgid before batching — the extraction prompt only
    uses the source string, so repeated msgids across apps or contexts would
    otherwise waste prompt tokens and batch capacity.

    Returns a TermGlossary with extracted terms (translations looked up separately).
    """
    glossary = TermGlossary()

    if not entries:
        return glossary

    # Deduplicate by msgid, preserving the first occurrence.
    seen: set[str] = set()
    unique_entries: list[TranslationEntry] = []
    for entry in entries:
        if entry.msgid in seen:
            continue
        seen.add(entry.msgid)
        unique_entries.append(entry)

    duplicates = len(entries) - len(unique_entries)
    if duplicates:
        logger.info(
            "Pass 1: deduplicated %d repeated msgids (%d -> %d unique entries)",
            duplicates,
            len(entries),
            len(unique_entries),
        )

    batches: list[list[TranslationEntry]] = []
    batches.extend(unique_entries[i : i + batch_size] for i in range(0, len(unique_entries), batch_size))
    logger.info("Pass 1: Extracting terms from %d entries in %d batches", len(unique_entries), len(batches))

    # Build prompts for all batches
    prompts = [build_term_extraction_prompt(batch, i + 1) for i, batch in enumerate(batches)]

    # Run all batch prompts concurrently
    results = await runner.run_batch(prompts)

    # Parse results and merge into glossary
    all_terms: set[str] = set()
    for i, result in enumerate(results):
        if result is None:
            logger.warning("Pass 1 batch %d failed, skipping", i + 1)
            continue

        try:
            data = parse_claude_json(result)
            if not isinstance(data, dict):
                logger.warning("Batch %d: expected dict, got %s", i + 1, type(data))
                continue
            terms = data.get("terms", [])
            if isinstance(terms, list):
                all_terms.update(str(t) for t in terms if t)
                logger.info("Batch %d: extracted %d terms", i + 1, len(terms))
            else:
                logger.warning("Batch %d: 'terms' is not a list: %s", i + 1, type(terms))
        except (ValueError, KeyError) as e:
            logger.warning("Pass 1 batch %d: failed to parse response: %s", i + 1, e)

    # Initialize glossary with empty translations (to be filled by lookup)
    for term in sorted(all_terms):
        glossary.terms[term] = {}

    logger.info("Pass 1 complete: %d unique terms extracted", len(glossary.terms))
    return glossary
