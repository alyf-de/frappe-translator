"""Data models for frappe-translator."""

from __future__ import annotations

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

    def get_relevant_terms(self, text: str) -> dict[str, dict[str, str]]:
        """Return glossary entries for terms that appear in the given text."""
        import re

        relevant = {}
        for term, translations in self.terms.items():
            if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
                relevant[term] = translations
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


@dataclass
class AppInfo:
    """Information about a Frappe app in the bench."""

    name: str
    path: Path
    pot_path: Path
    po_paths: dict[str, Path] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
