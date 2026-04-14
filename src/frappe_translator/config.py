"""Configuration loading for frappe-translator."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # ty: ignore[unresolved-import]


@dataclass
class StyleConfig:
    """Style instructions for a specific language."""

    formality: str = "formal"
    address: str | None = None
    direction: str = "ltr"
    notes: str = ""


@dataclass
class TranslatorConfig:
    """Main configuration for the translator."""

    bench_path: Path = field(default_factory=lambda: Path("."))
    concurrency: int = 5
    batch_size: int = 50
    timeout: int = 120
    model: str | None = None
    mode: str = "fill-missing"
    resume: bool = True
    skip_glossary: bool = False
    apps: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    app_priority: list[str] = field(default_factory=lambda: ["frappe", "erpnext", "hrms"])
    style_default: StyleConfig = field(default_factory=StyleConfig)
    style_per_language: dict[str, StyleConfig] = field(default_factory=dict)
    terminology: dict[str, dict[str, str]] = field(default_factory=dict)
    checkpoint_interval: int = 50
    max_retries: int = 2

    def get_style(self, language: str) -> StyleConfig:
        """Get style config for a language, falling back to default."""
        return self.style_per_language.get(language, self.style_default)


def load_config(path: Path | None) -> TranslatorConfig:
    """Load configuration from a TOML file. Returns defaults if path is None."""
    if path is None:
        return TranslatorConfig()

    with open(path, "rb") as f:
        data = tomllib.load(f)

    config = TranslatorConfig()

    general = data.get("general", {})
    config.concurrency = general.get("concurrency", config.concurrency)
    config.batch_size = general.get("batch_size", config.batch_size)
    config.timeout = general.get("timeout", config.timeout)
    config.model = general.get("model", config.model)
    config.checkpoint_interval = general.get("checkpoint_interval", config.checkpoint_interval)
    config.max_retries = general.get("max_retries", config.max_retries)

    run = data.get("run", {})
    config.mode = run.get("mode", config.mode)
    config.resume = run.get("resume", config.resume)
    config.skip_glossary = run.get("skip_glossary", config.skip_glossary)
    config.apps = run.get("apps", config.apps)
    config.languages = run.get("languages", config.languages)

    apps_section = data.get("apps", {})
    order = apps_section.get("order", {})
    config.app_priority = order.get("priority", config.app_priority)

    style_default_data = data.get("style", {}).get("default", {})
    if style_default_data:
        config.style_default = _parse_style(style_default_data)

    for key, value in data.get("style", {}).items():
        if key != "default" and isinstance(value, dict):
            config.style_per_language[key] = _parse_style(value)

    for lang, terms in data.get("terminology", {}).items():
        if isinstance(terms, dict):
            config.terminology[lang] = dict(terms)

    return config


def _parse_style(data: dict) -> StyleConfig:
    """Parse a style configuration section."""
    return StyleConfig(
        formality=data.get("formality", "formal"),
        address=data.get("address"),
        direction=data.get("direction", "ltr"),
        notes=data.get("notes", ""),
    )
