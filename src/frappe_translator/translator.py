"""Pass 2: Batched translation via claude CLI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from frappe_translator.models import AssembledContext, TranslationResult
from frappe_translator.prompts import build_batch_translation_prompt, build_translation_schema, unique_source_files
from frappe_translator.source_context import format_snippets
from frappe_translator.validation import parse_claude_json, validate_placeholders

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

    # Build batch prompts and schemas
    schema = build_translation_schema(target_languages)
    prompts: list[str] = []
    for batch in batches:
        prompt = _build_batch_prompt(batch, target_languages, style_config or {})
        prompts.append(prompt)

    # Run all batch prompts concurrently
    raw_results = await runner.run_batch(prompts, json_schemas=[schema] * len(prompts))

    # Process batch results
    entries_since_checkpoint = 0
    retry_contexts: list[AssembledContext] = []

    for batch_idx, (batch, raw) in enumerate(zip(batches, raw_results, strict=True)):
        batch_results = _process_batch_result(batch, raw, target_languages)

        for ctx, result in zip(batch, batch_results, strict=True):
            if result.skipped:
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
        retry_raw = await runner.run_batch(retry_prompts, json_schemas=[schema] * len(retry_prompts))

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
    shared_glossary: dict[str, dict[str, str]] = {}
    for ctx in batch:
        shared_glossary.update(ctx.glossary_terms)

    entries_info: list[dict] = []
    for ctx in batch:
        snippets_text = format_snippets(ctx.snippets)
        source_files = unique_source_files(ctx.entry.source_refs) if not snippets_text else []
        entries_info.append(
            {
                "msgid": ctx.entry.msgid,
                "msgctxt": ctx.entry.msgctxt,
                "comments": ctx.entry.comments,
                "snippets_text": snippets_text,
                "source_files": source_files,
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
    """Process a batch claude response (array of objects) into per-entry TranslationResults."""
    if raw is None:
        return [TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True) for ctx in batch]

    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse batch JSON response: %s", e)
        return [TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True) for ctx in batch]

    if not isinstance(data, list):
        logger.warning("Batch response is not an array, marking all for retry")
        return [TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True) for ctx in batch]

    # Build lookup by msgid from the response array
    response_by_msgid: dict[str, dict] = {}
    for item in data:
        if isinstance(item, dict) and "msgid" in item:
            response_by_msgid[item["msgid"]] = item

    results: list[TranslationResult] = []
    for ctx in batch:
        entry_langs = ctx.target_languages or target_languages
        entry_data = response_by_msgid.get(ctx.entry.msgid)
        result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt)

        if entry_data is None:
            result.skipped = True
            results.append(result)
            continue

        _validate_entry_translations(result, entry_data, ctx.entry.msgid, entry_langs)
        results.append(result)

    return results


def _process_single_result(
    ctx: AssembledContext,
    raw: str | None,
    target_languages: list[str],
) -> TranslationResult:
    """Process a single claude response (array with one object) into a TranslationResult."""
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

    # Handle array response (expected from --json-schema)
    if isinstance(data, list):
        if len(data) > 0 and isinstance(data[0], dict):
            entry_data = data[0]
        else:
            result.errors = {lang: "Empty or invalid array response" for lang in target_languages}
            return result
    elif isinstance(data, dict):
        entry_data = data
    else:
        result.errors = {lang: f"Unexpected response type: {type(data)}" for lang in target_languages}
        return result

    _validate_entry_translations(result, entry_data, ctx.entry.msgid, target_languages)
    return result


def _validate_entry_translations(
    result: TranslationResult,
    entry_data: dict,
    msgid: str,
    target_languages: list[str],
) -> None:
    """Validate translations from a response dict and populate the result."""
    for lang in target_languages:
        translated = entry_data.get(lang)
        if translated is None:
            result.errors[lang] = f"Missing language '{lang}' in response"
        elif not isinstance(translated, str):
            result.errors[lang] = f"Translation for '{lang}' is not a string: {type(translated)}"
        elif not translated.strip():
            result.errors[lang] = f"Empty translation for '{lang}'"
        else:
            placeholder_errors = validate_placeholders(msgid, translated)
            if placeholder_errors:
                result.errors[lang] = "; ".join(placeholder_errors)
                logger.warning(
                    "Placeholder mismatch for '%s' [%s]: %s",
                    msgid[:50],
                    lang,
                    "; ".join(placeholder_errors),
                )
            else:
                result.translations[lang] = translated
