"""Bench and app discovery for frappe-translator."""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003

from frappe_translator.models import AppInfo

logger = logging.getLogger(__name__)


def discover_bench(bench_path: Path) -> list[AppInfo]:
    """Scan the apps/ directory in a bench and return AppInfo for apps with a main.pot."""
    apps_dir = bench_path / "apps"
    if not apps_dir.is_dir():
        logger.warning("No apps/ directory found at %s", bench_path)
        return []

    result: list[AppInfo] = []
    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        app_name = app_dir.name
        pot_path = app_dir / app_name / "locale" / "main.pot"
        if not pot_path.exists():
            logger.debug("Skipping %s: no main.pot found at %s", app_name, pot_path)
            continue

        locale_dir = pot_path.parent
        po_paths: dict[str, Path] = {}
        for po_file in locale_dir.glob("*.po"):
            lang = po_file.stem
            po_paths[lang] = po_file

        result.append(AppInfo(name=app_name, path=app_dir, pot_path=pot_path, po_paths=po_paths))
        logger.debug("Discovered app %s with %d locale(s)", app_name, len(po_paths))

    return result


def resolve_app_order(apps: list[AppInfo], priority: list[str]) -> list[AppInfo]:
    """Sort apps: priority apps first (in order), then remaining apps alphabetically."""
    priority_apps: list[AppInfo] = []
    remaining_apps: list[AppInfo] = []

    app_by_name = {app.name: app for app in apps}

    for name in priority:
        if name in app_by_name:
            priority_apps.append(app_by_name[name])

    priority_set = set(priority)
    for app in sorted(apps, key=lambda a: a.name):
        if app.name not in priority_set:
            remaining_apps.append(app)

    return priority_apps + remaining_apps


def get_target_languages(apps: list[AppInfo], language_filter: list[str] | None) -> list[str]:
    """Get the union of all locale codes found across apps, optionally filtered."""
    found: set[str] = set()
    for app in apps:
        found.update(app.po_paths.keys())

    if language_filter:
        found = found & set(language_filter)

    return sorted(found)
