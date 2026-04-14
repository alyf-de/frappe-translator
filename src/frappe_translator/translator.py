"""Pass 2: Per-string translation via claude CLI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from frappe_translator.models import AssembledContext, TranslationResult
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
    checkpoint_interval: int = 50,
) -> list[TranslationResult]:
    """Translate entries using pre-assembled contexts.

    Runs claude concurrently, validates results per-language, buffers translations,
    and checkpoints (flush + save) every checkpoint_interval entries.
    """
    results: list[TranslationResult] = []

    if not contexts:
        return results

    logger.info("Pass 2: Translating %d entries into %d languages", len(contexts), len(target_languages))

    # Run all prompts concurrently
    prompts = [ctx.prompt for ctx in contexts]
    raw_results = await runner.run_batch(prompts)

    # Process results
    entries_since_checkpoint = 0
    for i, (ctx, raw) in enumerate(zip(contexts, raw_results, strict=True)):
        result = _process_single_result(ctx, raw, target_languages)
        results.append(result)

        # Buffer successful translations
        if result.translations:
            po_writer.buffer_translation(result)
            progress.mark_languages_done(app_name, result.msgid, result.msgctxt, list(result.translations.keys()))

        # Record per-language errors
        for lang, error in result.errors.items():
            progress.mark_language_error(app_name, result.msgid, result.msgctxt, lang, error)

        entries_since_checkpoint += 1

        # Checkpoint: flush PO writes + save progress
        if entries_since_checkpoint >= checkpoint_interval:
            logger.info("Checkpoint at entry %d/%d, flushing...", i + 1, len(contexts))
            await po_writer.flush_all()
            progress.save()
            entries_since_checkpoint = 0

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


def _process_single_result(
    ctx: AssembledContext,
    raw: str | None,
    target_languages: list[str],
) -> TranslationResult:
    """Process a single claude response into a TranslationResult."""
    result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt)

    if raw is None:
        result.skipped = True
        result.errors = {lang: "claude CLI call failed" for lang in target_languages}
        return result

    # Parse JSON response
    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse JSON for '%s': %s", ctx.entry.msgid[:50], e)
        result.errors = {lang: f"JSON parse error: {e}" for lang in target_languages}
        return result

    # Validate against expected languages
    valid_translations, lang_errors = validate_translation_result(data, target_languages)
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

    return result
