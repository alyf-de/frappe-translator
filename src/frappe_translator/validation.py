"""Validation utilities for translation output."""

from __future__ import annotations

import json
import re

# Patterns for placeholders that MUST be preserved exactly in translations
PLACEHOLDER_PATTERNS = [
    re.compile(r"\{(\d+)\}"),  # {0}, {1}
    re.compile(r"\{(\w+)\}"),  # {variable_name}
    re.compile(r"%[sdifr%]"),  # %s, %d, %f, %i, %r, %%
    re.compile(r"\$\{[^}]+\}"),  # ${values.x}
    re.compile(r"<[^>]+>"),  # HTML tags
    re.compile(r"&\w+;"),  # HTML entities
]


def extract_placeholders(text: str) -> set[str]:
    """Extract all placeholders from a string that must be preserved in translation."""
    placeholders: set[str] = set()
    for pattern in PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(text):
            placeholders.add(match.group())
    return placeholders


def validate_placeholders(original: str, translated: str) -> list[str]:
    """Check that all placeholders in original appear in translated.

    Returns a list of error descriptions. Empty list means valid.
    """
    original_ph = extract_placeholders(original)
    translated_ph = extract_placeholders(translated)

    errors: list[str] = []

    missing = original_ph - translated_ph
    if missing:
        errors.append(f"Missing placeholders: {', '.join(sorted(missing))}")

    extra = translated_ph - original_ph
    if extra:
        errors.append(f"Extra placeholders: {', '.join(sorted(extra))}")

    return errors


def parse_claude_json(raw: str) -> dict | list:
    """Parse JSON from claude CLI output, handling common wrapping patterns.

    Handles:
    - Clean JSON (objects or arrays)
    - JSON wrapped in markdown code fences
    - JSON with leading/trailing text
    - Claude CLI --output-format json envelope
    """
    raw = raw.strip()

    # Try direct parse first
    try:
        data = json.loads(raw)
        # Unwrap claude CLI --output-format json envelope if present
        if isinstance(data, dict):
            # --json-schema responses put the parsed result in structured_output
            if "structured_output" in data and data["structured_output"] is not None:
                return data["structured_output"]
            # Regular responses put text in result
            if "result" in data and isinstance(data["result"], str):
                return parse_claude_json(data["result"])
        return data
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding JSON array or object in the text
    for open_char, close_char in [("[", "]"), ("{", "}")]:
        start = raw.find(open_char)
        if start >= 0:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == open_char:
                    depth += 1
                elif raw[i] == close_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start : i + 1])
                        except json.JSONDecodeError:
                            break

    raise ValueError(f"Could not parse JSON from claude output: {raw[:200]}")


def validate_translation_result(result: dict, expected_languages: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Validate a translation result dict against expected languages.

    Returns:
        (valid_translations, per_language_errors)
    """
    valid: dict[str, str] = {}
    errors: dict[str, str] = {}

    for lang in expected_languages:
        if lang not in result:
            errors[lang] = f"Missing language '{lang}' in response"
        elif not isinstance(result[lang], str):
            errors[lang] = f"Translation for '{lang}' is not a string: {type(result[lang])}"
        elif not result[lang].strip():
            errors[lang] = f"Empty translation for '{lang}'"
        else:
            valid[lang] = result[lang]

    return valid, errors
