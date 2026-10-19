"""Context assembly step: prepares full context for each translation entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from frappe_translator.models import AssembledContext, SourceSnippet, TermGlossary, TranslationEntry
from frappe_translator.prompts import build_translation_prompt
from frappe_translator.source_context import extract_snippets, format_snippets, select_diverse_snippets

logger = logging.getLogger(__name__)


def assemble_contexts(
    entries: list[TranslationEntry],
    app_path: Path,
    glossary: TermGlossary,
    target_languages: list[str],
    style_config: dict[str, Any],
    per_entry_languages: dict[tuple[str, str | None], set[str]] | None = None,
    existing_translations: dict[tuple[str, str | None], dict[str, str]] | None = None,
    existing_rule: str = "",
    split_languages: bool = False,
) -> list[AssembledContext]:
    """Assemble full translation context for each entry.

    Reads source files once per unique path (via shared file cache) and builds the
    prompt string for each entry via build_translation_prompt.

    If per_entry_languages is provided, each entry's prompt only requests translation
    into the specified languages (used by fill-missing to skip already-translated locales).
    If existing_translations is provided, each prompt shows them with ``existing_rule``
    (used by fix-glossary and review-existing).
    With split_languages, each entry gets one context per language, with the glossary of that language only.
    Batches need the same languages for all entries, so this keeps them full when each entry needs a
    different subset of languages.
    """
    file_cache: dict[str, list[str] | None] = {}

    assembled: list[AssembledContext] = []
    for entry in entries:
        try:
            snippets = extract_snippets(entry, app_path, file_cache=file_cache)
            snippets = select_diverse_snippets(snippets, max_snippets=3)
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
        existing = (existing_translations or {}).get((entry.msgid, entry.msgctxt), {})

        if not split_languages:
            assembled.append(
                _context(
                    entry,
                    snippets,
                    snippets_text,
                    glossary_terms,
                    entry_languages,
                    entry_style,
                    existing,
                    existing_rule,
                )
            )
            continue
        for lang in entry_languages:
            lang_terms = {term: {lang: langs[lang]} for term, langs in glossary_terms.items() if lang in langs}
            lang_style = {lang: entry_style[lang]} if lang in entry_style else {}
            lang_existing = {lang: existing[lang]} if lang in existing else {}
            assembled.append(
                _context(entry, snippets, snippets_text, lang_terms, [lang], lang_style, lang_existing, existing_rule)
            )

    return assembled


def _context(
    entry: TranslationEntry,
    snippets: list[SourceSnippet],
    snippets_text: str,
    glossary_terms: dict[str, dict[str, str]],
    languages: list[str],
    style_config: dict[str, Any],
    existing: dict[str, str],
    existing_rule: str,
) -> AssembledContext:
    prompt = build_translation_prompt(
        entry=entry,
        snippets_text=snippets_text,
        glossary_terms=glossary_terms,
        target_languages=languages,
        style_config=style_config,
        existing=existing,
        existing_rule=existing_rule,
    )
    return AssembledContext(
        entry=entry,
        snippets=snippets,
        glossary_terms=glossary_terms,
        prompt=prompt,
        target_languages=languages,
        existing=existing,
    )
