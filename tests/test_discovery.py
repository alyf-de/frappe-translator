"""Tests for bench and app discovery."""

from __future__ import annotations

from pathlib import Path

from frappe_translator.discovery import discover_bench, get_app_languages, get_target_languages, resolve_app_order
from frappe_translator.models import AppInfo


class TestDiscoverBench:
    def test_finds_sample_app(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        assert len(apps) == 1
        assert apps[0].name == "sample_app"

    def test_correct_pot_path(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        expected = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "main.pot"
        assert apps[0].pot_path == expected

    def test_correct_po_paths(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        po_paths = apps[0].po_paths
        assert set(po_paths.keys()) == {"de", "fr"}
        assert po_paths["de"].name == "de.po"
        assert po_paths["fr"].name == "fr.po"

    def test_returns_empty_when_no_apps_dir(self, tmp_path: Path) -> None:
        apps = discover_bench(tmp_path)
        assert apps == []

    def test_skips_apps_without_pot(self, tmp_bench: Path) -> None:
        # Add a second app directory without a main.pot
        no_pot_dir = tmp_bench / "apps" / "no_pot_app" / "no_pot_app" / "locale"
        no_pot_dir.mkdir(parents=True)
        apps = discover_bench(tmp_bench)
        names = [a.name for a in apps]
        assert "no_pot_app" not in names


class TestResolveAppOrder:
    def _make_app(self, name: str) -> AppInfo:
        p = Path(f"/fake/{name}")
        return AppInfo(name=name, path=p, pot_path=p / "main.pot")

    def test_puts_priority_apps_first(self) -> None:
        apps = [self._make_app("erpnext"), self._make_app("frappe"), self._make_app("myapp")]
        ordered = resolve_app_order(apps, ["frappe", "erpnext"])
        assert ordered[0].name == "frappe"
        assert ordered[1].name == "erpnext"

    def test_remaining_apps_alphabetical(self) -> None:
        apps = [self._make_app("zebra"), self._make_app("alpha"), self._make_app("frappe")]
        ordered = resolve_app_order(apps, ["frappe"])
        assert ordered[0].name == "frappe"
        assert ordered[1].name == "alpha"
        assert ordered[2].name == "zebra"

    def test_priority_apps_not_in_list_ignored(self) -> None:
        apps = [self._make_app("myapp")]
        ordered = resolve_app_order(apps, ["frappe"])
        assert len(ordered) == 1
        assert ordered[0].name == "myapp"


class TestGetTargetLanguages:
    def test_returns_all_locales(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        langs = get_target_languages(apps, None)
        assert sorted(langs) == ["de", "fr"]

    def test_filters_when_filter_provided(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        langs = get_target_languages(apps, ["de"])
        assert langs == ["de"]

    def test_filter_excludes_unknown_langs(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        langs = get_target_languages(apps, ["es"])
        assert langs == []

    def test_returns_sorted(self, tmp_bench: Path) -> None:
        apps = discover_bench(tmp_bench)
        langs = get_target_languages(apps, None)
        assert langs == sorted(langs)

    def test_union_across_uneven_apps(self, tmp_bench_uneven: Path) -> None:
        apps = discover_bench(tmp_bench_uneven)
        langs = get_target_languages(apps, None)
        assert langs == ["de", "fr"]


class TestGetAppLanguages:
    def _make_app(self, name: str, locales: list[str]) -> AppInfo:
        base = Path(f"/fake/{name}")
        return AppInfo(
            name=name,
            path=base,
            pot_path=base / "main.pot",
            po_paths={loc: base / f"{loc}.po" for loc in locales},
        )

    def test_intersects_target_with_app_po_paths(self) -> None:
        app = self._make_app("a", ["de", "fr"])
        assert get_app_languages(app, ["de", "fr", "es"]) == ["de", "fr"]

    def test_preserves_target_order(self) -> None:
        app = self._make_app("a", ["de", "fr", "es"])
        assert get_app_languages(app, ["fr", "de"]) == ["fr", "de"]

    def test_returns_empty_when_no_overlap(self) -> None:
        app = self._make_app("a", ["de"])
        assert get_app_languages(app, ["fr", "es"]) == []

    def test_returns_empty_when_app_has_no_po_paths(self) -> None:
        app = self._make_app("a", [])
        assert get_app_languages(app, ["de", "fr"]) == []

    def test_returns_empty_when_target_list_empty(self) -> None:
        app = self._make_app("a", ["de", "fr"])
        assert get_app_languages(app, []) == []

    def test_matches_per_app_coverage_on_real_bench(self, tmp_bench_uneven: Path) -> None:
        apps = {a.name: a for a in discover_bench(tmp_bench_uneven)}
        assert get_app_languages(apps["app_full"], ["de", "fr"]) == ["de", "fr"]
        assert get_app_languages(apps["app_de_only"], ["de", "fr"]) == ["de"]
