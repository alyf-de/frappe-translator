"""Tests for PO/POT file reading, filtering, and writing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from frappe_translator.models import TranslationResult
from frappe_translator.po_handler import (
    POWriter,
    filter_entries,
    read_po_translations,
    read_pot_entries,
)


class TestReadPotEntries:
    def test_reads_all_four_entries(self, tmp_bench: Path) -> None:
        pot_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        entries = read_pot_entries(pot_path)
        assert len(entries) == 4

    def test_entry_msgids(self, tmp_bench: Path) -> None:
        pot_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        entries = read_pot_entries(pot_path)
        msgids = {e.msgid for e in entries}
        assert "Invoice {0} has been submitted" in msgids
        assert "Cancel" in msgids
        assert "Save" in msgids
        assert "Hello World" in msgids

    def test_entry_has_source_refs(self, tmp_bench: Path) -> None:
        pot_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        entries = read_pot_entries(pot_path)
        invoice_entry = next(e for e in entries if e.msgid == "Invoice {0} has been submitted")
        assert len(invoice_entry.source_refs) >= 1
        assert any("file.py" in ref for ref in invoice_entry.source_refs)

    def test_entry_has_comments(self, tmp_bench: Path) -> None:
        pot_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        entries = read_pot_entries(pot_path)
        invoice_entry = next(e for e in entries if e.msgid == "Invoice {0} has been submitted")
        # The POT fixture has "#. Description of a field" as a comment
        assert any("Description" in c for c in invoice_entry.comments)


class TestReadPoTranslations:
    def test_reads_translations_keyed_by_msgid_msgctxt(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        translations = read_po_translations(de_path)
        # Key is (msgid, msgctxt); msgctxt is None for these entries
        assert ("Invoice {0} has been submitted", None) in translations
        assert translations[("Invoice {0} has been submitted", None)] == "Rechnung {0} wurde eingereicht"

    def test_translated_cancel(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        translations = read_po_translations(de_path)
        assert translations[("Cancel", None)] == "Abbrechen"

    def test_empty_translations_present(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        translations = read_po_translations(de_path)
        assert translations[("Save", None)] == ""
        assert translations[("Hello World", None)] == ""


class TestFilterEntries:
    def _de_translations(self, tmp_bench: Path) -> dict:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        return read_po_translations(de_path)

    def _pot_entries(self, tmp_bench: Path) -> list:
        pot_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        return read_pot_entries(pot_path)

    def test_fill_missing_returns_empty_entries(self, tmp_bench: Path) -> None:
        entries = self._pot_entries(tmp_bench)
        po_trans = self._de_translations(tmp_bench)
        filtered = filter_entries(entries, po_trans, "fill-missing")
        msgids = {e.msgid for e in filtered}
        # Save and Hello World are empty in de.po
        assert "Save" in msgids
        assert "Hello World" in msgids
        # Invoice and Cancel are translated
        assert "Invoice {0} has been submitted" not in msgids
        assert "Cancel" not in msgids

    def test_review_existing_returns_translated_entries(self, tmp_bench: Path) -> None:
        entries = self._pot_entries(tmp_bench)
        po_trans = self._de_translations(tmp_bench)
        filtered = filter_entries(entries, po_trans, "review-existing")
        msgids = {e.msgid for e in filtered}
        assert "Invoice {0} has been submitted" in msgids
        assert "Cancel" in msgids
        assert "Save" not in msgids
        assert "Hello World" not in msgids

    def test_full_correct_returns_all_entries(self, tmp_bench: Path) -> None:
        entries = self._pot_entries(tmp_bench)
        po_trans = self._de_translations(tmp_bench)
        filtered = filter_entries(entries, po_trans, "full-correct")
        assert len(filtered) == 4


class TestPOWriter:
    def test_buffer_translation_adds_to_locale(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        writer = POWriter({"de": de_path})
        result = TranslationResult(msgid="Save", translations={"de": "Speichern"})
        writer.buffer_translation(result)
        assert writer.pending_count == 1

    def test_buffer_ignores_unknown_locale(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        writer = POWriter({"de": de_path})
        result = TranslationResult(msgid="Save", translations={"es": "Guardar"})
        writer.buffer_translation(result)
        assert writer.pending_count == 0

    def test_flush_all_writes_to_file(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        writer = POWriter({"de": de_path})
        result = TranslationResult(msgid="Save", translations={"de": "Speichern"})
        writer.buffer_translation(result)

        asyncio.run(writer.flush_all())

        # Verify file was updated
        updated = read_po_translations(de_path)
        assert updated[("Save", None)] == "Speichern"

    def test_flush_all_clears_buffer(self, tmp_bench: Path) -> None:
        de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
        writer = POWriter({"de": de_path})
        result = TranslationResult(msgid="Save", translations={"de": "Speichern"})
        writer.buffer_translation(result)

        asyncio.run(writer.flush_all())

        assert writer.pending_count == 0
