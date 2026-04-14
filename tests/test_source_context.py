"""Tests for source file snippet extraction and formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from frappe_translator.models import TranslationEntry
from frappe_translator.source_context import extract_snippets, format_snippets


def _entry(msgid: str, refs: list[str]) -> TranslationEntry:
    return TranslationEntry(msgid=msgid, source_refs=refs)


class TestExtractSnippets:
    def test_extracts_lines_around_reference(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        # The source file has the Invoice string on line 14 (1-indexed)
        # The ref in the POT points to sample_app/module/file.py:42 but
        # the actual fixture file.py is at sample_app/module/file.py with content on line 14
        # Use the actual line from conftest: line 14 is the frappe.throw(_("Invoice...")) line
        entry = _entry("Invoice {0} has been submitted", ["sample_app/module/file.py:14"])
        snippets = extract_snippets(entry, app_path)
        assert len(snippets) == 1
        assert "Invoice {0} has been submitted" in snippets[0].content

    def test_snippet_has_correct_metadata(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Invoice {0} has been submitted", ["sample_app/module/file.py:14"])
        snippets = extract_snippets(entry, app_path)
        assert snippets[0].file_path == "sample_app/module/file.py"
        assert snippets[0].line_number == 14

    def test_handles_missing_file_gracefully(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Something", ["sample_app/nonexistent/file.py:10"])
        snippets = extract_snippets(entry, app_path)
        assert snippets == []

    def test_handles_line_number_zero(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Something", ["sample_app/module/file.py:0"])
        snippets = extract_snippets(entry, app_path)
        assert snippets == []

    def test_handles_empty_refs(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Something", [])
        snippets = extract_snippets(entry, app_path)
        assert snippets == []

    def test_multiple_refs_returns_multiple_snippets(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Cancel", ["sample_app/module/file.py:14", "sample_app/module/file.py:18"])
        snippets = extract_snippets(entry, app_path)
        assert len(snippets) == 2


class TestFormatSnippets:
    def test_produces_file_path_header(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Invoice {0} has been submitted", ["sample_app/module/file.py:14"])
        snippets = extract_snippets(entry, app_path)
        formatted = format_snippets(snippets)
        assert "sample_app/module/file.py" in formatted
        assert "line 14" in formatted

    def test_produces_code_fence(self, tmp_bench: Path) -> None:
        app_path = tmp_bench / "apps" / "sample_app"
        entry = _entry("Invoice {0} has been submitted", ["sample_app/module/file.py:14"])
        snippets = extract_snippets(entry, app_path)
        formatted = format_snippets(snippets)
        assert "```py" in formatted
        assert "```" in formatted

    def test_empty_snippets_returns_empty_string(self) -> None:
        assert format_snippets([]) == ""
