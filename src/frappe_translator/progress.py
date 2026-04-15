"""Per-language progress tracking with resume support."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from frappe_translator._io import atomic_json_write

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PROGRESS_FILENAME = ".frappe-translator-progress.json"


def _entry_key(msgid: str, msgctxt: str | None) -> str:
    """Create a unique key for a translation entry.

    Uses null byte as separator since it cannot appear in PO msgid/msgctxt strings,
    avoiding collisions when msgid itself contains '::'.
    """
    if msgctxt:
        return f"{msgid}\x00{msgctxt}"
    return msgid


class ProgressTracker:
    """Tracks per-language translation progress for resume support.

    Schema: {app: {entry_key: {"done": [lang, ...], "error": {lang: msg}, "retries": {lang: count}}}}
    """

    def __init__(self, bench_path: Path, max_retries: int = 2) -> None:
        self._path = bench_path / PROGRESS_FILENAME
        self._max_retries = max_retries
        self._data: dict[str, dict[str, dict]] = {}
        self._dirty = False

    def load(self) -> None:
        """Load progress from disk."""
        if self._path.exists():
            with open(self._path) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning("Corrupt progress file (not a dict), resetting")
                self._data = {}
                return
            self._data = raw
            logger.info("Loaded progress from %s", self._path)
        else:
            self._data = {}

    def save(self) -> None:
        """Save progress to disk."""
        atomic_json_write(self._path, self._data, indent=2)
        self._dirty = False

    def clear(self) -> None:
        """Clear all progress data and delete the file."""
        self._data = {}
        if self._path.exists():
            self._path.unlink()

    def _get_entry(self, app: str, msgid: str, msgctxt: str | None) -> dict:
        """Get or create the progress entry for an app+entry."""
        key = _entry_key(msgid, msgctxt)
        app_data = self._data.setdefault(app, {})
        return app_data.setdefault(key, {"done": [], "error": {}, "retries": {}})

    def get_pending_languages(self, app: str, msgid: str, msgctxt: str | None, all_languages: list[str]) -> list[str]:
        """Return languages not yet successfully translated for this entry.

        Languages in 'done' are skipped. Languages in 'error' are retried
        unless they've exceeded max_retries.
        """
        entry = self._get_entry(app, msgid, msgctxt)
        done = set(entry.get("done", []))
        retries = entry.get("retries", {})

        pending = []
        for lang in all_languages:
            if lang in done:
                continue
            retry_count = retries.get(lang, 0)
            if retry_count >= self._max_retries:
                continue
            pending.append(lang)

        return pending

    def is_fully_done(self, app: str, msgid: str, msgctxt: str | None, all_languages: list[str]) -> bool:
        """Check if all target languages are done for this entry."""
        return len(self.get_pending_languages(app, msgid, msgctxt, all_languages)) == 0

    def mark_languages_done(self, app: str, msgid: str, msgctxt: str | None, languages: list[str]) -> None:
        """Mark languages as successfully translated."""
        entry = self._get_entry(app, msgid, msgctxt)
        done = set(entry.get("done", []))
        done.update(languages)
        entry["done"] = sorted(done)
        # Clear errors for these languages
        for lang in languages:
            entry.get("error", {}).pop(lang, None)
            entry.get("retries", {}).pop(lang, None)
        self._dirty = True

    def mark_language_error(self, app: str, msgid: str, msgctxt: str | None, language: str, error: str) -> None:
        """Record an error for a specific language."""
        entry = self._get_entry(app, msgid, msgctxt)
        entry.setdefault("error", {})[language] = error
        retries = entry.setdefault("retries", {})
        retries[language] = retries.get(language, 0) + 1
        self._dirty = True

    def get_errors(self, app: str, msgid: str, msgctxt: str | None) -> dict[str, str]:
        """Get per-language error map for an entry."""
        entry = self._get_entry(app, msgid, msgctxt)
        return dict(entry.get("error", {}))

    def get_summary(self) -> dict[str, dict[str, int]]:
        """Get summary statistics per app."""
        summary = {}
        for app, entries in self._data.items():
            done_count = 0
            error_count = 0
            for entry_data in entries.values():
                done_count += len(entry_data.get("done", []))
                error_count += len(entry_data.get("error", {}))
            summary[app] = {"done": done_count, "errors": error_count}
        return summary
