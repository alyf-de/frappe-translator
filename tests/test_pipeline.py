"""End-to-end pipeline tests with a fake ClaudeRunner."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from frappe_translator import pipeline
from frappe_translator.config import TranslatorConfig
from frappe_translator.discovery import discover_bench, resolve_app_order
from frappe_translator.glossary import load_glossary_file, migrate_bench_glossary
from frappe_translator.po_handler import read_po_translations, read_pot_entries
from tests.test_translator import RecordingRunner, _default_response

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class BatchRecordingRunner(RecordingRunner):
    """RecordingRunner that also serves the term extraction's run_batch."""

    async def run_batch(self, prompts: list[str], label: str) -> list[str]:
        return [await self.run(prompt) for prompt in prompts]


def _response(prompt: str, schema: str | None) -> str:
    if schema is None:  # term extraction
        return json.dumps({"terms": ["Invoice", "Hello", "Cancel"]})
    data = json.loads(_default_response(prompt, schema))
    # A term map the model might still volunteer; it must never be persisted
    data["terms"] = {"Invoice": {"de": "Model-Rechnung"}}
    return json.dumps(data)


def _add_app(bench: Path, name: str, msgids: list[str]) -> None:
    locale_dir = bench / "apps" / name / name / "locale"
    locale_dir.mkdir(parents=True)
    body = "".join(f'\nmsgid "{msgid}"\nmsgstr ""\n' for msgid in msgids)
    (locale_dir / "main.pot").write_text('msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=utf-8\\n"\n' + body)


def _write_glossary(bench: Path, app: str, data: dict) -> Path:
    path = bench / "glossaries" / f"{app}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return path


async def test_runtime_glossary_merges_chain_po_lookup_and_terminology(
    tmp_bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_app(tmp_bench, "frappe", ["Invoice"])
    _add_app(tmp_bench, "other_app", ["Save"])
    files = {
        "frappe": {
            "Invoice": {"de": {"text": "Rechnung", "source": "manual"}, "fr": {"text": "Facture-f", "source": "opus"}}
        },
        "sample_app": {"Invoice": {"fr": {"text": "Facture", "source": "opus"}}, "Cancel": {}},
        # Not in sample_app's chain: must not be used
        "other_app": {"Save": {"de": {"text": "Sichern", "source": "manual"}}},
    }
    paths = {app: _write_glossary(tmp_bench, app, data) for app, data in files.items()}
    runner = BatchRecordingRunner(_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    config = TranslatorConfig(
        bench_path=tmp_bench,
        resume=False,
        terminology={"de": {"Invoice": "Faktura"}},
    )
    await pipeline.run_pipeline(config)

    prompts = [prompt for prompt, _ in runner.calls]
    assert any('"Invoice": {"de": "Faktura", "fr": "Facture"}' in p for p in prompts)
    # PO lookup fills the empty file entry
    assert any('"Cancel": {"de": "Abbrechen"}' in p for p in prompts)
    assert not any("Sichern" in p for p in prompts)
    # Neither terminology, PO lookups nor model output reach the glossary files
    for app, path in paths.items():
        assert json.loads(path.read_text()) == files[app]


async def _extract(config: TranslatorConfig, runner: BatchRecordingRunner) -> None:
    all_apps = resolve_app_order(discover_bench(config.bench_path), config.app_priority)
    apps = [a for a in all_apps if a.name in config.apps] if config.apps else all_apps
    files = {app.name: load_glossary_file(config.get_glossary_dir(), app.name) for app in all_apps}
    pot_entries = {app.name: read_pot_entries(app.pot_path) for app in apps}
    await pipeline.extract_new_terms(config, apps, files, runner, pot_entries)


async def test_translate_does_not_extract_terms(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = BatchRecordingRunner(_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, resume=False))

    assert runner.calls
    assert all(schema is not None for _, schema in runner.calls)
    assert not (tmp_bench / "glossaries" / "sample_app.json").exists()
    assert not (tmp_bench / "glossary_extracted.json").exists()


async def test_extraction_adds_new_terms_to_app_file_unless_in_chain(tmp_bench: Path) -> None:
    _add_app(tmp_bench, "frappe", ["Invoice"])
    frappe_file = {"Invoice": {"de": {"text": "Rechnung", "source": "manual"}}}
    frappe_path = _write_glossary(tmp_bench, "frappe", frappe_file)
    runner = BatchRecordingRunner(_response)

    config = TranslatorConfig(bench_path=tmp_bench, apps=["sample_app"])
    await _extract(config, runner)

    app_path = tmp_bench / "glossaries" / "sample_app.json"
    assert app_path.read_text() == '{\n  "Cancel": {},\n  "Hello": {}\n}\n'
    assert json.loads(frappe_path.read_text()) == frappe_file
    extracted = json.loads((tmp_bench / "glossary_extracted.json").read_text())
    assert extracted == {
        "glossary_dir": str(app_path.parent.resolve()),
        "apps": {"sample_app": ["Cancel", "Hello World", "Invoice {0} has been submitted", "Save"]},
    }

    # Second run: nothing left to extract
    runner.calls.clear()
    await _extract(config, runner)
    assert not runner.calls


def test_migrates_bench_wide_glossary_to_app_files(tmp_bench: Path) -> None:
    _add_app(tmp_bench, "frappe", ["Sales Invoice"])
    old_glossary = {"invoice": {"de": "Rechnung"}, "Hello": {"de": "Hallo"}, "Unknown Term": {}}
    (tmp_bench / "glossary.json").write_text(json.dumps(old_glossary))
    (tmp_bench / "glossary_extracted.json").write_text(json.dumps(["Cancel", "Sales Invoice", "Gone"]))
    glossary_dir = tmp_bench / "glossaries"
    _write_glossary(tmp_bench, "sample_app", {"Hello": {"de": {"text": "Servus", "source": "manual"}}})

    priority = ["frappe", "erpnext"]
    apps = resolve_app_order(discover_bench(tmp_bench), priority)
    migrate_bench_glossary(tmp_bench, glossary_dir, apps, priority)

    # First app in order whose POT mentions the term; unmatched terms go to the first app
    assert json.loads((glossary_dir / "frappe.json").read_text()) == {"invoice": {}, "Unknown Term": {}}
    assert json.loads((glossary_dir / "sample_app.json").read_text()) == {
        "Hello": {"de": {"text": "Servus", "source": "manual"}}
    }
    assert json.loads((tmp_bench / "glossary_extracted.json").read_text()) == {
        "glossary_dir": str(glossary_dir.resolve()),
        "apps": {"frappe": ["Sales Invoice"], "sample_app": ["Cancel"]},
    }
    assert not (tmp_bench / "glossary.json").exists()
    assert (tmp_bench / "glossary.json.migrated").exists()

    # Running again is a no-op
    migrate_bench_glossary(tmp_bench, glossary_dir, apps, priority)
    assert json.loads((glossary_dir / "frappe.json").read_text()) == {"invoice": {}, "Unknown Term": {}}


def test_migration_gives_shared_term_to_each_app_outside_priority(tmp_bench: Path) -> None:
    _add_app(tmp_bench, "frappe", ["Save"])
    _add_app(tmp_bench, "app_a", ["Payment Entry"])
    _add_app(tmp_bench, "app_b", ["New Payment"])
    (tmp_bench / "glossary.json").write_text(json.dumps({"Payment": {}, "Save": {}}))
    glossary_dir = tmp_bench / "glossaries"

    priority = ["frappe"]
    apps = resolve_app_order(discover_bench(tmp_bench), priority)
    migrate_bench_glossary(tmp_bench, glossary_dir, apps, priority)

    # app_a and app_b are not in each other's chain, so both need the term
    assert json.loads((glossary_dir / "app_a.json").read_text()) == {"Payment": {}}
    assert json.loads((glossary_dir / "app_b.json").read_text()) == {"Payment": {}}
    # A priority app is in every chain, so it is enough there
    assert json.loads((glossary_dir / "frappe.json").read_text()) == {"Save": {}}


async def test_changed_glossary_dir_extracts_terms_again(tmp_bench: Path) -> None:
    runner = BatchRecordingRunner(_response)
    old_dir, new_dir = tmp_bench / "old_glossaries", tmp_bench / "new_glossaries"

    await _extract(TranslatorConfig(bench_path=tmp_bench, glossary_dir=old_dir), runner)
    await _extract(TranslatorConfig(bench_path=tmp_bench, glossary_dir=new_dir), runner)

    # The extraction cache belongs to old_dir, so the new folder gets its own terms
    assert json.loads((new_dir / "sample_app.json").read_text()) == json.loads(
        (old_dir / "sample_app.json").read_text()
    )


async def test_logs_stats_line(
    tmp_bench: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = BatchRecordingRunner(_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    with caplog.at_level(logging.INFO):
        await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, model="haiku", resume=False))

    stats = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Stats:")]
    assert len(stats) == 1
    assert stats[0].startswith("Stats: model=haiku, mode=fill-missing, concurrency=5, batch_size=50, timeout=120s,")
    assert stats[0].endswith("0 failed, 0 rate limit backoffs")


async def test_fix_glossary_corrects_only_curated_conflicts(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    glossary = {
        "Invoice": {"de": {"text": "Faktura", "source": "manual"}},
        "Cancel": {"de": {"text": "Abbrechen", "source": "manual"}},
    }
    _write_glossary(tmp_bench, "sample_app", glossary)
    runner = BatchRecordingRunner(_default_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, mode="fix-glossary", resume=False))

    # "Cancel" already uses the glossary term, and the fr PO file has no translations
    [(prompt, schema)] = runner.calls
    assert 'Existing translations: {"de": "Rechnung {0} wurde eingereicht"}' in prompt
    assert "Replace that term with its glossary translation" in prompt
    assert "### String 2" not in prompt
    assert json.loads(schema)["properties"]["translations"]["items"]["required"] == ["msgid", "msgctxt", "de"]
    de_po = read_po_translations(tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po")
    assert de_po[("Invoice {0} has been submitted", None)] == "Invoice {0} has been submitted::de"
    assert de_po[("Cancel", None)] == "Abbrechen"


async def test_fix_glossary_keeps_translation_the_model_returns_unchanged(
    tmp_bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_glossary(tmp_bench, "sample_app", {"Invoice": {"de": {"text": "Faktura", "source": "manual"}}})
    unchanged = {"msgid": "Invoice {0} has been submitted", "msgctxt": None, "de": "Rechnung {0} wurde eingereicht"}
    runner = BatchRecordingRunner(lambda _prompt, _schema: json.dumps({"translations": [unchanged]}))
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)
    de_path = tmp_bench / "apps" / "sample_app" / "sample_app" / "locale" / "de.po"
    before = de_path.read_text()

    summary = await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, mode="fix-glossary", resume=False))

    assert len(runner.calls) == 1
    assert summary.app_results["sample_app"].translated == 0
    assert de_path.read_text() == before


async def test_review_existing_sends_all_existing_translations(
    tmp_bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BatchRecordingRunner(_default_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, mode="review-existing", resume=False))

    # Both de translations are reviewed without a glossary conflict; fr has none
    [(prompt, _schema)] = runner.calls
    assert 'Existing translations: {"de": "Rechnung {0} wurde eingereicht"}' in prompt
    assert 'Existing translations: {"de": "Abbrechen"}' in prompt
    assert "Return an existing translation unchanged if it is correct" in prompt
    assert '"Save"' not in prompt


async def test_fix_glossary_ignores_term_translations_from_po_files(
    tmp_bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # frappe translates the term "Invoice" as "Faktura", but no glossary file has a translation
    _add_app(tmp_bench, "frappe", ["Invoice"])
    (tmp_bench / "apps" / "frappe" / "frappe" / "locale" / "de.po").write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=utf-8\\n"\n\nmsgid "Invoice"\nmsgstr "Faktura"\n'
    )
    _write_glossary(tmp_bench, "sample_app", {"Invoice": {}})
    runner = BatchRecordingRunner(_default_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    await pipeline.run_pipeline(TranslatorConfig(bench_path=tmp_bench, mode="fix-glossary", resume=False))

    assert not runner.calls
