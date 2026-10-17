# frappe-translator

AI-powered translation CLI for Frappe apps using the Claude CLI.

Massively parallelizes translation by assembling rich context (source code snippets, term glossaries, comments) and translating into all target languages simultaneously.

With Claude Code Max, this can translate around 500 strings into 30 languages per 4-hour token limit. However, the 4-hour token limit is already exhausted after 1 hour. So we're effectively doing 4 translations per second.

## Requirements

- Python 3.10+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) with an active subscription (Pro/Max)
- A Frappe bench directory with apps containing `locale/main.pot` files

## Installation

### As a user

```bash
uv tool install https://github.com/alyf-de/frappe-translator.git
```

### As a contributor

```bash
git clone https://github.com/alyf-de/frappe-translator
cd frappe-translator
uv sync
uv tool install -e .
```

## Usage

### Translate missing strings

```bash
frappe-translator translate /path/to/bench --mode fill-missing
```

### Translate specific apps and languages

```bash
frappe-translator translate /path/to/bench \
  --app frappe --app erpnext \
  --language de --language fr \
  --model sonnet
```

### Exclude languages

Skip locales you do not want to translate, e.g. Crowdin's `eo` pseudo-language:

```bash
frappe-translator translate /path/to/bench --exclude-language eo
```

Or in the config file:

```toml
[run]
exclude_languages = ["eo"]
```

### Skip glossary extraction (single-pass, faster)

```bash
frappe-translator translate /path/to/bench --skip-glossary
```

### Build the glossary

Translate the terms in the glossary files with a stronger model:

```bash
frappe-translator glossary /path/to/bench --model sonnet
```

Add `--seed` to also add all DocType names and a short list of core Frappe terms (for example "Submit", "Opening", "Party") first. Use Opus for this one-time seed:

```bash
frappe-translator glossary /path/to/bench --model opus --seed
```

The seed runs Pass 1 on all strings. To extract the terms with a cheaper model, add `--extract-model haiku`. `--model` then only sets the model for the term translations.

The command runs Pass 1 first. `--app`, `--language` and `--exclude-language` work as for `translate`. See [Glossary](#glossary).

The command only runs the slower Sonnet and Opus models, so it sends 10 terms per call and waits up to 600 seconds. It ignores `batch_size` and `timeout` in the config file. To change these values, use `--batch-size` and `--timeout`.

### Check translation coverage

```bash
frappe-translator status /path/to/bench
```

### Dry run (see what would be translated)

```bash
frappe-translator translate /path/to/bench --dry-run
```

### Clear progress (start fresh)

```bash
frappe-translator clear-progress /path/to/bench
```

## How it works

### Two-pass pipeline

1. **Pass 1 (term extraction):** Batches the translatable strings of each app and sends them to Claude to extract key domain-specific terms (e.g., "Invoice", "Purchase Order", "DocType"). This pass is optional (`--skip-glossary`) and cached across runs in `<bench>/glossary_extracted.json`. If `glossary_dir` changes, the cache is not used and terms are extracted again. New terms are added to the app's glossary file without a translation. Pass 2 only reads the glossary and never adds terms.

2. **Pass 2 (translation):** Strings are batched (default 50 per call) and sent to Claude with full context:
   - Source code snippets (up to 3 per string, from diverse files)
   - Term glossary with existing translations
   - Extractor comments from the POT file
   - Per-language style instructions (formality, address form)

   The LLM translates each string into all target languages at once. Failed batches are retried individually.

### Glossary

Each app has an optional glossary file `<glossary_dir>/<app>.json`. The default `glossary_dir` is `<bench>/glossaries`. Change it with `glossary_dir` in `[general]` or with `--glossary-dir`. Use a git-versioned folder that all runs share, so that curated translations are reviewed and kept.

```json
{
  "Opening": {
    "de": {"source": "opus", "text": "Eröffnung"},
    "fi": {"source": "manual", "text": "Avaus"}
  },
  "Party": {}
}
```

`source` is `manual`, `opus` or `sonnet`. A term with `{}` was extracted, but has no curated translation yet.

The `glossary` command fills the term and language pairs of each app file. It sends the term, up to 3 source strings that contain it and the existing PO translations as hints. It writes the results with `source` set to the model tier (`sonnet` or `opus`). A run writes a pair only if it has no entry or a lower source: `manual` > `opus` > `sonnet`. So a Sonnet run fills gaps, an Opus run also replaces Sonnet entries, and no run changes `manual` entries. The file is saved after each batch, so an interrupted run continues where it stopped.

Glossary entries win over existing PO translations. Strings that were translated before keep their old term until they are translated again.

To translate app X, the tool merges the files of the apps in `[apps.order] priority` (if they are in the bench) and then the file of X. For each term and language, the later app wins. Files of other apps are not used. Then:

1. Existing translations from the PO files of all apps fill the term and language pairs that have no file entry. These lookups run on each run and are not written to the glossary files.
2. Entries from `[terminology.<lang>]` win over all other sources.

Older versions stored one bench-wide `<bench>/glossary.json`. The first run moves its terms into the app files (without translations) and renames it to `glossary.json.migrated`.

### Key design decisions

- **All languages at once:** Context assembly is 80% of the work. Once rich context is assembled, translating into multiple languages simultaneously is efficient and produces more consistent results.
- **Dependency ordering:** Apps are processed in order (frappe, erpnext, hrms, ...) so earlier apps' translations inform downstream glossaries.
- **Incremental writes:** Translations are written to PO files every 50 entries. Re-runs skip already-translated entries.
- **Per-language resume:** If translation fails for one language (e.g., placeholder mismatch), only that language is retried on the next run.

## Run modes

| Mode | Description |
|------|-------------|
| `fill-missing` | Only translate entries with empty `msgstr` in any target locale (default) |
| `review-existing` | Re-translate entries that already have translations |
| `full-correct` | Re-translate all entries regardless of existing translations |

## Configuration

Create a `frappe-translator.toml` file:

```toml
[general]
concurrency = 5
batch_size = 50
timeout = 120
model = "sonnet"
extract_model = "haiku"  # Pass 1 term extraction; default: model
glossary_dir = "/path/to/glossaries"  # default: <bench>/glossaries

[style.default]
formality = "formal"
notes = "Use formal address. Preserve technical terms commonly used in ERP software."

[style.de]
formality = "formal"
address = "Sie"
notes = "Use 'Sie' form. German compound nouns preferred over English loanwords."

[style.fr]
formality = "formal"
address = "vous"

[terminology.de]
"Workspace" = "Arbeitsbereich"

[apps.order]
priority = ["frappe", "erpnext", "hrms"]
```

Pass it with `--config frappe-translator.toml`.

Entries in `[terminology.<lang>]` win over all other glossary sources for that term and language. They are used only for the current run and are not written to the glossary files.

## Scheduled runs

To run the tool every night on a server and open pull requests automatically, see [docs/scheduled-runs.md](docs/scheduled-runs.md).

For a quality comparison of Haiku and Sonnet as translation models, see [docs/model-comparison.md](docs/model-comparison.md).

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run ty check src/
uv run pytest
```

## License

MIT
