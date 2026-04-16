"""Tests for Pass 1 term extraction."""

from __future__ import annotations

import json

from frappe_translator.models import TranslationEntry
from frappe_translator.term_extractor import extract_terms


class FakeRunner:
    """Records prompts sent to run_batch and returns a canned JSON response per batch."""

    def __init__(self, response: dict | None = None) -> None:
        self.captured_prompts: list[str] = []
        self._response = json.dumps(response if response is not None else {"terms": []})

    async def run_batch(self, prompts: list[str]) -> list[str]:
        self.captured_prompts.extend(prompts)
        return [self._response for _ in prompts]


def _entry(msgid: str, msgctxt: str | None = None) -> TranslationEntry:
    return TranslationEntry(msgid=msgid, msgctxt=msgctxt)


def _numbered_section(prompt: str) -> str:
    """Return the numbered source-strings section of an extraction prompt.

    The system prompt mentions some example terms (e.g. "Invoice", "Submit"),
    so assertions must only look at the input list, which follows "---\n".
    """
    _, _, listing = prompt.partition("---\n")
    return listing


class TestExtractTermsDeduplication:
    async def test_empty_input_returns_empty_glossary(self) -> None:
        runner = FakeRunner()
        glossary = await extract_terms([], runner, batch_size=10)
        assert glossary.terms == {}
        assert runner.captured_prompts == []

    async def test_unique_entries_are_all_sent(self) -> None:
        runner = FakeRunner(response={"terms": ["Rechnung", "Abbrechen", "Speichern"]})
        entries = [_entry("Rechnung"), _entry("Abbrechen"), _entry("Speichern")]

        await extract_terms(entries, runner, batch_size=10)

        assert len(runner.captured_prompts) == 1
        listing = _numbered_section(runner.captured_prompts[0])
        assert "1. Rechnung" in listing
        assert "2. Abbrechen" in listing
        assert "3. Speichern" in listing

    async def test_deduplicates_repeated_msgids_across_apps(self) -> None:
        # Simulate the same msgid appearing in multiple apps' POT files.
        runner = FakeRunner(response={"terms": ["Rechnung"]})
        entries = [
            _entry("Rechnung"),
            _entry("Abbrechen"),
            _entry("Rechnung"),
            _entry("Speichern"),
            _entry("Abbrechen"),
        ]

        await extract_terms(entries, runner, batch_size=50)

        listing = _numbered_section(runner.captured_prompts[0])
        assert listing.count("Rechnung") == 1
        assert listing.count("Abbrechen") == 1
        assert listing.count("Speichern") == 1
        assert "1. Rechnung" in listing
        assert "2. Abbrechen" in listing
        assert "3. Speichern" in listing
        assert "4." not in listing

    async def test_deduplicates_same_msgid_with_different_contexts(self) -> None:
        # Extraction is keyed by source string only, so repeated msgids
        # with distinct msgctxt values should still be sent once.
        runner = FakeRunner(response={"terms": ["Einreichen"]})
        entries = [
            _entry("Einreichen", msgctxt="button"),
            _entry("Einreichen", msgctxt="menu"),
            _entry("Einreichen", msgctxt=None),
        ]

        await extract_terms(entries, runner, batch_size=50)

        listing = _numbered_section(runner.captured_prompts[0])
        assert listing.count("Einreichen") == 1
        assert "1. Einreichen" in listing
        assert "2." not in listing

    async def test_deduplication_preserves_first_occurrence_order(self) -> None:
        runner = FakeRunner(response={"terms": []})
        entries = [
            _entry("Speichern"),
            _entry("Abbrechen"),
            _entry("Speichern"),
            _entry("Rechnung"),
            _entry("Abbrechen"),
        ]

        await extract_terms(entries, runner, batch_size=50)

        listing = _numbered_section(runner.captured_prompts[0])
        assert "1. Speichern" in listing
        assert "2. Abbrechen" in listing
        assert "3. Rechnung" in listing

    async def test_dedup_reduces_batch_count(self) -> None:
        # 10 entries with only 2 unique msgids at batch_size=3 should yield 1 batch, not 4.
        runner = FakeRunner(response={"terms": []})
        entries = [_entry("Rechnung" if i % 2 == 0 else "Abbrechen") for i in range(10)]

        await extract_terms(entries, runner, batch_size=3)

        assert len(runner.captured_prompts) == 1

    async def test_glossary_equivalent_with_and_without_duplicates(self) -> None:
        response = {"terms": ["Rechnung", "Abbrechen"]}

        runner_dup = FakeRunner(response=response)
        entries_dup = [
            _entry("Rechnung"),
            _entry("Abbrechen"),
            _entry("Rechnung"),
            _entry("Abbrechen"),
            _entry("Rechnung"),
        ]
        glossary_dup = await extract_terms(entries_dup, runner_dup, batch_size=50)

        runner_unique = FakeRunner(response=response)
        entries_unique = [_entry("Rechnung"), _entry("Abbrechen")]
        glossary_unique = await extract_terms(entries_unique, runner_unique, batch_size=50)

        assert glossary_dup.terms == glossary_unique.terms
