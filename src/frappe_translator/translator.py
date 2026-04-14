"""Pass 2: Batched translation via claude CLI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from frappe_translator.models import AssembledContext, TranslationResult
from frappe_translator.prompts import build_batch_translation_prompt
from frappe_translator.source_context import format_snippets
from frappe_translator.validation import parse_claude_json, validate_placeholders, validate_translation_result

if TYPE_CHECKING:
    from frappe_translator.claude_runner import ClaudeRunner
    from frappe_translator.po_handler import POWriter
    from frappe_translator.progress import ProgressTracker

logger = logging.getLogger(__name__)


async def translate_entries(
    contexts: list[AssembledContext],
    runner: ClaudeRunner,
    po_writer: POWriter,
    progress: ProgressTracker,
    app_name: str,
    target_languages: list[str],
    style_config: dict | None = None,
    checkpoint_interval: int = 50,
    batch_size: int = 50,
) -> list[TranslationResult]:
    """Translate entries in batches using pre-assembled contexts.

    Groups contexts into batches, sends one Claude call per batch, validates
    results, and retries failed entries individually.
    """
    results: list[TranslationResult] = []

    if not contexts:
        return results

    # Split into batches
    batches: list[list[AssembledContext]] = []
    for i in range(0, len(contexts), batch_size):
        batches.append(contexts[i : i + batch_size])

    logger.info(
        "Pass 2: Translating %d entries into %d languages in %d batches",
        len(contexts),
        len(target_languages),
        len(batches),
    )

    # Build batch prompts
    prompts: list[str] = []
    for batch in batches:
        prompt = _build_batch_prompt(batch, target_languages, style_config or {})
        prompts.append(prompt)

    # Run all batch prompts concurrently
    raw_results = await runner.run_batch(prompts)

    # Process batch results
    entries_since_checkpoint = 0
    retry_contexts: list[AssembledContext] = []

    for batch_idx, (batch, raw) in enumerate(zip(batches, raw_results, strict=True)):
        batch_results = _process_batch_result(batch, raw, target_languages)

        for ctx, result in zip(batch, batch_results, strict=True):
            if result.skipped:
                # Entire batch failed or this entry had no result — queue for individual retry
                retry_contexts.append(ctx)
                continue

            results.append(result)

            if result.translations:
                po_writer.buffer_translation(result)
                progress.mark_languages_done(app_name, result.msgid, result.msgctxt, list(result.translations.keys()))

            for lang, error in result.errors.items():
                progress.mark_language_error(app_name, result.msgid, result.msgctxt, lang, error)

            entries_since_checkpoint += 1

        # Checkpoint after each batch
        if entries_since_checkpoint >= checkpoint_interval:
            logger.info(
                "Checkpoint at batch %d/%d (%d entries), flushing...",
                batch_idx + 1,
                len(batches),
                entries_since_checkpoint,
            )
            await po_writer.flush_all()
            progress.save()
            entries_since_checkpoint = 0

    # Retry failed entries individually using their pre-built prompts
    if retry_contexts:
        logger.info("Retrying %d entries individually", len(retry_contexts))
        retry_prompts = [ctx.prompt for ctx in retry_contexts]
        retry_raw = await runner.run_batch(retry_prompts)

        for ctx, raw in zip(retry_contexts, retry_raw, strict=True):
            entry_langs = ctx.target_languages or target_languages
            result = _process_single_result(ctx, raw, entry_langs)
            results.append(result)

            if result.translations:
                po_writer.buffer_translation(result)
                progress.mark_languages_done(app_name, result.msgid, result.msgctxt, list(result.translations.keys()))

            for lang, error in result.errors.items():
                progress.mark_language_error(app_name, result.msgid, result.msgctxt, lang, error)

    # Summary
    translated = sum(1 for r in results if r.translations)
    errored = sum(1 for r in results if r.errors)
    skipped = sum(1 for r in results if r.skipped)
    logger.info(
        "Pass 2 complete: %d translated, %d with errors, %d skipped",
        translated,
        errored,
        skipped,
    )

    return results


def _build_batch_prompt(
    batch: list[AssembledContext],
    target_languages: list[str],
    style_config: dict,
) -> str:
    """Build a batch translation prompt from assembled contexts."""
    # Collect union of glossary terms across the batch
    shared_glossary: dict[str, dict[str, str]] = {}
    for ctx in batch:
        shared_glossary.update(ctx.glossary_terms)

    # Build per-entry info
    entries_info: list[dict] = []
    for i, ctx in enumerate(batch):
        snippets_text = format_snippets(ctx.snippets)
        entries_info.append(
            {
                "index": i + 1,
                "msgid": ctx.entry.msgid,
                "msgctxt": ctx.entry.msgctxt,
                "comments": ctx.entry.comments,
                "snippets_text": snippets_text,
            }
        )

    return build_batch_translation_prompt(
        entries=entries_info,
        shared_glossary=shared_glossary,
        target_languages=target_languages,
        style_config=style_config,
    )


def _process_batch_result(
    batch: list[AssembledContext],
    raw: str | None,
    target_languages: list[str],
) -> list[TranslationResult]:
    """Process a batch claude response into per-entry TranslationResults."""
    results: list[TranslationResult] = []

    if raw is None:
        # Entire batch failed — mark all for retry
        for ctx in batch:
            result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True)
            results.append(result)
        return results

    # Parse JSON response
    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse batch JSON response: %s", e)
        for ctx in batch:
            result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True)
            results.append(result)
        return results

    # Extract per-entry results by index
    for i, ctx in enumerate(batch):
        idx_key = str(i + 1)
        entry_data = data.get(idx_key)
        entry_langs = ctx.target_languages or target_languages
        result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt)

        if entry_data is None or not isinstance(entry_data, dict):
            # This entry missing from batch response — mark for retry
            result.skipped = True
            results.append(result)
            continue

        # Validate against expected languages
        valid_translations, lang_errors = validate_translation_result(entry_data, entry_langs)
        result.errors.update(lang_errors)

        # Validate placeholders per language
        for lang, translated in valid_translations.items():
            placeholder_errors = validate_placeholders(ctx.entry.msgid, translated)
            if placeholder_errors:
                result.errors[lang] = "; ".join(placeholder_errors)
                logger.warning(
                    "Placeholder mismatch for '%s' [%s]: %s",
                    ctx.entry.msgid[:50],
                    lang,
                    "; ".join(placeholder_errors),
                )
            else:
                result.translations[lang] = translated

        results.append(result)

    return results


def _process_single_result(
    ctx: AssembledContext,
    raw: str | None,
    target_languages: list[str],
) -> TranslationResult:
    """Process a single claude response into a TranslationResult (for retries)."""
    result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt)

    if raw is None:
        result.skipped = True
        result.errors = {lang: "claude CLI call failed" for lang in target_languages}
        return result

    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse JSON for '%s': %s", ctx.entry.msgid[:50], e)
        result.errors = {lang: f"JSON parse error: {e}" for lang in target_languages}
        return result

    valid_translations, lang_errors = validate_translation_result(data, target_languages)
    result.errors.update(lang_errors)

    for lang, translated in valid_translations.items():
        placeholder_errors = validate_placeholders(ctx.entry.msgid, translated)
        if placeholder_errors:
            result.errors[lang] = "; ".join(placeholder_errors)
            logger.warning(
                "Placeholder mismatch for '%s' [%s]: %s",
                ctx.entry.msgid[:50],
                lang,
                "; ".join(placeholder_errors),
            )
        else:
            result.translations[lang] = translated

    return result
