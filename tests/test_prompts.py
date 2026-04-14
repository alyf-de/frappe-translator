"""Tests for prompt building functions."""

from __future__ import annotations

from frappe_translator.models import TranslationEntry
from frappe_translator.prompts import build_term_extraction_prompt, build_translation_prompt


def _entry(msgid: str, comments: list[str] | None = None) -> TranslationEntry:
    return TranslationEntry(msgid=msgid, comments=comments or [])


class TestBuildTermExtractionPrompt:
    def test_includes_all_msgids_numbered(self) -> None:
        entries = [_entry("Invoice"), _entry("Cancel"), _entry("Save")]
        prompt = build_term_extraction_prompt(entries, batch_index=1)
        assert "1. Invoice" in prompt
        assert "2. Cancel" in prompt
        assert "3. Save" in prompt

    def test_includes_batch_index(self) -> None:
        entries = [_entry("Invoice")]
        prompt = build_term_extraction_prompt(entries, batch_index=3)
        assert "batch 3" in prompt

    def test_includes_json_instruction(self) -> None:
        entries = [_entry("Invoice")]
        prompt = build_term_extraction_prompt(entries, batch_index=1)
        assert "terms" in prompt
        assert "JSON" in prompt


class TestBuildTranslationPrompt:
    def test_includes_msgid(self) -> None:
        entry = _entry("Invoice {0} has been submitted")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert "Invoice {0} has been submitted" in prompt

    def test_includes_glossary_terms_when_provided(self) -> None:
        entry = _entry("Invoice")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={"Invoice": {"de": "Rechnung"}},
            target_languages=["de"],
            style_config={},
        )
        assert "Rechnung" in prompt
        assert "Terminology Glossary" in prompt

    def test_no_glossary_section_when_empty(self) -> None:
        entry = _entry("Save")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert "Glossary" not in prompt

    def test_includes_style_instructions(self) -> None:
        entry = _entry("Save")
        style = {"de": {"formality": "formal", "address": "Sie"}}
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config=style,
        )
        assert "Style Instructions" in prompt
        assert "formal" in prompt

    def test_includes_target_languages(self) -> None:
        entry = _entry("Save")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de", "fr", "es"],
            style_config={},
        )
        assert "de" in prompt
        assert "fr" in prompt
        assert "es" in prompt

    def test_includes_snippets_when_provided(self) -> None:
        entry = _entry("Save")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="File: module/file.py, line 10:\n```py\ncode here\n```",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert "Source Code Context" in prompt
        assert "module/file.py" in prompt

    def test_no_snippet_section_when_empty(self) -> None:
        entry = _entry("Save")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert "Source Code Context" not in prompt
