"""Tests for prompt building functions."""

from __future__ import annotations

import json

from frappe_translator.models import TranslationEntry
from frappe_translator.prompts import (
    build_batch_translation_prompt,
    build_term_extraction_prompt,
    build_translation_prompt,
    build_translation_schema,
)


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

    def test_includes_msgctxt_as_null_when_absent(self) -> None:
        entry = _entry("Save")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert "msgctxt: null" in prompt

    def test_includes_msgctxt_value_when_present(self) -> None:
        entry = TranslationEntry(msgid="Save", msgctxt="Button in form")
        prompt = build_translation_prompt(
            entry=entry,
            snippets_text="",
            glossary_terms={},
            target_languages=["de"],
            style_config={},
        )
        assert 'msgctxt: "Button in form"' in prompt


class TestBuildTranslationSchema:
    def test_msgctxt_is_required_and_nullable(self) -> None:
        schema = json.loads(build_translation_schema(["de", "fr"]))
        items = schema["properties"]["translations"]["items"]
        assert "msgctxt" in items["required"]
        assert items["properties"]["msgctxt"]["type"] == ["string", "null"]

    def test_msgid_and_languages_required(self) -> None:
        schema = json.loads(build_translation_schema(["de", "fr"]))
        items = schema["properties"]["translations"]["items"]
        assert set(items["required"]) == {"msgid", "msgctxt", "de", "fr"}


class TestBuildBatchTranslationPrompt:
    def _entry_info(self, msgid: str, msgctxt: str | None = None) -> dict:
        return {
            "msgid": msgid,
            "msgctxt": msgctxt,
            "comments": [],
            "snippets_text": "",
            "source_files": [],
        }

    def test_msgctxt_always_printed(self) -> None:
        prompt = build_batch_translation_prompt(
            entries=[
                self._entry_info("Save"),
                self._entry_info("Cancel", msgctxt="Button in dialog"),
            ],
            shared_glossary={},
            target_languages=["de"],
            style_config={},
        )
        assert "msgctxt: null" in prompt
        assert 'msgctxt: "Button in dialog"' in prompt

    def test_distinguishes_same_msgid_different_msgctxt(self) -> None:
        prompt = build_batch_translation_prompt(
            entries=[
                self._entry_info("Discard", msgctxt="Button in web form"),
                self._entry_info("Discard", msgctxt="Email discard action"),
            ],
            shared_glossary={},
            target_languages=["de"],
            style_config={},
        )
        assert prompt.count('msgid: "Discard"') == 2
        assert 'msgctxt: "Button in web form"' in prompt
        assert 'msgctxt: "Email discard action"' in prompt

    def test_msgid_with_quotes_is_json_escaped(self) -> None:
        prompt = build_batch_translation_prompt(
            entries=[self._entry_info('Click "OK" to confirm')],
            shared_glossary={},
            target_languages=["de"],
            style_config={},
        )
        assert r'msgid: "Click \"OK\" to confirm"' in prompt

    def test_echo_instruction_present(self) -> None:
        prompt = build_batch_translation_prompt(
            entries=[self._entry_info("Save")],
            shared_glossary={},
            target_languages=["de"],
            style_config={},
        )
        assert "echo" in prompt.lower()
        assert "msgctxt" in prompt
