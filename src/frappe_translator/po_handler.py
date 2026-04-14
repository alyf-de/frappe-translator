"""PO/POT file reading, writing, and management."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path  # noqa: TC003

import polib

from frappe_translator.models import TranslationEntry, TranslationResult

logger = logging.getLogger(__name__)


def read_pot_entries(pot_path: Path) -> list[TranslationEntry]:
    """Read all entries from a POT file using polib."""
    pot = polib.pofile(str(pot_path))
    entries: list[TranslationEntry] = []
    for entry in pot:
        source_refs = [f"{loc[0]}:{loc[1]}" for loc in entry.occurrences]
        is_plural = bool(entry.msgid_plural)
        entries.append(
            TranslationEntry(
                msgid=entry.msgid,
                msgstr=entry.msgstr,
                msgctxt=entry.msgctxt or None,
                source_refs=source_refs,
                comments=list(entry.comment.splitlines()) if entry.comment else [],
                flags=list(entry.flags),
                is_plural=is_plural,
            )
        )
    return entries


def read_po_translations(po_path: Path) -> dict[tuple[str, str | None], str]:
    """Read existing translations from a PO file. Key is (msgid, msgctxt), value is msgstr."""
    po = polib.pofile(str(po_path))
    result: dict[tuple[str, str | None], str] = {}
    for entry in po:
        key = (entry.msgid, entry.msgctxt or None)
        result[key] = entry.msgstr
    return result


def filter_entries(
    entries: list[TranslationEntry],
    po_translations: dict[tuple[str, str | None], str],
    mode: str,
) -> list[TranslationEntry]:
    """Filter entries based on translation mode.

    Modes:
    - "fill-missing": entries missing or empty in po_translations
    - "review-existing": entries with non-empty translations
    - "full-correct": all entries
    """
    result: list[TranslationEntry] = []
    for entry in entries:
        if entry.is_plural:
            logger.debug("Skipping plural entry: %r", entry.msgid)
            continue
        key = (entry.msgid, entry.msgctxt)
        existing = po_translations.get(key, "")
        if mode == "fill-missing":
            if not existing:
                result.append(entry)
        elif mode == "review-existing":
            if existing:
                result.append(entry)
        elif mode == "full-correct":
            result.append(entry)
        else:
            logger.warning("Unknown filter mode %r, defaulting to full-correct", mode)
            result.append(entry)
    return result


def lookup_term_translations(term: str, all_po_paths: dict[str, dict[str, Path]]) -> dict[str, str]:
    """Search across all apps' PO files for existing translations of a term.

    Args:
        term: The source term to look up.
        all_po_paths: {app_name: {locale: Path}}

    Returns:
        {locale: translated_term} — first non-empty match per locale wins.
    """
    found: dict[str, str] = {}
    for _app_name, locale_paths in all_po_paths.items():
        for locale, po_path in locale_paths.items():
            if locale in found:
                continue
            try:
                translations = read_po_translations(po_path)
            except Exception:
                logger.warning("Failed to read PO file %s", po_path, exc_info=True)
                continue
            for (msgid, _msgctxt), msgstr in translations.items():
                if msgid == term and msgstr:
                    found[locale] = msgstr
                    break
    return found


def lookup_terms_batch(terms: list[str], all_po_paths: dict[str, dict[str, Path]]) -> dict[str, dict[str, str]]:
    """Look up translations for multiple terms, parsing each PO file only once.

    Args:
        terms: List of source terms to look up.
        all_po_paths: {app_name: {locale: Path}}

    Returns:
        {term: {locale: translated_term}} — first non-empty match per locale wins.
    """
    terms_set = set(terms)
    # {term: {locale: translation}}
    found: dict[str, dict[str, str]] = {t: {} for t in terms}

    for _app_name, locale_paths in all_po_paths.items():
        for locale, po_path in locale_paths.items():
            try:
                translations = read_po_translations(po_path)
            except Exception:
                logger.warning("Failed to read PO file %s", po_path, exc_info=True)
                continue
            for (msgid, _msgctxt), msgstr in translations.items():
                if msgid in terms_set and msgstr and locale not in found.get(msgid, {}):
                    found.setdefault(msgid, {})[locale] = msgstr
    return found


class POWriter:
    """Batched writer for PO files with per-locale async locking."""

    def __init__(self, po_paths: dict[str, Path]) -> None:
        self.po_paths = po_paths
        self._locks: dict[str, asyncio.Lock] = {locale: asyncio.Lock() for locale in po_paths}
        self._buffers: dict[str, list[tuple[str, str | None, str]]] = {locale: [] for locale in po_paths}

    def buffer_translation(self, result: TranslationResult) -> None:
        """Buffer translations from a TranslationResult for later flushing."""
        for locale, translation in result.translations.items():
            if locale not in self._buffers:
                logger.warning("Locale %r not in known po_paths, skipping", locale)
                continue
            self._buffers[locale].append((result.msgid, result.msgctxt, translation))

    async def flush(self, locale: str | None = None) -> None:
        """Flush buffered translations to disk.

        If locale is given, flush only that locale. Otherwise flush all locales sequentially.
        """
        if locale is not None:
            await self._flush_locale(locale)
        else:
            for loc in list(self._buffers.keys()):
                await self._flush_locale(loc)

    async def _flush_locale(self, locale: str) -> None:
        """Flush a single locale's buffer to its PO file."""
        if locale not in self._locks:
            logger.warning("No lock for locale %r", locale)
            return
        async with self._locks[locale]:
            buffer = self._buffers.get(locale, [])
            if not buffer:
                return
            po_path = self.po_paths[locale]
            # Run blocking polib I/O in a thread to avoid stalling the event loop
            count = len(buffer)
            try:
                await asyncio.to_thread(self._write_po_file, po_path, buffer)
            except Exception:
                logger.error("Failed to flush locale %s to %s", locale, po_path, exc_info=True)
                return
            self._buffers[locale] = []
            logger.debug("Flushed %d translations to %s", count, po_path)

    @staticmethod
    def _write_po_file(po_path: Path, buffer: list[tuple[str, str | None, str]]) -> None:
        """Blocking I/O: load, modify, and save a PO file. Runs in a thread."""
        po = polib.pofile(str(po_path))
        for msgid, msgctxt, translated_str in buffer:
            entry = po.find(msgid, msgctxt=msgctxt)
            if entry is not None:
                entry.msgstr = translated_str
            else:
                logger.debug("Entry not found in %s: msgid=%r msgctxt=%r", po_path, msgid, msgctxt)
        po.save(str(po_path))

    async def flush_all(self) -> None:
        """Flush all locale buffers concurrently."""
        await asyncio.gather(*[self._flush_locale(locale) for locale in self._buffers])

    @property
    def pending_count(self) -> int:
        """Total buffered translations across all locales."""
        return sum(len(buf) for buf in self._buffers.values())
