"""PO/POT file reading, writing, and management.

Uses Babel for parsing and serialization so our output matches Frappe's
canonical format (no line wrapping, sort_output=True, ignore_obsolete=True)
and produces minimal diffs against ``bench update-po-files``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

from babel.messages.pofile import read_po, write_po

from frappe_translator.models import TranslationEntry, TranslationResult

if TYPE_CHECKING:
    from babel.messages.catalog import Catalog

logger = logging.getLogger(__name__)


def _load_catalog(path: Path) -> Catalog:
    """Load a PO/POT file into a Babel ``Catalog``."""
    with path.open("rb") as f:
        return read_po(f)


def _save_catalog(catalog: Catalog, path: Path) -> None:
    """Write a Catalog in Frappe's canonical format.

    Matches ``frappe.gettext.translate.write_catalog``:
    ``width=0`` (Babel treats 0 and None identically — both disable msgid/msgstr
    wrapping; we use 0 for type-stub compatibility), ``sort_output=True``
    (entries sorted by msgid), ``ignore_obsolete=True`` (drop ``#~`` entries).
    """
    with path.open("wb") as f:
        write_po(f, catalog, width=0, sort_output=True, ignore_obsolete=True)


def _message_msgid(message) -> str:
    """Return the singular msgid for a Babel ``Message``."""
    return message.id[0] if isinstance(message.id, tuple) else message.id


def _message_msgstr(message) -> str:
    """Return the msgstr for a non-plural Babel ``Message``, or empty for plurals."""
    if isinstance(message.string, tuple):
        return ""
    return message.string or ""


def read_pot_entries(pot_path: Path) -> list[TranslationEntry]:
    """Read all entries from a POT file."""
    catalog = _load_catalog(pot_path)
    entries: list[TranslationEntry] = []
    for message in catalog:
        if not message.id:
            continue
        is_plural = isinstance(message.id, tuple)
        source_refs = [f"{fname}:{lineno}" if lineno else fname for fname, lineno in message.locations]
        entries.append(
            TranslationEntry(
                msgid=_message_msgid(message),
                msgstr=_message_msgstr(message),
                msgctxt=message.context or None,
                source_refs=source_refs,
                comments=list(message.auto_comments) + list(message.user_comments),
                flags=sorted(message.flags),
                is_plural=is_plural,
            )
        )
    return entries


def read_po_translations(po_path: Path) -> dict[tuple[str, str | None], str]:
    """Read existing translations from a PO file. Key is (msgid, msgctxt), value is msgstr."""
    catalog = _load_catalog(po_path)
    result: dict[tuple[str, str | None], str] = {}
    for message in catalog:
        if not message.id:
            continue
        key = (_message_msgid(message), message.context or None)
        result[key] = _message_msgstr(message)
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
    """Batched writer for PO files with per-locale async locking.

    Keeps Babel Catalogs loaded in memory so flushes only save (no re-parse),
    and uses ``Catalog.get(msgid, context=...)`` for O(1) entry lookup.
    """

    def __init__(self, po_paths: dict[str, Path]) -> None:
        self.po_paths = po_paths
        self._locks: dict[str, asyncio.Lock] = {locale: asyncio.Lock() for locale in po_paths}
        self._buffers: dict[str, list[tuple[str, str | None, str]]] = {locale: [] for locale in po_paths}
        self._catalogs: dict[str, Catalog] = {}

    def _get_catalog(self, locale: str) -> Catalog:
        """Get or lazily load the Catalog for a locale."""
        if locale not in self._catalogs:
            self._catalogs[locale] = _load_catalog(self.po_paths[locale])
        return self._catalogs[locale]

    def get_translations(self, locale: str) -> dict[tuple[str, str | None], str]:
        """Return ``{(msgid, msgctxt): msgstr}`` for a locale, using the cached Catalog.

        Lets callers reuse already-parsed PO data instead of re-reading from disk.
        """
        catalog = self._get_catalog(locale)
        return {
            (_message_msgid(message), message.context or None): _message_msgstr(message)
            for message in catalog
            if message.id
        }

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
            count = len(buffer)
            try:
                await asyncio.to_thread(self._apply_and_save, locale, buffer, po_path)
            except Exception:
                logger.error("Failed to flush locale %s to %s", locale, po_path, exc_info=True)
                return
            self._buffers[locale] = []
            logger.debug("Flushed %d translations to %s", count, po_path)

    def _apply_and_save(self, locale: str, buffer: list[tuple[str, str | None, str]], po_path: Path) -> None:
        """Blocking I/O: apply buffered translations and save. Runs in a thread."""
        catalog = self._get_catalog(locale)
        for msgid, msgctxt, translated_str in buffer:
            message = catalog.get(msgid, context=msgctxt)
            if message is not None:
                message.string = translated_str
            else:
                logger.debug("Entry not found in %s: msgid=%r msgctxt=%r", po_path, msgid, msgctxt)
        _save_catalog(catalog, po_path)

    async def flush_all(self) -> None:
        """Flush all locale buffers concurrently."""
        await asyncio.gather(*[self._flush_locale(locale) for locale in self._buffers])

    @property
    def pending_count(self) -> int:
        """Total buffered translations across all locales."""
        return sum(len(buf) for buf in self._buffers.values())
