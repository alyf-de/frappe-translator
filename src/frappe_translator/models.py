"""Data models for frappe-translator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class TranslationEntry:
    """A single translatable string from a POT/PO file."""

    msgid: str
    msgstr: str = ""
    msgctxt: str | None = None
    source_refs: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    is_plural: bool = False


@dataclass
class SourceSnippet:
    """A source code snippet around a translation reference."""

    file_path: str
    line_number: int
    content: str


@dataclass
class TermGlossary:
    """Glossary of key terms with their existing translations per locale."""

    terms: dict[str, dict[str, str]] = field(default_factory=dict)

    _compiled_pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def _build_pattern(self) -> None:
        """Build a single compiled regex matching any glossary term."""
        if not self.terms:
            self._compiled_pattern = None
            return
        # Sort by length descending so longer terms match first
        sorted_terms = sorted(self.terms.keys(), key=lambda t: -len(t))
        pattern = "|".join(rf"\b{re.escape(t)}\b" for t in sorted_terms)
        self._compiled_pattern = re.compile(pattern, re.IGNORECASE)

    def get_relevant_terms(self, text: str) -> dict[str, dict[str, str]]:
        """Return glossary entries for terms that appear in the given text."""
        if self._compiled_pattern is None:
            self._build_pattern()
        if self._compiled_pattern is None:
            return {}
        matches = self._compiled_pattern.findall(text)
        if not matches:
            return {}
        # Normalize matched terms back to glossary keys (case-insensitive lookup)
        terms_lower = {t.lower(): t for t in self.terms}
        relevant = {}
        for m in matches:
            key = terms_lower.get(m.lower())
            if key and key not in relevant:
                relevant[key] = self.terms[key]
        return relevant


@dataclass
class TranslationResult:
    """Result of translating a single entry into multiple languages."""

    msgid: str
    msgctxt: str | None = None
    translations: dict[str, str] = field(default_factory=dict)
    skipped: bool = False
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class AssembledContext:
    """Fully assembled context for a Pass 2 translation prompt."""

    entry: TranslationEntry
    snippets: list[SourceSnippet] = field(default_factory=list)
    glossary_terms: dict[str, dict[str, str]] = field(default_factory=dict)
    prompt: str = ""
    target_languages: list[str] = field(default_factory=list)


@dataclass
class AppInfo:
    """Information about a Frappe app in the bench."""

    name: str
    path: Path
    pot_path: Path
    po_paths: dict[str, Path] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
