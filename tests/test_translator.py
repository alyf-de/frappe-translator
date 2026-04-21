"""Tests for Pass 2 translation orchestration, especially language grouping."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import pytest

from frappe_translator.models import AssembledContext, TranslationEntry, TranslationResult
from frappe_translator.translator import _group_contexts_by_languages, translate_entries

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(msgid: str, target_languages: list[str], *, msgctxt: str | None = None) -> AssembledContext:
    entry = TranslationEntry(msgid=msgid, msgctxt=msgctxt)
    prompt = f"prompt for {msgid} -> {target_languages}"
    return AssembledContext(entry=entry, prompt=prompt, target_languages=target_languages)


class RecordingRunner:
    """Fake ClaudeRunner that records prompts/schemas and returns synthetic translations."""

    def __init__(
        self,
        response_fn=None,
        *,
        raise_on_indices: set[int] | None = None,
        return_invalid_on_indices: set[int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.response_fn = response_fn or _default_response
        self.raise_on_indices = raise_on_indices or set()
        self.return_invalid_on_indices = return_invalid_on_indices or set()

    async def run(self, prompt: str, json_schema: str | None = None) -> str:
        idx = len(self.calls)
        self.calls.append((prompt, json_schema))
        await asyncio.sleep(0)
        if idx in self.raise_on_indices:
            raise RuntimeError(f"simulated failure #{idx}")
        if idx in self.return_invalid_on_indices:
            return "not valid json"
        return self.response_fn(prompt, json_schema)


_NON_LANG_REQUIRED = {"msgid", "msgctxt"}


def _default_response(prompt: str, schema: str | None) -> str:
    """Synthesize a response matching the schema and prompt.

    Extracts required languages from the schema and msgids from the prompt,
    returning translations of the form "{msgid}::{lang}" for each.
    """
    schema_dict = json.loads(schema or "{}")
    required = schema_dict["properties"]["translations"]["items"]["required"]
    langs = [field for field in required if field not in _NON_LANG_REQUIRED]
    msgids = re.findall(r'msgid:\s*"([^"]+)"', prompt)
    translations = [
        {"msgid": msgid, "msgctxt": None, **{lang: f"{msgid}::{lang}" for lang in langs}} for msgid in msgids
    ]
    return json.dumps({"translations": translations})


class FakePOWriter:
    def __init__(self) -> None:
        self.buffered: list[TranslationResult] = []

    def buffer_translation(self, result: TranslationResult) -> None:
        self.buffered.append(result)

    async def flush_all(self) -> None:
        return None


class FakeProgressTracker:
    def __init__(self) -> None:
        self.done_calls: list[tuple[str, str, str | None, list[str]]] = []
        self.error_calls: list[tuple[str, str, str | None, str, str]] = []

    def mark_languages_done(self, app: str, msgid: str, msgctxt: str | None, languages: list[str]) -> None:
        self.done_calls.append((app, msgid, msgctxt, list(languages)))

    def mark_language_error(self, app: str, msgid: str, msgctxt: str | None, language: str, error: str) -> None:
        self.error_calls.append((app, msgid, msgctxt, language, error))

    def save(self) -> None:
        return None


def _schema_langs(schema: str | None) -> tuple[str, ...]:
    """Extract the sorted tuple of required languages from a translation schema."""
    data = json.loads(schema or "{}")
    required = data["properties"]["translations"]["items"]["required"]
    return tuple(sorted(field for field in required if field not in _NON_LANG_REQUIRED))


def _prompt_target_languages(prompt: str) -> list[str]:
    """Pull the comma-separated language list from the `## Target Languages` section."""
    match = re.search(r"## Target Languages\n([^\n]+)", prompt)
    assert match, f"Target Languages section missing in prompt:\n{prompt}"
    return [lang.strip() for lang in match.group(1).split(",")]


class TestGroupContextsByLanguages:
    def test_groups_identical_sets_together(self) -> None:
        ctx_a = _ctx("A", ["de", "fr"])
        ctx_b = _ctx("B", ["de", "fr"])
        ctx_c = _ctx("C", ["de"])

        groups = _group_contexts_by_languages([ctx_a, ctx_b, ctx_c], ["de", "fr", "es"])

        assert set(groups.keys()) == {("de", "fr"), ("de",)}
        assert groups[("de", "fr")] == [ctx_a, ctx_b]
        assert groups[("de",)] == [ctx_c]

    def test_order_independent_key(self) -> None:
        ctx_a = _ctx("A", ["fr", "de"])
        ctx_b = _ctx("B", ["de", "fr"])

        groups = _group_contexts_by_languages([ctx_a, ctx_b], ["de", "fr"])

        assert list(groups.keys()) == [("de", "fr")]
        assert groups[("de", "fr")] == [ctx_a, ctx_b]

    def test_falls_back_to_default_when_empty(self) -> None:
        ctx = _ctx("A", [])

        groups = _group_contexts_by_languages([ctx], ["de", "fr"])

        assert list(groups.keys()) == [("de", "fr")]
        assert groups[("de", "fr")] == [ctx]

    def test_preserves_insertion_order_within_group(self) -> None:
        ctx_a = _ctx("A", ["de"])
        ctx_b = _ctx("B", ["fr"])
        ctx_c = _ctx("C", ["de"])
        ctx_d = _ctx("D", ["fr"])

        groups = _group_contexts_by_languages([ctx_a, ctx_b, ctx_c, ctx_d], ["de", "fr"])

        assert groups[("de",)] == [ctx_a, ctx_c]
        assert groups[("fr",)] == [ctx_b, ctx_d]


@pytest.mark.asyncio
class TestTranslateEntries:
    async def test_single_language_set_produces_one_batch(self, tmp_path: Path) -> None:
        contexts = [_ctx("Save", ["de", "fr"]), _ctx("Cancel", ["de", "fr"])]
        runner = RecordingRunner()

        await translate_entries(
            contexts=contexts,
            runner=runner,
            po_writer=FakePOWriter(),
            progress=FakeProgressTracker(),
            app_name="sample_app",
            target_languages=["de", "fr"],
            batch_size=50,
        )

        assert len(runner.calls) == 1
        _prompt, schema = runner.calls[0]
        assert _schema_langs(schema) == ("de", "fr")

    async def test_mixed_missing_languages_produce_separate_batches(self, tmp_path: Path) -> None:
        # Three entries with three distinct pending-language sets
        ctx_de_fr = _ctx("Hello", ["de", "fr"])
        ctx_de = _ctx("World", ["de"])
        ctx_fr = _ctx("Goodbye", ["fr"])

        runner = RecordingRunner()

        await translate_entries(
            contexts=[ctx_de_fr, ctx_de, ctx_fr],
            runner=runner,
            po_writer=FakePOWriter(),
            progress=FakeProgressTracker(),
            app_name="sample_app",
            target_languages=["de", "fr", "es"],
            batch_size=50,
        )

        assert len(runner.calls) == 3

        schemas_seen = {_schema_langs(schema) for _, schema in runner.calls}
        assert schemas_seen == {("de", "fr"), ("de",), ("fr",)}

    async def test_batch_prompt_only_lists_group_languages(self, tmp_path: Path) -> None:
        ctx_de = _ctx("Hello", ["de"])
        ctx_fr = _ctx("Goodbye", ["fr"])

        runner = RecordingRunner()

        await translate_entries(
            contexts=[ctx_de, ctx_fr],
            runner=runner,
            po_writer=FakePOWriter(),
            progress=FakeProgressTracker(),
            app_name="sample_app",
            target_languages=["de", "fr", "es"],
            batch_size=50,
        )

        prompts_by_langs = {_schema_langs(schema): prompt for prompt, schema in runner.calls}
        assert _prompt_target_languages(prompts_by_langs[("de",)]) == ["de"]
        assert _prompt_target_languages(prompts_by_langs[("fr",)]) == ["fr"]

    async def test_completed_locale_is_not_requested_or_overwritten(self, tmp_path: Path) -> None:
        # Simulates fill-missing mode: "de" already complete for this entry; only "fr" pending.
        ctx = _ctx("Save", ["fr"])
        po_writer = FakePOWriter()
        progress = FakeProgressTracker()
        runner = RecordingRunner()

        await translate_entries(
            contexts=[ctx],
            runner=runner,
            po_writer=po_writer,
            progress=progress,
            app_name="sample_app",
            target_languages=["de", "fr"],
            batch_size=50,
        )

        assert len(runner.calls) == 1
        _prompt, schema = runner.calls[0]
        assert _schema_langs(schema) == ("fr",)
        assert "de" not in json.loads(schema)["properties"]["translations"]["items"]["properties"]

        assert len(po_writer.buffered) == 1
        assert set(po_writer.buffered[0].translations.keys()) == {"fr"}
        assert progress.done_calls == [("sample_app", "Save", None, ["fr"])]

    async def test_retry_uses_entry_specific_schema(self, tmp_path: Path) -> None:
        ctx_de = _ctx("Hello", ["de"])
        ctx_fr = _ctx("World", ["fr"])

        # Batch 0 and 1 return invalid JSON -> both entries go to retry.
        runner = RecordingRunner(return_invalid_on_indices={0, 1})

        await translate_entries(
            contexts=[ctx_de, ctx_fr],
            runner=runner,
            po_writer=FakePOWriter(),
            progress=FakeProgressTracker(),
            app_name="sample_app",
            target_languages=["de", "fr"],
            batch_size=50,
        )

        # 2 batches + 2 retries = 4 calls total
        assert len(runner.calls) == 4

        # The last two calls are the retries; each should use the entry's own lang set
        retry_calls = runner.calls[2:]
        retry_schema_langs = {_schema_langs(schema) for _, schema in retry_calls}
        assert retry_schema_langs == {("de",), ("fr",)}

    async def test_empty_contexts_noop(self, tmp_path: Path) -> None:
        runner = RecordingRunner()

        results = await translate_entries(
            contexts=[],
            runner=runner,
            po_writer=FakePOWriter(),
            progress=FakeProgressTracker(),
            app_name="sample_app",
            target_languages=["de", "fr"],
            batch_size=50,
        )

        assert results == []
        assert runner.calls == []
