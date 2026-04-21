"""Full pipeline orchestrator for frappe-translator."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from frappe_translator._io import atomic_json_write
from frappe_translator.claude_runner import ClaudeRunner
from frappe_translator.context_assembler import assemble_contexts
from frappe_translator.discovery import discover_bench, get_app_languages, get_target_languages, resolve_app_order
from frappe_translator.models import TermGlossary, TranslationEntry
from frappe_translator.po_handler import (
    POWriter,
    lookup_terms_batch,
    read_pot_entries,
)
from frappe_translator.progress import ProgressTracker
from frappe_translator.term_extractor import extract_terms
from frappe_translator.translator import translate_entries

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

    # Keep full app list for glossary building (cross-app term consistency),
    # but only translate the filtered subset.
    all_apps_ordered = resolve_app_order(all_apps, config.app_priority)
    apps = [a for a in all_apps_ordered if a.name in config.apps] if config.apps else all_apps_ordered
    logger.info("Processing %d apps: %s", len(apps), [a.name for a in apps])

    # Determine target languages
    target_languages = get_target_languages(apps, config.languages or None)
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

    # Pass 1: Build a single bench-wide glossary from ALL apps (not just filtered ones)
    glossary = TermGlossary()
    glossary_path = config.bench_path / "glossary.json"
    extracted_path = config.bench_path / "glossary_extracted.json"

    # Always load existing glossary (Pass 2 enriches it even with --skip-glossary)
    if glossary_path.exists():
        with open(glossary_path) as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            glossary.terms.update(existing)
            logger.info("Loaded existing glossary with %d terms", len(glossary.terms))

    if not config.skip_glossary:
        # Load set of msgids already processed for term extraction
        extracted_msgids: set[str] = set()
        if extracted_path.exists():
            with open(extracted_path) as f:
                data = json.load(f)
            if isinstance(data, list):
                extracted_msgids = {str(x) for x in data}

        # Extract terms only from the apps being translated (not all bench apps).
        # Term translations are still looked up across ALL apps' PO files.
        # Cache POT entries so _process_app doesn't re-parse them.
        all_entries = []
        for app in apps:
            entries = read_pot_entries(app.pot_path)
            pot_cache[app.name] = entries
            all_entries.extend(e for e in entries if not e.is_plural)

        uncovered = [e for e in all_entries if e.msgid not in extracted_msgids]
        logger.info(
            "Pass 1: %d/%d entries need term extraction (%d already extracted)",
            len(uncovered),
            len(all_entries),
            len(all_entries) - len(uncovered),
        )

        new_terms: list[str] = []
        if uncovered:
            new_glossary = await extract_terms(uncovered, runner, config.batch_size)
            # Merge new terms into existing glossary
            for term in new_glossary.terms:
                if term not in glossary.terms:
                    glossary.terms[term] = {}
                    new_terms.append(term)
            # Mark these entries as extracted
            extracted_msgids.update(e.msgid for e in uncovered)

        # Look up translations only for newly extracted terms
        if new_terms:
            logger.info("Looking up translations for %d new terms", len(new_terms))
            batch_results = lookup_terms_batch(new_terms, all_po_paths)
            for term, translations in batch_results.items():
                glossary.terms[term] = translations

        # Persist glossary and extracted set
        atomic_json_write(glossary_path, glossary.terms, indent=2)
        atomic_json_write(extracted_path, sorted(extracted_msgids))
        logger.info("Glossary saved to %s (%d terms)", glossary_path, len(glossary.terms))

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
            glossary=glossary,
            glossary_path=glossary_path,
            pot_cache=pot_cache,
        )
        summary.app_results[app.name] = app_results

    # Save glossary with any new terms discovered during Pass 2
    if glossary.terms:
        atomic_json_write(glossary_path, glossary.terms, indent=2)
        logger.info("Glossary updated: %d terms", len(glossary.terms))

    # Print summary
    summary.log_summary()
    return summary


async def _process_app(
    app: AppInfo,
    config: TranslatorConfig,
    runner: ClaudeRunner,
    progress: ProgressTracker,
    target_languages: list[str],
    style_config: dict,
    glossary: TermGlossary,
    glossary_path: Path | None = None,
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

    # Context assembly (using bench-wide glossary)
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
        glossary=glossary,
        glossary_path=glossary_path,
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
