"""Tests for PO/POT file reading, filtering, and writing."""

from __future__ import annotations

import asyncio
import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from frappe_translator.models import TranslationResult
from frappe_translator.po_handler import (
    POWriter,
    backfill_po,
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

    def test_flush_changes_only_the_translated_msgstr_lines(self, tmp_path: Path) -> None:
        # Crowdin style: unwrapped lines, Crowdin headers, no flags
        long_comment = "#. Description of the 'Very Long Field Name' (Small Text) field in DocType 'Some Settings Doc'"
        original = textwrap.dedent(f"""\
            msgid ""
            msgstr ""
            "Content-Type: text/plain; charset=UTF-8\\n"
            "X-Crowdin-Project: frappe\\n"

            {long_comment}
            #: frappe/a.py:1
            msgid "Hello {{0}}"
            msgstr ""

            #: frappe/b.py:2
            msgid "Line one\\n"
            "Line two"
            msgstr ""

            msgctxt "Verb"
            msgid "Open"
            msgstr ""
            "Old\\n"
            "value"

            msgid "Untouched {{0}}"
            msgstr "Unberührt {{0}}"
            """)
        po_path = tmp_path / "de.po"
        po_path.write_text(original)
        writer = POWriter({"de": po_path})
        for msgid, msgctxt, text in [
            ("Hello {0}", None, "Hallo {0}"),
            ("Line one\nLine two", None, 'Zeile "eins"\nZeile zwei'),
            ("Open", "Verb", "Öffnen"),
        ]:
            writer.buffer_translation(TranslationResult(msgid=msgid, msgctxt=msgctxt, translations={"de": text}))

        asyncio.run(writer.flush_all())

        expected = (
            original.replace('msgid "Hello {0}"\nmsgstr ""', 'msgid "Hello {0}"\nmsgstr "Hallo {0}"')
            .replace(
                # Like Frappe's PO files, each line of a multi-line msgstr gets its own string
                'msgid "Line one\\n"\n"Line two"\nmsgstr ""',
                'msgid "Line one\\n"\n"Line two"\nmsgstr "Zeile \\"eins\\"\\n"\n"Zeile zwei"',
            )
            .replace('msgstr ""\n"Old\\n"\n"value"', 'msgstr "Öffnen"')
        )
        assert po_path.read_text() == expected

    def test_flush_warns_when_an_entry_cannot_be_patched(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Babel reads Windows line endings, but the patch expects "\n"
        po_path = tmp_path / "de.po"
        po_path.write_bytes(b'msgid ""\r\nmsgstr ""\r\n\r\nmsgid "Save"\r\nmsgstr ""\r\n')
        writer = POWriter({"de": po_path})
        writer.buffer_translation(TranslationResult(msgid="Save", translations={"de": "Speichern"}))

        asyncio.run(writer.flush_all())

        assert "Could not write to" in caplog.text
        assert "'Save'" in caplog.text


class TestBackfillPo:
    def test_copies_only_missing_translations(self, tmp_path: Path) -> None:
        header = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        source = tmp_path / "source.po"
        source.write_text(
            header
            + 'msgid "Save"\nmsgstr "Speichern"\n\n'
            + 'msgctxt "Verb"\nmsgid "Open"\nmsgstr "Öffnen"\n\n'
            + 'msgid "Open"\nmsgstr "Offen"\n\n'
            + 'msgid "Cancel"\nmsgstr "Abbrechen (neu)"\n\n'
            + 'msgid "Only in source"\nmsgstr "Nur in der Quelle"\n'
        )
        target = tmp_path / "target.po"
        original = (
            header
            + '#: app/a.py:1\nmsgid "Save"\nmsgstr ""\n\n'
            + 'msgctxt "Verb"\nmsgid "Open"\nmsgstr ""\n\n'
            + 'msgid "Cancel"\nmsgstr "Abbrechen"\n\n'
            + 'msgid "Only in target"\nmsgstr ""\n'
        )
        target.write_text(original)

        assert backfill_po(source, target) == 2

        expected = original.replace('msgid "Save"\nmsgstr ""', 'msgid "Save"\nmsgstr "Speichern"').replace(
            'msgid "Open"\nmsgstr ""', 'msgid "Open"\nmsgstr "Öffnen"'
        )
        assert target.read_text() == expected
