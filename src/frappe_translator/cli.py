"""CLI entry point for frappe-translator."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from frappe_translator.config import TranslatorConfig


def _setup_logging(verbose: bool) -> None:
    """Configure logging for the CLI."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.version_option(package_name="frappe-translator")
def main() -> None:
    """Frappe Translator -- AI-powered translation for Frappe apps."""


@main.command()
@click.argument("bench_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=None, help="Config TOML file")
@click.option("--app", "-a", multiple=True, help="App to translate (repeatable, default: all)")
@click.option("--language", "-l", multiple=True, help="Target language (repeatable, default: all found)")
@click.option("--exclude-language", "-x", multiple=True, help="Language to skip (repeatable, e.g. eo)")
@click.option(
    "--mode",
    type=click.Choice(["fill-missing", "review-existing", "full-correct"]),
    default="fill-missing",
    help="Translation mode",
)
@click.option("--concurrency", type=click.IntRange(1, 50), default=5, help="Max parallel claude processes")
@click.option("--batch-size", type=int, default=50, help="Strings per batch")
@click.option("--timeout", type=int, default=120, help="Timeout per claude call in seconds")
@click.option("--model", type=str, default=None, help="Claude model to use (e.g., sonnet, opus)")
@click.option("--extract-model", type=str, default=None, help="Claude model for Pass 1 (default: --model)")
@click.option("--resume/--no-resume", default=True, help="Resume from previous progress")
@click.option("--dry-run", is_flag=True, help="Show what would be translated without running")
@click.option(
    "--glossary-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Folder with per-app glossary files (default: <bench>/glossaries)",
)
@click.option("--skip-glossary", is_flag=True, help="Skip Pass 1 term extraction (single-pass mode)")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def translate(
    ctx: click.Context,
    bench_path: Path,
    config: Path | None,
    app: tuple[str, ...],
    language: tuple[str, ...],
    exclude_language: tuple[str, ...],
    mode: str,
    concurrency: int,
    batch_size: int,
    timeout: int,
    model: str | None,
    extract_model: str | None,
    resume: bool,
    dry_run: bool,
    glossary_dir: Path | None,
    skip_glossary: bool,
    verbose: bool,
) -> None:
    """Translate Frappe app strings using Claude."""
    _setup_logging(verbose)

    from frappe_translator.config import load_config

    cfg = load_config(config)
    cfg.bench_path = bench_path
    if app:
        cfg.apps = list(app)
    if language:
        cfg.languages = list(language)
    if exclude_language:
        cfg.exclude_languages = list(exclude_language)

    # Only override config-file values when the user explicitly passed the CLI flag
    source = ctx.get_parameter_source
    if source("mode") != click.core.ParameterSource.DEFAULT:
        cfg.mode = mode
    if source("concurrency") != click.core.ParameterSource.DEFAULT:
        cfg.concurrency = concurrency
    if source("batch_size") != click.core.ParameterSource.DEFAULT:
        cfg.batch_size = batch_size
    if source("timeout") != click.core.ParameterSource.DEFAULT:
        cfg.timeout = timeout
    if model is not None:
        cfg.model = model
    if extract_model is not None:
        cfg.extract_model = extract_model
    if glossary_dir is not None:
        cfg.glossary_dir = glossary_dir
    if source("resume") != click.core.ParameterSource.DEFAULT:
        cfg.resume = resume
    if source("skip_glossary") != click.core.ParameterSource.DEFAULT:
        cfg.skip_glossary = skip_glossary

    # Check claude CLI is available
    if not dry_run:
        _check_claude_cli()

    if dry_run:
        _dry_run(cfg)
        return

    from frappe_translator.pipeline import run_pipeline

    try:
        summary = asyncio.run(run_pipeline(cfg))
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Progress was saved at the last checkpoint.", err=True)
        sys.exit(130)
    if summary.has_errors:
        sys.exit(1)


@main.command()
@click.argument("bench_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--model", required=True, help="Claude model: sonnet or opus. Sets the source of written entries.")
@click.option("--extract-model", type=str, default=None, help="Claude model for Pass 1 (default: --model)")
@click.option("--seed", is_flag=True, help="Add DocType names and core Frappe terms to the glossary files first")
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=None, help="Config TOML file")
@click.option("--app", "-a", multiple=True, help="App to build the glossary for (repeatable, default: all)")
@click.option("--language", "-l", multiple=True, help="Target language (repeatable, default: all found)")
@click.option("--exclude-language", "-x", multiple=True, help="Language to skip (repeatable, e.g. eo)")
@click.option(
    "--glossary-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Folder with per-app glossary files (default: <bench>/glossaries)",
)
@click.option("--concurrency", type=click.IntRange(1, 50), default=5, help="Max parallel claude processes")
@click.option("--batch-size", type=int, default=10, help="Terms per batch (ignores [general] batch_size)")
@click.option("--timeout", type=int, default=600, help="Timeout per claude call in seconds (ignores [general] timeout)")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def glossary(
    ctx: click.Context,
    bench_path: Path,
    model: str,
    extract_model: str | None,
    seed: bool,
    config: Path | None,
    app: tuple[str, ...],
    language: tuple[str, ...],
    exclude_language: tuple[str, ...],
    glossary_dir: Path | None,
    concurrency: int,
    batch_size: int,
    timeout: int,
    verbose: bool,
) -> None:
    """Translate the terms in the glossary files with Claude."""
    model_name = model.lower()
    if "opus" in model_name:
        tier = "opus"
    elif "sonnet" in model_name:
        tier = "sonnet"
    else:
        raise click.UsageError(f"--model must be a sonnet or opus model, got {model!r}")

    _setup_logging(verbose)

    from frappe_translator.config import load_config

    cfg = load_config(config)
    cfg.bench_path = bench_path
    cfg.model = model
    if extract_model is not None:
        cfg.extract_model = extract_model
    if app:
        cfg.apps = list(app)
    if language:
        cfg.languages = list(language)
    if exclude_language:
        cfg.exclude_languages = list(exclude_language)
    if glossary_dir is not None:
        cfg.glossary_dir = glossary_dir
    source = ctx.get_parameter_source
    if source("concurrency") != click.core.ParameterSource.DEFAULT:
        cfg.concurrency = concurrency
    # [general] batch_size and timeout fit `translate` with a fast model; this command needs sonnet or opus
    cfg.batch_size = batch_size
    cfg.timeout = timeout

    _check_claude_cli()

    from frappe_translator.pipeline import run_glossary

    try:
        failed = asyncio.run(run_glossary(cfg, tier, seed))
    except KeyboardInterrupt:
        click.echo("\nInterrupted. The glossary files are saved after each batch.", err=True)
        sys.exit(130)
    if failed:
        click.echo(f"{failed} glossary batches failed. Run again to retry them.", err=True)
        sys.exit(1)


@main.command()
@click.argument("bench_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--app", "-a", multiple=True, help="App to check (repeatable, default: all)")
def status(bench_path: Path, app: tuple[str, ...]) -> None:
    """Show translation coverage statistics."""
    _setup_logging(verbose=False)

    from frappe_translator.discovery import discover_bench, resolve_app_order
    from frappe_translator.po_handler import read_po_translations, read_pot_entries

    discovered = discover_bench(bench_path)
    if app:
        discovered = [a for a in discovered if a.name in app]
    discovered = resolve_app_order(discovered, ["frappe", "erpnext", "hrms"])

    for app_info in discovered:
        entries = read_pot_entries(app_info.pot_path)
        total = len(entries)
        click.echo(f"\n{app_info.name}: {total} entries")

        for locale, po_path in sorted(app_info.po_paths.items()):
            translations = read_po_translations(po_path)
            translated = sum(1 for v in translations.values() if v.strip())
            pct = (translated / total * 100) if total > 0 else 0
            click.echo(f"  {locale}: {translated}/{total} ({pct:.1f}%)")


@main.command("clear-progress")
@click.argument("bench_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def clear_progress(bench_path: Path) -> None:
    """Clear saved progress state for a fresh run."""
    from frappe_translator.progress import ProgressTracker

    tracker = ProgressTracker(bench_path)
    tracker.clear()

    # Also clear glossary extraction cache
    extracted_path = bench_path / "glossary_extracted.json"
    if extracted_path.exists():
        extracted_path.unlink()

    click.echo("Progress cleared.")


def _check_claude_cli() -> None:
    """Check that the claude CLI is installed and accessible."""
    import shutil

    if not shutil.which("claude"):
        click.echo(
            "Error: 'claude' CLI not found. Please install it first.\n"
            "See: https://docs.anthropic.com/en/docs/claude-cli",
            err=True,
        )
        sys.exit(1)


def _dry_run(cfg: TranslatorConfig) -> None:
    """Show what would be translated without running."""
    from frappe_translator.discovery import (
        discover_bench,
        get_app_languages,
        get_target_languages,
        resolve_app_order,
    )
    from frappe_translator.po_handler import filter_entries, read_po_translations, read_pot_entries

    all_apps = discover_bench(cfg.bench_path)
    if cfg.apps:
        all_apps = [a for a in all_apps if a.name in cfg.apps]
    apps = resolve_app_order(all_apps, cfg.app_priority)

    target_languages = get_target_languages(apps, cfg.languages or None, cfg.exclude_languages)

    click.echo(f"Mode: {cfg.mode}")
    click.echo(f"Target languages ({len(target_languages)}): {', '.join(target_languages)}")
    click.echo(f"Concurrency: {cfg.concurrency}")
    click.echo(f"Glossary: {'disabled' if cfg.skip_glossary else 'enabled'}")
    click.echo()

    total_entries = 0
    for app in apps:
        entries = read_pot_entries(app.pot_path)
        non_plural = [e for e in entries if not e.is_plural]

        app_languages = get_app_languages(app, target_languages)

        if app_languages:
            # Estimate by filtering against the first app-local locale; actual
            # per-locale missing sets are resolved during the real run.
            po_translations = read_po_translations(app.po_paths[app_languages[0]])
            filtered = filter_entries(non_plural, po_translations, cfg.mode)
        else:
            filtered = []

        plural_count = len(entries) - len(non_plural)
        locales_str = ", ".join(app_languages) if app_languages else "none"
        click.echo(
            f"{app.name}: {len(filtered)} entries x {len(app_languages)} locales [{locales_str}]"
            f" (of {len(entries)} total, {plural_count} plural skipped)"
        )
        total_entries += len(filtered)

    click.echo(f"\nTotal: {total_entries} entries across {len(apps)} apps")
    estimated_batches = (total_entries + cfg.batch_size - 1) // cfg.batch_size
    if not cfg.skip_glossary:
        click.echo(f"Estimated claude calls: ~{estimated_batches} (Pass 1) + ~{estimated_batches} (Pass 2)")
    else:
        click.echo(f"Estimated claude calls: ~{estimated_batches} (single pass)")
