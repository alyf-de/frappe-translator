"""Tests for validation utilities."""

from __future__ import annotations

import pytest

from frappe_translator.validation import (
    extract_placeholders,
    parse_claude_json,
    validate_placeholders,
)


class TestExtractPlaceholders:
    def test_finds_numeric_placeholder(self) -> None:
        assert "{0}" in extract_placeholders("Invoice {0} submitted")

    def test_finds_percent_s(self) -> None:
        assert "%s" in extract_placeholders("Hello %s")

    def test_finds_named_placeholder(self) -> None:
        assert "{name}" in extract_placeholders("Hello {name}")

    def test_finds_dollar_placeholder(self) -> None:
        assert "${values.x}" in extract_placeholders("Value is ${values.x}")

    def test_finds_html_tag(self) -> None:
        assert "<b>" in extract_placeholders("Click <b>here</b>")

    def test_finds_html_entity(self) -> None:
        assert "&amp;" in extract_placeholders("Tom &amp; Jerry")

    def test_returns_empty_for_plain_text(self) -> None:
        assert extract_placeholders("Hello World") == set()

    def test_returns_empty_for_empty_string(self) -> None:
        assert extract_placeholders("") == set()


class TestValidatePlaceholders:
    def test_returns_empty_when_all_preserved(self) -> None:
        errors = validate_placeholders("Invoice {0} submitted", "Rechnung {0} eingereicht")
        assert errors == []

    def test_detects_missing_placeholder(self) -> None:
        errors = validate_placeholders("Invoice {0} submitted", "Rechnung eingereicht")
        assert len(errors) == 1
        assert "Missing" in errors[0]
        assert "{0}" in errors[0]

    def test_detects_extra_placeholder(self) -> None:
        errors = validate_placeholders("Hello World", "Hallo {0} Welt")
        assert len(errors) == 1
        assert "Extra" in errors[0]
        assert "{0}" in errors[0]

    def test_no_errors_for_plain_text(self) -> None:
        assert validate_placeholders("Cancel", "Abbrechen") == []

    def test_html_tags_must_match(self) -> None:
        assert validate_placeholders("<b>Hello</b>", "<b>Hallo</b>") == []

    def test_missing_html_tag_is_error(self) -> None:
        errors = validate_placeholders("<b>Hello</b>", "Hallo")
        assert len(errors) >= 1
        assert "Missing" in errors[0]


class TestParseClaudeJson:
    def test_parses_clean_json(self) -> None:
        result = parse_claude_json('{"de": "Hallo"}')
        assert result == {"de": "Hallo"}

    def test_parses_json_in_markdown_fences(self) -> None:
        raw = '```json\n{"de": "Hallo"}\n```'
        result = parse_claude_json(raw)
        assert result == {"de": "Hallo"}

    def test_parses_json_in_plain_fences(self) -> None:
        raw = '```\n{"de": "Hallo"}\n```'
        result = parse_claude_json(raw)
        assert result == {"de": "Hallo"}

    def test_parses_json_with_surrounding_text(self) -> None:
        raw = 'Here is the translation:\n{"de": "Hallo"}\nDone.'
        result = parse_claude_json(raw)
        assert result == {"de": "Hallo"}

    def test_raises_on_invalid_input(self) -> None:
        with pytest.raises(ValueError):
            parse_claude_json("not json at all")

    def test_raises_on_empty_string(self) -> None:
        with pytest.raises(ValueError):
            parse_claude_json("")
