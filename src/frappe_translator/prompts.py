"""Prompt templates for Pass 1 (term extraction) and Pass 2 (translation)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frappe_translator.models import TranslationEntry


def build_term_extraction_prompt(entries: list[TranslationEntry], batch_index: int) -> str:
    """Build a prompt asking Claude to extract key domain-specific terms from a batch of strings."""
    numbered = "\n".join(f"{i + 1}. {entry.msgid}" for i, entry in enumerate(entries))

    return (
        "You are a translation term extractor for a business software application (Frappe/ERPNext).\n"
        "\n"
        "Below are source strings from the application. Extract key domain-specific terms that should be"
        " translated consistently. Focus on:\n"
        "- Business/accounting terms (Invoice, Purchase Order, Quotation)\n"
        "- Technical terms specific to the application (DocType, Workspace, Report Builder)\n"
        "- UI terms that need consistent translation (Submit, Amend, Cancel)\n"
        "\n"
        "Do NOT extract: common English words, placeholders ({0}, %s), HTML tags, HTML entities.\n"
        "\n"
        'Return a JSON object: {"terms": ["term1", "term2", ...]}\n'
        "\n"
        f"Source strings (batch {batch_index}):\n"
        "---\n"
        f"{numbered}"
    )


def build_translation_prompt(
    entry: TranslationEntry,
    snippets_text: str,
    glossary_terms: dict[str, dict[str, str]],
    target_languages: list[str],
    style_config: dict[str, Any],
) -> str:
    """Build a prompt for translating a single string into multiple languages."""
    lines: list[str] = [
        "You are a professional software translator for a business application (Frappe/ERPNext).",
        "",
        "Translate the following UI string into the target languages listed below.",
        "",
        "## Source String",
        f'msgid: "{entry.msgid}"',
    ]

    if entry.msgctxt:
        lines.append(f'context: "{entry.msgctxt}"')

    if entry.comments:
        lines.append(f"Comments: {chr(10).join(entry.comments)}")

    if snippets_text:
        lines.append("")
        lines.append("## Source Code Context")
        lines.append(snippets_text)

    if glossary_terms:
        lines.append("")
        lines.append("## Terminology Glossary (use these translations for consistency)")
        for term, translations in glossary_terms.items():
            lines.append(f'- "{term}": {json.dumps(translations, ensure_ascii=False)}')

    if style_config:
        lines.append("")
        lines.append("## Style Instructions")
        for language, style in style_config.items():
            if isinstance(style, dict):
                formality = style.get("formality", "")
                address = style.get("address", "")
                notes = style.get("notes", "")
                direction = style.get("direction", "")
                parts = []
                if formality:
                    parts.append(f"formality: {formality}")
                if address:
                    parts.append(f"address: {address}")
                if direction:
                    parts.append(f"direction: {direction}")
                if notes:
                    parts.append(f"notes: {notes}")
                style_notes = ", ".join(parts)
            else:
                style_notes = str(style)
            lines.append(f"- {language}: {style_notes}")

    lines.extend(
        [
            "",
            "## Rules",
            "1. Preserve ALL placeholders exactly: {0}, %s, {variable_name}, ${...}",
            "2. Preserve HTML tags and entities exactly",
            "3. Preserve Markdown formatting",
            "4. Do not translate technical identifiers (DocType names used as identifiers)",
            "5. Match the tone and register of the original (error messages stay direct, help text stays friendly)",
            "",
            "## Target Languages",
            ", ".join(target_languages),
            "",
            "Return ONLY a JSON object mapping language code to translated string:",
            "{" + ", ".join(f'"{lang}": "..."' for lang in target_languages) + "}",
        ]
    )

    return "\n".join(lines)


def build_batch_translation_prompt(
    entries: list[dict[str, Any]],
    shared_glossary: dict[str, dict[str, str]],
    target_languages: list[str],
    style_config: dict[str, Any],
) -> str:
    """Build a prompt for translating multiple strings at once.

    Args:
        entries: List of dicts with keys: index, msgid, msgctxt, comments, snippets_text,
                 glossary_terms (entry-specific subset), target_languages (if per-entry).
        shared_glossary: Union of all glossary terms across the batch.
        target_languages: Default target languages for the batch.
        style_config: Style instructions per language.
    """
    lines: list[str] = [
        "You are a professional software translator for a business application (Frappe/ERPNext).",
        "",
        "Translate each of the following UI strings into the target languages listed.",
        "",
        "## Rules",
        "1. Preserve ALL placeholders exactly: {0}, %s, {variable_name}, ${...}",
        "2. Preserve HTML tags and entities exactly",
        "3. Preserve Markdown formatting",
        "4. Do not translate technical identifiers (DocType names used as identifiers)",
        "5. Match the tone and register of the original (error messages stay direct, help text stays friendly)",
    ]

    if shared_glossary:
        lines.append("")
        lines.append("## Terminology Glossary (use these translations for consistency)")
        for term, translations in shared_glossary.items():
            lines.append(f'- "{term}": {json.dumps(translations, ensure_ascii=False)}')

    if style_config:
        lines.append("")
        lines.append("## Style Instructions")
        for language, style in style_config.items():
            if isinstance(style, dict):
                formality = style.get("formality", "")
                address = style.get("address", "")
                notes = style.get("notes", "")
                direction = style.get("direction", "")
                parts = []
                if formality:
                    parts.append(f"formality: {formality}")
                if address:
                    parts.append(f"address: {address}")
                if direction:
                    parts.append(f"direction: {direction}")
                if notes:
                    parts.append(f"notes: {notes}")
                style_notes = ", ".join(parts)
            else:
                style_notes = str(style)
            lines.append(f"- {language}: {style_notes}")

    lines.append("")
    lines.append("## Target Languages")
    lines.append(", ".join(target_languages))

    lines.append("")
    lines.append("## Strings to Translate")

    for entry_info in entries:
        idx = entry_info["index"]
        msgid = entry_info["msgid"]
        lines.append("")
        lines.append(f"### String {idx}")
        lines.append(f'msgid: "{msgid}"')

        if entry_info.get("msgctxt"):
            lines.append(f'context: "{entry_info["msgctxt"]}"')

        if entry_info.get("comments"):
            lines.append(f"Comments: {chr(10).join(entry_info['comments'])}")

        if entry_info.get("snippets_text"):
            lines.append(f"Source context:\n{entry_info['snippets_text']}")

    lines.extend(
        [
            "",
            "## Response Format",
            "Return ONLY a JSON object mapping string index to translations:",
            "{",
        ]
    )
    for entry_info in entries:
        idx = entry_info["index"]
        lines.append(f'  "{idx}": {{"' + '": "...", "'.join(target_languages) + '": "..."},')
    lines.append("}")

    return "\n".join(lines)
