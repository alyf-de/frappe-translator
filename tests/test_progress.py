"""Tests for ProgressTracker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from frappe_translator.progress import PROGRESS_FILENAME, ProgressTracker


class TestProgressTrackerSaveLoad:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("sample_app", "Save", None, ["de", "fr"])
        tracker.save()

        tracker2 = ProgressTracker(tmp_path)
        tracker2.load()
        pending = tracker2.get_pending_languages("sample_app", "Save", None, ["de", "fr"])
        assert pending == []

    def test_load_creates_empty_when_no_file(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.load()
        pending = tracker.get_pending_languages("app", "msgid", None, ["de"])
        assert pending == ["de"]


class TestGetPendingLanguages:
    def test_returns_all_languages_initially(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        pending = tracker.get_pending_languages("app", "Save", None, ["de", "fr", "es"])
        assert pending == ["de", "fr", "es"]

    def test_done_languages_excluded(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("app", "Save", None, ["de"])
        pending = tracker.get_pending_languages("app", "Save", None, ["de", "fr"])
        assert "de" not in pending
        assert "fr" in pending


class TestMarkLanguagesDone:
    def test_removes_from_pending(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("app", "Save", None, ["de", "fr"])
        pending = tracker.get_pending_languages("app", "Save", None, ["de", "fr"])
        assert pending == []

    def test_clears_errors_for_done_languages(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_language_error("app", "Save", None, "de", "timeout")
        tracker.mark_languages_done("app", "Save", None, ["de"])
        errors = tracker.get_errors("app", "Save", None)
        assert "de" not in errors


class TestMarkLanguageError:
    def test_records_error_message(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_language_error("app", "Save", None, "de", "API timeout")
        errors = tracker.get_errors("app", "Save", None)
        assert errors["de"] == "API timeout"

    def test_increments_retry_count(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_language_error("app", "Save", None, "de", "err1")
        tracker.mark_language_error("app", "Save", None, "de", "err2")
        # After 2 errors with max_retries=2, language should be excluded
        pending = tracker.get_pending_languages("app", "Save", None, ["de"])
        assert "de" not in pending


class TestMaxRetries:
    def test_stops_returning_language_after_max_retries(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path, max_retries=2)
        tracker.mark_language_error("app", "Save", None, "de", "err")
        tracker.mark_language_error("app", "Save", None, "de", "err")
        pending = tracker.get_pending_languages("app", "Save", None, ["de"])
        assert "de" not in pending

    def test_still_pending_before_max_retries(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path, max_retries=3)
        tracker.mark_language_error("app", "Save", None, "de", "err")
        tracker.mark_language_error("app", "Save", None, "de", "err")
        pending = tracker.get_pending_languages("app", "Save", None, ["de"])
        assert "de" in pending


class TestIsFullyDone:
    def test_returns_false_initially(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        assert not tracker.is_fully_done("app", "Save", None, ["de", "fr"])

    def test_returns_true_when_all_done(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("app", "Save", None, ["de", "fr"])
        assert tracker.is_fully_done("app", "Save", None, ["de", "fr"])

    def test_returns_true_for_empty_language_list(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        assert tracker.is_fully_done("app", "Save", None, [])


class TestClear:
    def test_clear_removes_file(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("app", "Save", None, ["de"])
        tracker.save()
        assert (tmp_path / PROGRESS_FILENAME).exists()

        tracker.clear()
        assert not (tmp_path / PROGRESS_FILENAME).exists()

    def test_clear_resets_in_memory_data(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path)
        tracker.mark_languages_done("app", "Save", None, ["de"])
        tracker.clear()
        pending = tracker.get_pending_languages("app", "Save", None, ["de"])
        assert "de" in pending
