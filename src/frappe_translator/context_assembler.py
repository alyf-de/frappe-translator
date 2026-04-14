"""Context assembly step: prepares full context for each translation entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from frappe_translator.models import AssembledContext, TermGlossary, TranslationEntry
from frappe_translator.prompts import build_translation_prompt
from frappe_translator.source_context import extract_snippets, format_snippets

logger = logging.getLogger(__name__)


def assemble_contexts(
    entries: list[TranslationEntry],
    app_path: Path,
    glossary: TermGlossary,
    target_languages: list[str],
    style_config: dict[str, Any],
    per_entry_languages: dict[tuple[str, str | None], set[str]] | None = None,
) -> list[AssembledContext]:
    """Assemble full translation context for each entry.

    Reads source files once per unique path (via shared file cache) and builds the
    prompt string for each entry via build_translation_prompt.

    If per_entry_languages is provided, each entry's prompt only requests translation
    into the specified languages (used by fill-missing to skip already-translated locales).
    """
    file_cache: dict[str, list[str] | None] = {}

    assembled: list[AssembledContext] = []
    for entry in entries:
        try:
            snippets = extract_snippets(entry, app_path, file_cache=file_cache)
        except Exception:
            logger.warning("Failed to extract snippets for %r, using empty list", entry.msgid, exc_info=True)
            snippets = []

        glossary_terms = glossary.get_relevant_terms(entry.msgid)
        snippets_text = format_snippets(snippets)

        # Use per-entry languages if available, otherwise all target languages
        if per_entry_languages is not None:
            key = (entry.msgid, entry.msgctxt)
            entry_languages = sorted(per_entry_languages.get(key, set()))
            entry_style = {lang: style_config[lang] for lang in entry_languages if lang in style_config}
        else:
            entry_languages = target_languages
            entry_style = style_config

        prompt = build_translation_prompt(
            entry=entry,
            snippets_text=snippets_text,
            glossary_terms=glossary_terms,
            target_languages=entry_languages,
            style_config=entry_style,
        )

        assembled.append(
            AssembledContext(
                entry=entry,
                snippets=snippets,
                glossary_terms=glossary_terms,
                prompt=prompt,
                target_languages=entry_languages,
            )
        )

    return assembled
