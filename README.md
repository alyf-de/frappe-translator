# frappe-translator

AI-powered translation CLI for Frappe apps using the Claude CLI.

Massively parallelizes translation by assembling rich context (source code snippets, term glossaries, comments) and translating into all target languages simultaneously.

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

### Skip glossary extraction (single-pass, faster)

```bash
frappe-translator translate /path/to/bench --skip-glossary
```

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

1. **Pass 1 (term extraction):** Batches all translatable strings and sends them to Claude to extract key domain-specific terms (e.g., "Invoice", "Purchase Order", "DocType"). Existing translations for these terms are looked up across all PO files to build a glossary. This pass is optional (`--skip-glossary`) and cached across runs.

2. **Pass 2 (translation):** Each string is sent to Claude with full context:
   - Source code snippets (5 lines around each reference)
   - Term glossary with existing translations
   - Extractor comments from the POT file
   - Per-language style instructions (formality, address form)

   The LLM translates into all target languages at once, producing a JSON response like `{"de": "...", "fr": "..."}`.

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

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run ty check src/
uv run pytest
```

## License

MIT
