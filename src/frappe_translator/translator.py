"""Batched translation via claude CLI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from frappe_translator.claude_runner import as_completed_logged
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
    existing_rule: str = "",
) -> list[TranslationResult]:
    """Translate entries in batches using pre-assembled contexts.

    Contexts are grouped by their pending-language set so that each batch
    prompt and JSON schema only request the locales that every entry in the
    batch still needs. This avoids regenerating translations for locales that
    are already complete (see issue #1).
    """
    results: list[TranslationResult] = []

    if not contexts:
        return results

    # Group by identical pending-language sets, then split each group into
    # fixed-size batches. Each batch carries its own language list.
    groups = _group_contexts_by_languages(contexts, target_languages)
    batches: list[tuple[list[AssembledContext], list[str]]] = []
    for langs_key, group_contexts in groups.items():
        langs_list = list(langs_key)
        for i in range(0, len(group_contexts), batch_size):
            batches.append((group_contexts[i : i + batch_size], langs_list))

    logger.info(
        "Translating %d entries in %d batches across %d unique language sets",
        len(contexts),
        len(batches),
        len(groups),
    )

    async def _run_one(batch_idx: int) -> tuple[int, str | None]:
        batch, batch_langs = batches[batch_idx]
        prompt = _build_batch_prompt(batch, batch_langs, style_config or {}, existing_rule)
        schema = build_translation_schema(batch_langs)
        try:
            raw = await runner.run(prompt, json_schema=schema)
        except Exception as e:
            logger.error("Batch %d failed: %s", batch_idx + 1, e)
            raw = None
        return batch_idx, raw

    # Launch all tasks — the runner's semaphore limits actual concurrency
    runs = [_run_one(i) for i in range(len(batches))]

    entries_since_checkpoint = 0
    retry_contexts: list[AssembledContext] = []
    total_translated = 0
    total_errors = 0
    batches_done = 0

    async for batch_idx, raw in as_completed_logged(runs, f"Translate {app_name}", runner):
        batch, batch_langs = batches[batch_idx]
        batches_done += 1

        batch_results = _process_batch_result(batch, raw, batch_langs)

        batch_ok = 0
        batch_err = 0
        batch_skip = 0
        for ctx, result in zip(batch, batch_results, strict=True):
            if result.skipped:
                retry_contexts.append(ctx)
                batch_skip += 1
                continue

            _drop_unchanged(result, ctx)
            results.append(result)
            batch_ok += len(result.translations)
            batch_err += len(result.errors)

            if result.translations:
                po_writer.buffer_translation(result)
                progress.mark_languages_done(app_name, result.msgid, result.msgctxt, list(result.translations.keys()))

            for lang, error in result.errors.items():
                progress.mark_language_error(app_name, result.msgid, result.msgctxt, lang, error)

            entries_since_checkpoint += 1

        total_translated += batch_ok
        total_errors += batch_err
        logger.debug(
            "Batch %d/%d: %d translated, %d errors, %d to retry",
            batch_idx + 1,
            len(batches),
            batch_ok,
            batch_err,
            batch_skip,
        )

        # Checkpoint after processing completed batches
        if entries_since_checkpoint >= checkpoint_interval:
            logger.debug(
                "Checkpoint (%d/%d batches, %d entries), flushing...",
                batches_done,
                len(batches),
                entries_since_checkpoint,
            )
            await po_writer.flush_all()
            progress.save()
            entries_since_checkpoint = 0

    # Retry failed entries individually using their pre-built prompts
    if retry_contexts:
        logger.info("Retrying %d entries individually", len(retry_contexts))

        async def _retry_one(ctx: AssembledContext) -> tuple[AssembledContext, str | None]:
            entry_langs = ctx.target_languages or target_languages
            entry_schema = build_translation_schema(entry_langs)
            try:
                raw = await runner.run(ctx.prompt, json_schema=entry_schema)
            except Exception as e:
                logger.error("Retry failed for '%s': %s", ctx.entry.msgid[:50], e)
                raw = None
            return ctx, raw

        retries = [_retry_one(ctx) for ctx in retry_contexts]
        async for ctx, raw in as_completed_logged(retries, f"Retries {app_name}", runner):
            entry_langs = ctx.target_languages or target_languages
            result = _process_single_result(ctx, raw, entry_langs)
            _drop_unchanged(result, ctx)
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
        "Translation complete: %d translated, %d with errors, %d skipped",
        translated,
        errored,
        skipped,
    )

    return results


def _drop_unchanged(result: TranslationResult, ctx: AssembledContext) -> None:
    """Keep only translations that differ from the existing ones, so a correction writes real changes only."""
    result.translations = {lang: text for lang, text in result.translations.items() if text != ctx.existing.get(lang)}


def _group_contexts_by_languages(
    contexts: list[AssembledContext],
    default_target_languages: list[str],
) -> dict[tuple[str, ...], list[AssembledContext]]:
    """Group assembled contexts by their pending-language set.

    Contexts with identical target-language sets share a batch so every entry
    in the resulting batch needs the same locales. Insertion order is
    preserved both for groups (first-seen language set) and within each group.
    """
    groups: dict[tuple[str, ...], list[AssembledContext]] = {}
    for ctx in contexts:
        langs = ctx.target_languages or default_target_languages
        key = tuple(sorted(langs))
        groups.setdefault(key, []).append(ctx)
    return groups


def _build_batch_prompt(
    batch: list[AssembledContext],
    target_languages: list[str],
    style_config: dict,
    existing_rule: str = "",
) -> str:
    """Build a batch translation prompt from assembled contexts."""
    shared_glossary: dict[str, dict[str, str]] = {}
    for ctx in batch:
        for term, translations in ctx.glossary_terms.items():
            shared_glossary.setdefault(term, {}).update(translations)

    entries_info: list[dict] = []
    for ctx in batch:
        snippets_text = format_snippets(ctx.snippets)
        source_files = unique_source_files(ctx.entry.source_refs) if not snippets_text else []
        entries_info.append(
            {
                "msgid": ctx.entry.msgid,
                "msgctxt": ctx.entry.msgctxt,
                "comments": ctx.entry.comments,
                "existing": ctx.existing,
                "snippets_text": snippets_text,
                "source_files": source_files,
            }
        )

    return build_batch_translation_prompt(
        entries=entries_info,
        shared_glossary=shared_glossary,
        target_languages=target_languages,
        style_config=style_config,
        existing_rule=existing_rule,
    )


def _process_batch_result(
    batch: list[AssembledContext],
    raw: str | None,
    target_languages: list[str],
) -> list[TranslationResult]:
    """Process a batch claude response into per-entry TranslationResults."""
    _skip_all = [TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt, skipped=True) for ctx in batch]

    if raw is None:
        return _skip_all

    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse batch JSON response: %s", e)
        return _skip_all

    # Expect {"translations": [...]}
    if isinstance(data, dict):
        translations_list = data.get("translations", [])
    elif isinstance(data, list):
        # Fallback: plain array without the wrapper object
        translations_list = data
    else:
        logger.warning("Unexpected batch response type: %s", type(data))
        return _skip_all

    if not isinstance(translations_list, list):
        logger.warning("translations field is not an array, marking all for retry")
        return _skip_all

    # Build lookup by (msgid, msgctxt) from the response array
    response_by_key: dict[tuple[str, str | None], dict] = {}
    for item in translations_list:
        if isinstance(item, dict) and "msgid" in item:
            key = (item["msgid"], item.get("msgctxt") or None)
            response_by_key[key] = item

    results: list[TranslationResult] = []
    missed_keys: list[tuple[str, str | None]] = []
    for ctx in batch:
        entry_langs = ctx.target_languages or target_languages
        entry_data = response_by_key.get((ctx.entry.msgid, ctx.entry.msgctxt))
        result = TranslationResult(msgid=ctx.entry.msgid, msgctxt=ctx.entry.msgctxt)

        if entry_data is None:
            result.skipped = True
            missed_keys.append((ctx.entry.msgid, ctx.entry.msgctxt))
            results.append(result)
            continue

        _validate_entry_translations(result, entry_data, ctx.entry.msgid, entry_langs)
        results.append(result)

    if missed_keys:
        _log_batch_mismatch(batch, response_by_key, missed_keys, raw)

    return results


def _log_batch_mismatch(
    batch: list[AssembledContext],
    response_by_key: dict[tuple[str, str | None], dict],
    missed_keys: list[tuple[str, str | None]],
    raw: str,
) -> None:
    """Diagnostic logging for batch entries whose msgid/msgctxt didn't appear in the response.

    Logs at DEBUG always; escalates to WARNING when the entire batch missed (a strong signal
    that the response is structurally wrong rather than a per-entry omission).
    """
    expected_keys = {(ctx.entry.msgid, ctx.entry.msgctxt) for ctx in batch}
    response_keys = set(response_by_key.keys())
    extra_in_response = response_keys - expected_keys

    all_missed = len(missed_keys) == len(batch)
    log = logger.warning if all_missed else logger.debug

    log(
        "Batch response mismatch: %d/%d entries not found in response. "
        "Expected %d keys, got %d keys, %d unexpected msgids in response.",
        len(missed_keys),
        len(batch),
        len(expected_keys),
        len(response_keys),
        len(extra_in_response),
    )

    for msgid, msgctxt in missed_keys[:5]:
        log("  missed: msgid=%r msgctxt=%r", msgid[:120], msgctxt)
    for msgid, msgctxt in list(extra_in_response)[:5]:
        log("  extra in response: msgid=%r msgctxt=%r", msgid[:120], msgctxt)

    if all_missed:
        log("  raw response (first 2000 chars): %s", raw[:2000])


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

    try:
        data = parse_claude_json(raw)
    except ValueError as e:
        logger.warning("Failed to parse JSON for '%s': %s", ctx.entry.msgid[:50], e)
        result.errors = {lang: f"JSON parse error: {e}" for lang in target_languages}
        return result

    # Handle {"translations": [...]} envelope
    if isinstance(data, dict) and "translations" in data:
        translations_list = data["translations"]
        if isinstance(translations_list, list) and len(translations_list) > 0:
            entry_data = translations_list[0]
        else:
            result.errors = {lang: "Empty translations array" for lang in target_languages}
            return result
    elif isinstance(data, list):
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

    if isinstance(entry_data, dict):
        _validate_entry_translations(result, entry_data, ctx.entry.msgid, target_languages)
    else:
        result.errors = {lang: "Invalid entry data" for lang in target_languages}

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
