"""End-to-end tests for the glossary command with a fake ClaudeRunner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from frappe_translator import pipeline
from frappe_translator.cli import main
from frappe_translator.config import TranslatorConfig
from frappe_translator.glossary import CORE_TERMS, load_glossary_file
from frappe_translator.models import TermGlossary
from tests.test_pipeline import BatchRecordingRunner, _add_app, _write_glossary

if TYPE_CHECKING:
    from pathlib import Path


def _response(prompt: str, schema: str | None) -> str:
    if schema is None:  # Pass 1 term extraction
        return json.dumps({"terms": []})
    term_props = json.loads(schema)["properties"]["terms"]["properties"]
    terms = {term: {lang: f"{term}::{lang}" for lang in spec["required"]} for term, spec in term_props.items()}
    # Bad items the model might return; they must be skipped
    terms["Unknown"] = {"de": "Unbekannt"}
    first = next(iter(terms))
    terms[first]["xx"] = "?"
    return json.dumps({"terms": terms})


@pytest.fixture(autouse=True)
def _no_min_term_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture POT has only a few strings
    monkeypatch.setattr(pipeline, "MIN_TERM_STRINGS", 0)


async def _run(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch, tier: str, seed: bool = False) -> BatchRecordingRunner:
    runner = BatchRecordingRunner(_response)
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)
    config = TranslatorConfig(bench_path=tmp_bench, model=tier)
    await pipeline.run_glossary(config, tier, seed)
    return runner


def _read(tmp_bench: Path, app: str) -> dict:
    return json.loads((tmp_bench / "glossaries" / f"{app}.json").read_text())


async def test_sonnet_fills_only_empty_pairs(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manual = {"text": "Stornieren", "source": "manual"}
    opus = {"text": "Rechnung", "source": "opus"}
    _write_glossary(tmp_bench, "sample_app", {"Cancel": {"de": manual}, "Invoice": {"de": opus}, "Hello": {}})

    runner = await _run(tmp_bench, monkeypatch, "sonnet")

    assert _read(tmp_bench, "sample_app") == {
        "Cancel": {"de": manual, "fr": {"text": "Cancel::fr", "source": "sonnet"}},
        "Invoice": {"de": opus, "fr": {"text": "Invoice::fr", "source": "sonnet"}},
        "Hello": {"de": {"text": "Hello::de", "source": "sonnet"}, "fr": {"text": "Hello::fr", "source": "sonnet"}},
    }
    prompt = next(prompt for prompt, schema in runner.calls if schema)
    assert 'term: "Invoice"\nlanguages: fr\nused in:\n- "Invoice {0} has been submitted"' in prompt

    # Second run: nothing left to translate
    runner = await _run(tmp_bench, monkeypatch, "sonnet")
    assert all(schema is None for _, schema in runner.calls)


async def test_opus_upgrades_sonnet_but_not_manual(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manual = {"text": "Abbrechen", "source": "manual"}
    _write_glossary(tmp_bench, "sample_app", {"Cancel": {"de": manual, "fr": {"text": "Annuler?", "source": "sonnet"}}})

    await _run(tmp_bench, monkeypatch, "opus")

    assert _read(tmp_bench, "sample_app") == {"Cancel": {"de": manual, "fr": {"text": "Cancel::fr", "source": "opus"}}}


async def test_seed_adds_doctype_names_and_core_terms(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_app(tmp_bench, "frappe", ["Report"])
    doctype_dir = tmp_bench / "apps" / "sample_app" / "sample_app" / "module" / "doctype"
    for name in ["Sample Invoice", "Report"]:
        folder = doctype_dir / name.lower().replace(" ", "_")
        folder.mkdir(parents=True)
        (folder / f"{folder.name}.json").write_text(json.dumps({"doctype": "DocType", "name": name}))
    (doctype_dir / "sample_invoice" / "test_records.json").write_text("[]")

    await _run(tmp_bench, monkeypatch, "opus", seed=True)

    # frappe has no PO files, so its terms stay without translations
    assert _read(tmp_bench, "frappe") == {term: {} for term in CORE_TERMS}
    # "Report" is already in frappe's file, which is in sample_app's chain
    assert _read(tmp_bench, "sample_app") == {
        "Sample Invoice": {
            "de": {"text": "Sample Invoice::de", "source": "opus"},
            "fr": {"text": "Sample Invoice::fr", "source": "opus"},
        }
    }


async def test_extract_model_runs_pass_1(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runners: dict[str | None, BatchRecordingRunner] = {}

    def make_runner(model: str | None, **_: object) -> BatchRecordingRunner:
        return runners.setdefault(model, BatchRecordingRunner(_response))

    monkeypatch.setattr(pipeline, "ClaudeRunner", make_runner)
    config = TranslatorConfig(bench_path=tmp_bench, model="opus", extract_model="haiku")
    await pipeline.run_glossary(config, "opus")

    assert runners["haiku"].calls
    assert all(schema is None for _, schema in runners["haiku"].calls)
    assert all(schema is not None for _, schema in runners["opus"].calls)


def test_cli_rejects_other_models(tmp_bench: Path) -> None:
    result = CliRunner().invoke(main, ["glossary", str(tmp_bench), "--model", "haiku"])
    assert result.exit_code == 2
    assert "sonnet or opus" in result.output


async def test_failed_batches_are_counted(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_glossary(tmp_bench, "sample_app", {"Hello": {}})
    runner = BatchRecordingRunner(lambda _prompt, schema: json.dumps({"terms": []}) if schema is None else "not json")
    monkeypatch.setattr(pipeline, "ClaudeRunner", lambda **_: runner)

    failed = await pipeline.run_glossary(TranslatorConfig(bench_path=tmp_bench, model="sonnet"), "sonnet")

    assert failed == 1
    assert _read(tmp_bench, "sample_app") == {"Hello": {}}


def test_load_glossary_file_rejects_plain_string_entry(tmp_path: Path) -> None:
    (tmp_path / "frappe.json").write_text(json.dumps({"Submit": {"de": "Buchen"}}))
    with pytest.raises(ValueError, match=r"frappe\.json.*'Submit'.*'de'"):
        load_glossary_file(tmp_path, "frappe")


def test_load_glossary_file_rejects_plain_string_term(tmp_path: Path) -> None:
    (tmp_path / "frappe.json").write_text(json.dumps({"Submit": "Buchen"}))
    with pytest.raises(ValueError, match=r"frappe\.json.*'Submit'"):
        load_glossary_file(tmp_path, "frappe")


async def test_rare_terms_are_removed_or_skipped(tmp_bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "MIN_TERM_STRINGS", 1)
    manual = {"de": {"text": "Auf Wiedersehen", "source": "manual"}}
    # The fixture POT has "Hello World", but no string with "Hell" or "Goodbye"
    _write_glossary(tmp_bench, "sample_app", {"hello": {}, "HELLO": {}, "Hell": {}, "Goodbye": manual})

    await _run(tmp_bench, monkeypatch, "opus")

    # "HELLO" is merged into "hello"; "Hell" is removed; "Goodbye" keeps its translation, without a new "fr"
    assert _read(tmp_bench, "sample_app") == {
        "hello": {"de": {"text": "hello::de", "source": "opus"}, "fr": {"text": "hello::fr", "source": "opus"}},
        "Goodbye": manual,
    }


def test_load_glossary_file_merges_case_duplicates(tmp_path: Path) -> None:
    manual = {"text": "Partei", "source": "manual"}
    opus = {"text": "Party", "source": "opus"}
    data = {"Party": {"de": opus, "fr": opus}, "party": {"de": manual, "it": opus}}
    (tmp_path / "frappe.json").write_text(json.dumps(data))

    assert load_glossary_file(tmp_path, "frappe") == {"Party": {"de": manual, "fr": opus, "it": opus}}


def test_load_glossary_file_drops_plurals_unless_manual(tmp_path: Path) -> None:
    manual = {"text": "Gutschriften", "source": "manual"}
    opus = {"text": "Rechnung", "source": "opus"}
    data = {
        "Invoices": {"de": {"text": "Rechnungen", "source": "opus"}},
        "invoice": {"de": opus},
        "Credit Notes": {"de": manual},
        "Credit Note": {},
        "Sales": {},
    }
    (tmp_path / "frappe.json").write_text(json.dumps(data))

    assert load_glossary_file(tmp_path, "frappe") == {
        "invoice": {"de": opus},
        "Credit Notes": {"de": manual},
        "Credit Note": {},
        "Sales": {},
    }


def test_glossary_term_matches_plural() -> None:
    glossary = TermGlossary(terms={"Invoice": {"de": "Rechnung"}})

    assert glossary.get_relevant_terms("Submit all invoices") == {"Invoice": {"de": "Rechnung"}}
