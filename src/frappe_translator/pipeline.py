"""Full pipeline orchestrator for frappe-translator."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from itertools import islice
from typing import TYPE_CHECKING

from frappe_translator._io import atomic_json_write
from frappe_translator.claude_runner import ClaudeRunner
from frappe_translator.context_assembler import assemble_contexts
from frappe_translator.discovery import discover_bench, get_app_languages, get_target_languages, resolve_app_order
from frappe_translator.glossary import (
    SOURCE_RANK,
    GlossaryFile,
    app_chain,
    load_glossary_file,
    merge_chain,
    migrate_bench_glossary,
    save_glossary_file,
    seed_glossary,
)
from frappe_translator.models import TermGlossary, TranslationEntry
from frappe_translator.po_handler import (
    POWriter,
    lookup_terms_batch,
    read_pot_entries,
)
from frappe_translator.progress import ProgressTracker
from frappe_translator.prompts import build_glossary_prompt, build_glossary_schema
from frappe_translator.term_extractor import extract_terms
from frappe_translator.translator import translate_entries
from frappe_translator.validation import parse_claude_json

if TYPE_CHECKING:
    from pathlib import Path

    from frappe_translator.config import TranslatorConfig
    from frappe_translator.models import AppInfo

logger = logging.getLogger(__name__)


async def run_pipeline(config: TranslatorConfig) -> PipelineSummary:
    """Run the full translation pipeline."""
    summary = PipelineSummary()

    # Discover bench and apps
    bench_path = config.bench_path
    all_apps = discover_bench(bench_path)
    if not all_apps:
        logger.error("No apps with POT files found in %s", bench_path)
        return summary

    # Keep full app list for PO term lookup and the glossary chain,
    # but only translate the filtered subset.
    all_apps_ordered = resolve_app_order(all_apps, config.app_priority)
    apps = [a for a in all_apps_ordered if a.name in config.apps] if config.apps else all_apps_ordered
    logger.info("Processing %d apps: %s", len(apps), [a.name for a in apps])

    # Determine target languages
    target_languages = get_target_languages(apps, config.languages or None, config.exclude_languages)
    if not target_languages:
        logger.error("No target languages found")
        return summary
    logger.info("Target languages (%d): %s", len(target_languages), target_languages)

    # Initialize runner and progress
    runner = ClaudeRunner(
        concurrency=config.concurrency,
        model=config.model,
        timeout=config.timeout,
    )
    progress = ProgressTracker(bench_path, max_retries=config.max_retries)
    if config.resume:
        progress.load()

    # Build style config dict for prompts
    style_config = {}
    for lang in target_languages:
        style = config.get_style(lang)
        style_config[lang] = {
            "formality": style.formality,
            "address": style.address,
            "notes": style.notes,
            "direction": style.direction,
        }

    # Collect PO paths from ALL apps for cross-app term lookup (not just filtered ones)
    all_po_paths: dict[str, dict[str, Path]] = {}
    for app in all_apps_ordered:
        all_po_paths[app.name] = app.po_paths

    # Cache for POT entries to avoid re-parsing in _process_app
    pot_cache: dict[str, list[TranslationEntry]] = {}

    glossary_dir = config.get_glossary_dir()
    migrate_bench_glossary(bench_path, glossary_dir, all_apps_ordered, config.app_priority)
    app_names = {app.name for app in all_apps_ordered}
    files = {name: load_glossary_file(glossary_dir, name) for name in app_names}

    # Pass 1: extract terms per app; new terms go into that app's glossary file as {}
    if not config.skip_glossary:
        await extract_new_terms(config, apps, files, runner, pot_cache)

    # Runtime glossary per app: merged chain files, then PO lookups fill gaps, then [terminology] wins.
    # PO lookups are not written back; they are recomputed each run.
    merged = {app.name: merge_chain(files, app_chain(app.name, app_names, config.app_priority)) for app in apps}
    all_terms = sorted({term for terms in merged.values() for term in terms})
    po_lookup = lookup_terms_batch(all_terms, all_po_paths)
    glossaries: dict[str, TermGlossary] = {}
    for app in apps:
        terms = merged[app.name]
        for term, langs in terms.items():
            for lang, text in po_lookup[term].items():
                langs.setdefault(lang, text)
        for lang, overlay in config.terminology.items():
            for term, text in overlay.items():
                terms.setdefault(term, {})[lang] = text
        glossaries[app.name] = TermGlossary(terms=terms)
        logger.info("%s: glossary with %d terms", app.name, len(terms))

    # Process each app in dependency order (Pass 2 only)
    for app in apps:
        logger.info("=== Processing app: %s ===", app.name)
        app_results = await _process_app(
            app=app,
            config=config,
            runner=runner,
            progress=progress,
            target_languages=target_languages,
            style_config=style_config,
            glossary=glossaries[app.name],
            pot_cache=pot_cache,
        )
        summary.app_results[app.name] = app_results

    # Print summary
    summary.log_summary()
    return summary


async def run_glossary(config: TranslatorConfig, tier: str, seed: bool = False) -> int:
    """Translate the terms of each app's glossary file and write them with ``source`` = ``tier``.

    Returns the number of failed batches.

    A (term, lang) pair is written only if it has no entry or its source ranks lower than ``tier``.
    """
    all_apps = discover_bench(config.bench_path)
    if not all_apps:
        logger.error("No apps with POT files found in %s", config.bench_path)
        return 0
    all_apps_ordered = resolve_app_order(all_apps, config.app_priority)
    apps = [a for a in all_apps_ordered if a.name in config.apps] if config.apps else all_apps_ordered
    target_languages = get_target_languages(apps, config.languages or None, config.exclude_languages)
    runner = ClaudeRunner(concurrency=config.concurrency, model=config.model, timeout=config.timeout)

    glossary_dir = config.get_glossary_dir()
    migrate_bench_glossary(config.bench_path, glossary_dir, all_apps_ordered, config.app_priority)
    files = {app.name: load_glossary_file(glossary_dir, app.name) for app in all_apps_ordered}

    if seed:
        first_app = all_apps_ordered[0].name
        seed_glossary(files, apps, first_app, config.app_priority)
        for name in {first_app, *(app.name for app in apps)}:
            if files[name]:
                save_glossary_file(glossary_dir, name, files[name])

    pot_cache: dict[str, list[TranslationEntry]] = {}
    await extract_new_terms(config, apps, files, runner, pot_cache)

    all_terms = sorted({term for app in apps for term in files[app.name]})
    po_hints = lookup_terms_batch(all_terms, {app.name: app.po_paths for app in all_apps_ordered})
    failed = 0
    for app in apps:
        failed += await _translate_glossary_terms(
            app,
            files[app.name],
            get_app_languages(app, target_languages),
            tier,
            pot_cache[app.name],
            po_hints,
            runner,
            config,
        )
    return failed


async def _translate_glossary_terms(
    app: AppInfo,
    terms: GlossaryFile,
    langs: list[str],
    tier: str,
    entries: list[TranslationEntry],
    po_hints: dict[str, dict[str, str]],
    runner: ClaudeRunner,
    config: TranslatorConfig,
) -> int:
    """Translate the open pairs of one app's glossary file; save the file after each batch.

    Returns the number of failed batches.
    """
    rank = SOURCE_RANK[tier]
    todo: dict[str, list[str]] = {}
    for term, translations in terms.items():
        missing = [
            lang for lang in langs if lang not in translations or SOURCE_RANK[translations[lang]["source"]] < rank
        ]
        if missing:
            todo[term] = missing
    logger.info("Glossary %s: %d terms to translate", app.name, len(todo))
    if not todo:
        return 0

    msgids = [entry.msgid for entry in entries]
    todo_items = list(todo.items())
    batches = [dict(todo_items[i : i + config.batch_size]) for i in range(0, len(todo_items), config.batch_size)]

    async def run_one(batch: dict[str, list[str]]) -> tuple[dict[str, list[str]], str | None]:
        items = []
        for term, missing in batch.items():
            pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
            examples = list(islice((m for m in msgids if pattern.search(m)), 3))
            hints = {lang: text for lang, text in po_hints[term].items() if lang in missing}
            items.append({"term": term, "languages": missing, "examples": examples, "hints": hints})
        try:
            raw = await runner.run(build_glossary_prompt(items), json_schema=build_glossary_schema(batch))
        except Exception as e:
            logger.error("Glossary %s: batch failed: %s", app.name, e)
            raw = None
        return batch, raw

    glossary_dir = config.get_glossary_dir()
    failed = 0
    for next_done in asyncio.as_completed([run_one(batch) for batch in batches]):
        batch, raw = await next_done
        if raw is None:
            failed += 1
            continue
        try:
            data = parse_claude_json(raw)
            if not isinstance(data, dict):
                raise ValueError(f"expected an object, got {type(data).__name__}")
            result = data["terms"]
            if not isinstance(result, dict):
                raise ValueError(f"expected an object for terms, got {type(result).__name__}")
        except (ValueError, KeyError) as e:
            logger.warning("Glossary %s: failed to parse response: %s", app.name, e)
            failed += 1
            continue
        written = 0
        for term, texts in result.items():
            if term not in batch or not isinstance(texts, dict):
                logger.warning("Glossary %s: skipping %r: %r", app.name, term, texts)
                continue
            for lang, text in texts.items():
                text = text.strip() if isinstance(text, str) else ""
                if lang not in batch[term] or not text:
                    logger.warning("Glossary %s: skipping %r / %s: %r", app.name, term, lang, text)
                    continue
                terms[term][lang] = {"text": text, "source": tier}
                written += 1
        save_glossary_file(glossary_dir, app.name, terms)
        logger.info("Glossary %s: wrote %d translations", app.name, written)
    return failed


async def extract_new_terms(
    config: TranslatorConfig,
    apps: list[AppInfo],
    files: dict[str, GlossaryFile],
    runner: ClaudeRunner,
    pot_cache: dict[str, list[TranslationEntry]],
) -> None:
    """Pass 1: add newly extracted terms to each app's glossary file as {}.

    ``files`` holds the glossary files of all apps; it is updated in place.
    ``pot_cache`` receives the POT entries of ``apps``.
    """
    bench_path = config.bench_path
    glossary_dir = config.get_glossary_dir()
    app_names = set(files)
    extracted_path = bench_path / "glossary_extracted.json"
    cache = json.loads(extracted_path.read_text()) if extracted_path.exists() else {}
    # The cache only says which msgids have their terms in *this* glossary_dir.
    if cache and cache["glossary_dir"] != str(glossary_dir.resolve()):
        logger.info("glossary_dir changed from %s, extracting terms again", cache["glossary_dir"])
        cache = {}
    extracted: dict[str, list[str]] = cache.get("apps", {})

    for app in apps:
        # Cache POT entries so _process_app doesn't re-parse them.
        entries = read_pot_entries(app.pot_path)
        pot_cache[app.name] = entries
        done = set(extracted.get(app.name, []))
        non_plural = [e for e in entries if not e.is_plural]
        uncovered = [e for e in non_plural if e.msgid not in done]
        logger.info("Pass 1 %s: %d/%d entries need term extraction", app.name, len(uncovered), len(non_plural))
        if not uncovered:
            continue

        extracted_terms = await extract_terms(uncovered, runner, config.batch_size)
        chain = app_chain(app.name, app_names, config.app_priority)
        new_terms = [t for t in extracted_terms.terms if not any(t in files[a] for a in chain)]
        if new_terms:
            for term in new_terms:
                files[app.name][term] = {}
            save_glossary_file(glossary_dir, app.name, files[app.name])
            logger.info("%s: added %d new terms to %s", app.name, len(new_terms), glossary_dir)

        extracted[app.name] = sorted(done | {e.msgid for e in uncovered})
        atomic_json_write(extracted_path, {"glossary_dir": str(glossary_dir.resolve()), "apps": extracted})


async def _process_app(
    app: AppInfo,
    config: TranslatorConfig,
    runner: ClaudeRunner,
    progress: ProgressTracker,
    target_languages: list[str],
    style_config: dict,
    glossary: TermGlossary,
    pot_cache: dict[str, list[TranslationEntry]] | None = None,
) -> AppResult:
    """Process a single app through the full pipeline."""
    result = AppResult(app_name=app.name)

    # An app can only write translations for locales it already has a PO file for.
    # Scope the target list down so we don't send Claude locales we'd just drop.
    app_languages = get_app_languages(app, target_languages)
    if not app_languages:
        logger.info("%s: no PO files for requested locales; skipping", app.name)
        return result
    logger.info("%s: target locales (%d): %s", app.name, len(app_languages), app_languages)

    app_style_config = {lang: style_config[lang] for lang in app_languages if lang in style_config}

    # Read POT entries (use cache from Pass 1 if available)
    entries = pot_cache.get(app.name) if pot_cache else None
    if entries is None:
        entries = read_pot_entries(app.pot_path)
    result.total_entries = len(entries)
    logger.info("%s: %d entries in POT", app.name, len(entries))

    # Filter entries that are plural (skip them)
    plural_entries = [e for e in entries if e.is_plural]
    if plural_entries:
        logger.info("%s: Skipping %d plural entries", app.name, len(plural_entries))
        result.skipped_plural = len(plural_entries)

    entries = [e for e in entries if not e.is_plural]

    # Create POWriter early so filtering can reuse its loaded PO data
    po_writer = POWriter(app.po_paths)

    # Filter entries based on run mode across the app's locales
    # In fill-missing mode: include entry if ANY locale is missing it
    # In review-existing mode: include entry if ANY locale has it
    # In full-correct mode: include all entries
    # Also track per-entry missing languages for fill-missing (to avoid overwriting existing)
    missing_langs_per_entry: dict[tuple[str, str | None], set[str]] | None = None
    if config.mode != "full-correct":
        all_po_translations: dict[str, dict[tuple[str, str | None], str]] = {}
        for lang in app_languages:
            all_po_translations[lang] = po_writer.get_translations(lang)

        filtered: list[TranslationEntry] = []
        if config.mode == "fill-missing":
            missing_langs_per_entry = {}
            for entry in entries:
                key = (entry.msgid, entry.msgctxt)
                missing = {lang for lang, po in all_po_translations.items() if not po.get(key, "")}
                if missing:
                    filtered.append(entry)
                    missing_langs_per_entry[key] = missing
        elif config.mode == "review-existing":
            for entry in entries:
                key = (entry.msgid, entry.msgctxt)
                if any(po.get(key, "") for po in all_po_translations.values()):
                    filtered.append(entry)
        entries = filtered

    # Filter by progress — skip fully-done entries (only in fill-missing mode)
    if config.resume and config.mode == "fill-missing":
        pending_entries = []
        for entry in entries:
            pending_langs = progress.get_pending_languages(app.name, entry.msgid, entry.msgctxt, app_languages)
            if pending_langs:
                pending_entries.append(entry)
            else:
                result.skipped_done += 1
        entries = pending_entries

    logger.info("%s: %d entries to process (after filtering)", app.name, len(entries))

    if not entries:
        return result

    # Context assembly (using the app's chain glossary)
    logger.info("%s: Assembling contexts...", app.name)
    contexts = assemble_contexts(
        entries=entries,
        app_path=app.path,
        glossary=glossary,
        target_languages=app_languages,
        style_config=app_style_config,
        per_entry_languages=missing_langs_per_entry,
    )

    # Pass 2: Translation with batched writes
    translation_results = await translate_entries(
        contexts=contexts,
        runner=runner,
        po_writer=po_writer,
        progress=progress,
        app_name=app.name,
        target_languages=app_languages,
        style_config=app_style_config,
        checkpoint_interval=config.checkpoint_interval,
        batch_size=config.batch_size,
    )

    # Final flush
    await po_writer.flush_all()
    progress.save()

    # Collect stats
    for tr in translation_results:
        result.translated += len(tr.translations)
        result.errors += len(tr.errors)
        if tr.skipped:
            result.skipped_failed += 1

    return result


class AppResult:
    """Results for a single app."""

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self.total_entries = 0
        self.translated = 0
        self.errors = 0
        self.skipped_plural = 0
        self.skipped_done = 0
        self.skipped_failed = 0


class PipelineSummary:
    """Summary of the full pipeline run."""

    def __init__(self) -> None:
        self.app_results: dict[str, AppResult] = {}

    def log_summary(self) -> None:
        """Log a summary report."""
        logger.info("=" * 60)
        logger.info("Translation Summary")
        logger.info("=" * 60)

        total_translated = 0
        total_errors = 0

        for app_name, result in self.app_results.items():
            logger.info(
                "  %s: %d entries, %d translations written, %d errors, %d skipped (done: %d, plural: %d, failed: %d)",
                app_name,
                result.total_entries,
                result.translated,
                result.errors,
                result.skipped_done + result.skipped_plural + result.skipped_failed,
                result.skipped_done,
                result.skipped_plural,
                result.skipped_failed,
            )
            total_translated += result.translated
            total_errors += result.errors

        logger.info("-" * 60)
        logger.info("  Total: %d translations, %d errors", total_translated, total_errors)

    @property
    def has_errors(self) -> bool:
        return any(r.errors > 0 for r in self.app_results.values())
