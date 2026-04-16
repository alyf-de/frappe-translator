"""Tests for the CLI entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from click.testing import CliRunner

from frappe_translator.cli import main


class TestHelp:
    def test_main_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "frappe" in result.output.lower() or "translator" in result.output.lower()

    def test_translate_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["translate", "--help"])
        assert result.exit_code == 0
        assert "bench_path" in result.output.lower() or "BENCH_PATH" in result.output

    def test_status_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_clear_progress_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["clear-progress", "--help"])
        assert result.exit_code == 0


class TestStatus:
    def test_shows_app_name(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", str(tmp_bench)])
        assert result.exit_code == 0
        assert "sample_app" in result.output

    def test_shows_entry_count(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", str(tmp_bench)])
        assert result.exit_code == 0
        # 4 entries in the POT file
        assert "4" in result.output

    def test_shows_locale_coverage(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", str(tmp_bench)])
        assert result.exit_code == 0
        # Both de and fr locales should appear
        assert "de" in result.output
        assert "fr" in result.output

    def test_shows_percentage(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", str(tmp_bench)])
        assert result.exit_code == 0
        assert "%" in result.output

    def test_filters_by_app(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", str(tmp_bench), "--app", "sample_app"])
        assert result.exit_code == 0
        assert "sample_app" in result.output


class TestClearProgress:
    def test_works_on_bench_path(self, tmp_bench: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["clear-progress", str(tmp_bench)])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()

    def test_clear_progress_works_when_no_file(self, tmp_bench: Path) -> None:
        # Should not raise even when no progress file exists
        runner = CliRunner()
        result = runner.invoke(main, ["clear-progress", str(tmp_bench)])
        assert result.exit_code == 0

    def test_clear_progress_removes_file(self, tmp_bench: Path) -> None:
        from frappe_translator.progress import PROGRESS_FILENAME, ProgressTracker

        # Create a progress file
        tracker = ProgressTracker(tmp_bench)
        tracker.mark_languages_done("sample_app", "Save", None, ["de"])
        tracker.save()
        assert (tmp_bench / PROGRESS_FILENAME).exists()

        runner = CliRunner()
        runner.invoke(main, ["clear-progress", str(tmp_bench)])
        assert not (tmp_bench / PROGRESS_FILENAME).exists()


class TestDryRunPerAppLocales:
    def test_shows_per_app_locale_set_on_uneven_bench(self, tmp_bench_uneven: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["translate", str(tmp_bench_uneven), "--dry-run"])
        assert result.exit_code == 0

        output = result.output
        # Global target languages are the union.
        assert "Target languages (2): de, fr" in output

        # Each app line should list only the locales it actually has PO files for.
        app_full_line = next(line for line in output.splitlines() if line.startswith("app_full:"))
        app_de_line = next(line for line in output.splitlines() if line.startswith("app_de_only:"))

        assert "2 locales [de, fr]" in app_full_line
        assert "1 locales [de]" in app_de_line
        assert "fr" not in app_de_line

    def test_skips_locales_that_no_app_can_write(self, tmp_bench_uneven: Path) -> None:
        runner = CliRunner()
        # Request a locale that no app supports alongside one that only app_full has.
        result = runner.invoke(
            main,
            ["translate", str(tmp_bench_uneven), "--dry-run", "-l", "fr", "-l", "es"],
        )
        assert result.exit_code == 0

        output = result.output
        assert "Target languages (1): fr" in output

        app_full_line = next(line for line in output.splitlines() if line.startswith("app_full:"))
        app_de_line = next(line for line in output.splitlines() if line.startswith("app_de_only:"))

        assert "1 locales [fr]" in app_full_line
        # app_de_only has no fr.po, so it has no writable locales for this run.
        assert "0 locales [none]" in app_de_line
