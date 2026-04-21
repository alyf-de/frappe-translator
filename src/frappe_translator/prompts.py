"""Prompt templates for Pass 1 (term extraction) and Pass 2 (translation)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frappe_translator.models import TranslationEntry


def build_translation_schema(target_languages: list[str]) -> str:
    """Build a JSON Schema for the translation response.

    Returns a schema for an object with:
    - translations: array of objects, each with msgid + msgctxt + language translations
    - terms: object mapping term -> {locale: translation} for glossary enrichment

    ``msgctxt`` is required and nullable so the model is forced to echo it back
    verbatim. Two entries can share the same ``msgid`` but differ on ``msgctxt``;
    without ``msgctxt`` in the response we cannot match translations back to
    their source entries.
    """
    translation_props: dict[str, Any] = {
        "msgid": {"type": "string"},
        "msgctxt": {"type": ["string", "null"]},
    }
    required = ["msgid", "msgctxt"]
    for lang in target_languages:
        translation_props[lang] = {"type": "string"}
        required.append(lang)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": translation_props,
                    "required": required,
                },
            },
            "terms": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
        },
        "required": ["translations"],
    }
    return json.dumps(schema)


def unique_source_files(source_refs: list[str]) -> list[str]:
    """Extract unique file paths from source refs, preserving order."""
    seen: set[str] = set()
    files: list[str] = []
    for ref in source_refs:
        file_part, _, _ = ref.rpartition(":")
        if file_part and file_part not in seen:
            seen.add(file_part)
            files.append(file_part)
    return files


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
        f"msgid: {json.dumps(entry.msgid, ensure_ascii=False)}",
        f"msgctxt: {json.dumps(entry.msgctxt or None, ensure_ascii=False)}",
    ]

    if entry.comments:
        lines.append(f"Comments: {chr(10).join(entry.comments)}")

    if snippets_text:
        lines.append("")
        lines.append("## Source Code Context")
        lines.append(snippets_text)
    elif entry.source_refs:
        source_files = unique_source_files(entry.source_refs)
        if source_files:
            lines.append("")
            lines.append(f"Source: {', '.join(source_files)}")

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
            "6. In your response, echo `msgid` AND `msgctxt` verbatim (use null for `msgctxt` when the"
            " input shows it as null) — these together identify the source entry.",
            "",
            "## Target Languages",
            ", ".join(target_languages),
            "",
            "In the terms field, include any business terms, UI actions, DocType names, and"
            " technical terms you translated that should stay consistent across the application.",
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
        "6. In your response, echo each entry's `msgid` AND `msgctxt` verbatim — these together identify"
        " the entry. Two entries can share the same msgid with different msgctxt and need separate"
        " translations. Use null for `msgctxt` when the input shows it as null.",
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

    for i, entry_info in enumerate(entries):
        msgid = entry_info["msgid"]
        msgctxt = entry_info.get("msgctxt") or None
        lines.append("")
        lines.append(f"### String {i + 1}")
        lines.append(f"msgid: {json.dumps(msgid, ensure_ascii=False)}")
        lines.append(f"msgctxt: {json.dumps(msgctxt, ensure_ascii=False)}")

        if entry_info.get("comments"):
            lines.append(f"Comments: {chr(10).join(entry_info['comments'])}")

        if entry_info.get("snippets_text"):
            lines.append(f"Source context:\n{entry_info['snippets_text']}")
        elif entry_info.get("source_files"):
            lines.append(f"Source: {', '.join(entry_info['source_files'])}")

    lines.extend(
        [
            "",
            "In the terms field, include any business terms, UI actions, DocType names, and"
            " technical terms you translated that should stay consistent across the application.",
        ]
    )

    return "\n".join(lines)
