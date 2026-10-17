"""Per-app glossary files, merged along the app priority chain.

File format of ``<glossary_dir>/<app>.json``::

    {"Opening": {"de": {"text": "Eröffnung", "source": "opus"}}, "Party": {}}

A term with ``{}`` was extracted but has no curated translation yet.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from frappe_translator._io import atomic_json_write
from frappe_translator.po_handler import read_pot_entries

if TYPE_CHECKING:
    from pathlib import Path

    from frappe_translator.models import AppInfo

logger = logging.getLogger(__name__)

# {term: {lang: {"text": ..., "source": "manual" | "opus" | "sonnet"}}}
GlossaryFile = dict[str, dict[str, dict[str, str]]]

# A run may only write a (term, lang) pair whose current source ranks lower than its own.
SOURCE_RANK = {"sonnet": 1, "opus": 2, "manual": 3}

# Seeded into the first app's file (frappe) by `glossary --seed`.
CORE_TERMS = [
    "Submit",
    "Cancel",
    "Amend",
    "Draft",
    "Submitted",
    "Cancelled",
    "Opening",
    "Party",
    "Workspace",
    "Report",
    "DocType",
    "Workflow",
    "Role",
    "Permission",
    "Naming Series",
    "Print Format",
]


def load_glossary_file(glossary_dir: Path, app: str) -> GlossaryFile:
    path = glossary_dir / f"{app}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_glossary_file(glossary_dir: Path, app: str, data: GlossaryFile) -> None:
    glossary_dir.mkdir(parents=True, exist_ok=True)
    atomic_json_write(glossary_dir / f"{app}.json", data, indent=2, sort_keys=True)


def app_chain(app: str, app_names: set[str], priority: list[str]) -> list[str]:
    """Glossary files used when translating ``app``, lowest precedence first."""
    return [a for a in priority if a in app_names and a != app] + [app]


def merge_chain(files: dict[str, GlossaryFile], chain: list[str]) -> dict[str, dict[str, str]]:
    """Merge the chain's files into {term: {lang: text}}; later apps win per (term, lang)."""
    merged: dict[str, dict[str, str]] = {}
    for app in chain:
        for term, langs in files[app].items():
            merged_langs = merged.setdefault(term, {})
            for lang, entry in langs.items():
                merged_langs[lang] = entry["text"]
    return merged


def doctype_names(app: AppInfo) -> list[str]:
    """Names of the DocTypes defined in ``app`` (``<app>/<module>/doctype/<name>/<name>.json``)."""
    names = []
    for path in sorted((app.path / app.name).glob("*/doctype/*/*.json")):
        data = json.loads(path.read_text())
        # Other JSON files can live next to a DocType, e.g. test records as a list
        if isinstance(data, dict) and data.get("doctype") == "DocType":
            names.append(data["name"])
    return names


def seed_glossary(files: dict[str, GlossaryFile], apps: list[AppInfo], first_app: str, priority: list[str]) -> None:
    """Add CORE_TERMS to ``first_app`` and DocType names to the file of each app in ``apps``, as {}.

    A DocType name already in the app's chain is not added again. Existing entries are kept.
    """
    for term in CORE_TERMS:
        files[first_app].setdefault(term, {})
    app_names = set(files)
    for app in apps:
        chain = app_chain(app.name, app_names, priority)
        for name in doctype_names(app):
            if not any(name in files[a] for a in chain):
                files[app.name][name] = {}


def migrate_bench_glossary(bench_path: Path, glossary_dir: Path, apps: list[AppInfo], priority: list[str]) -> None:
    """One-time migration from the old bench-wide glossary files.

    ``apps`` are all discovered apps, in ``resolve_app_order(apps, priority)`` order.
    """
    extracted_path = bench_path / "glossary_extracted.json"
    old_glossary_path = bench_path / "glossary.json"
    extracted = json.loads(extracted_path.read_text()) if extracted_path.exists() else {}
    if not isinstance(extracted, list) and not old_glossary_path.exists():
        return

    pot_msgids = {app.name: [e.msgid for e in read_pot_entries(app.pot_path)] for app in apps}

    if isinstance(extracted, list):
        done = set(extracted)
        per_app = {app: sorted(done.intersection(msgids)) for app, msgids in pot_msgids.items()}
        atomic_json_write(extracted_path, {"glossary_dir": str(glossary_dir.resolve()), "apps": per_app})
        logger.info("Migrated %s to per-app format", extracted_path)

    if old_glossary_path.exists():
        # Old values are dropped: PO lookups now run at runtime and model output is not trusted.
        old_terms = json.loads(old_glossary_path.read_text())
        pot_texts = {app: "\n".join(msgids) for app, msgids in pot_msgids.items()}
        files: dict[str, GlossaryFile] = {}
        for term in old_terms:
            pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
            matches = [app for app, text in pot_texts.items() if pattern.search(text)] or [apps[0].name]
            # A priority app is in every chain; other apps don't see each other's files.
            owners = matches[:1] if matches[0] in priority else matches
            for owner in owners:
                if owner not in files:
                    files[owner] = load_glossary_file(glossary_dir, owner)
                files[owner].setdefault(term, {})
        for app, data in files.items():
            save_glossary_file(glossary_dir, app, data)
        migrated_path = old_glossary_path.with_name("glossary.json.migrated")
        old_glossary_path.rename(migrated_path)
        logger.info("Migrated %d terms from %s to %s", len(old_terms), old_glossary_path, glossary_dir)
