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
@click.option("--resume/--no-resume", default=True, help="Resume from previous progress")
@click.option("--dry-run", is_flag=True, help="Show what would be translated without running")
@click.option("--skip-glossary", is_flag=True, help="Skip Pass 1 term extraction (single-pass mode)")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def translate(
    bench_path: Path,
    config: Path | None,
    app: tuple[str, ...],
    language: tuple[str, ...],
    mode: str,
    concurrency: int,
    batch_size: int,
    timeout: int,
    model: str | None,
    resume: bool,
    dry_run: bool,
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
    cfg.mode = mode
    cfg.concurrency = concurrency
    cfg.batch_size = batch_size
    cfg.timeout = timeout
    if model is not None:
        cfg.model = model
    cfg.resume = resume
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
    from frappe_translator.discovery import discover_bench, get_target_languages, resolve_app_order
    from frappe_translator.po_handler import filter_entries, read_po_translations, read_pot_entries

    all_apps = discover_bench(cfg.bench_path)
    if cfg.apps:
        all_apps = [a for a in all_apps if a.name in cfg.apps]
    apps = resolve_app_order(all_apps, cfg.app_priority)

    target_languages = get_target_languages(apps, cfg.languages or None)

    click.echo(f"Mode: {cfg.mode}")
    click.echo(f"Target languages ({len(target_languages)}): {', '.join(target_languages)}")
    click.echo(f"Concurrency: {cfg.concurrency}")
    click.echo(f"Glossary: {'disabled' if cfg.skip_glossary else 'enabled'}")
    click.echo()

    total_entries = 0
    for app in apps:
        entries = read_pot_entries(app.pot_path)
        non_plural = [e for e in entries if not e.is_plural]

        if target_languages and app.po_paths:
            first_locale = target_languages[0]
            if first_locale in app.po_paths:
                po_translations = read_po_translations(app.po_paths[first_locale])
                filtered = filter_entries(non_plural, po_translations, cfg.mode)
            else:
                filtered = non_plural
        else:
            filtered = non_plural

        plural_count = len(entries) - len(non_plural)
        click.echo(
            f"{app.name}: {len(filtered)} entries to process (of {len(entries)} total, {plural_count} plural skipped)"
        )
        total_entries += len(filtered)

    click.echo(f"\nTotal: {total_entries} entries x {len(target_languages)} languages")
    estimated_batches = (total_entries + cfg.batch_size - 1) // cfg.batch_size
    if not cfg.skip_glossary:
        click.echo(f"Estimated claude calls: ~{estimated_batches} (Pass 1) + ~{estimated_batches} (Pass 2)")
    else:
        click.echo(f"Estimated claude calls: ~{estimated_batches} (single pass)")
