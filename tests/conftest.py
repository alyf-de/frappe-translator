"""Shared test fixtures for frappe-translator."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest


@pytest.fixture
def tmp_bench(tmp_path: Path) -> Path:
    """Create a minimal bench directory with sample app structure."""
    app_name = "sample_app"
    locale_dir = tmp_path / "apps" / app_name / app_name / "locale"
    locale_dir.mkdir(parents=True)

    # Create a minimal POT file
    pot_content = textwrap.dedent("""\
        # Translation template for sample_app.
        msgid ""
        msgstr ""
        "Content-Type: text/plain; charset=utf-8\\n"
        "Content-Transfer-Encoding: 8bit\\n"

        #. Description of a field
        #: sample_app/module/file.py:42
        msgid "Invoice {0} has been submitted"
        msgstr ""

        #: sample_app/module/file.py:55
        msgid "Cancel"
        msgstr ""

        #: sample_app/module/file.py:60
        msgid "Save"
        msgstr ""

        #: sample_app/module/file.py:70
        msgid "Hello World"
        msgstr ""
    """)
    (locale_dir / "main.pot").write_text(pot_content)

    # Create German PO file with some translations
    de_content = textwrap.dedent("""\
        # German translations for sample_app.
        msgid ""
        msgstr ""
        "Content-Type: text/plain; charset=utf-8\\n"
        "Content-Transfer-Encoding: 8bit\\n"

        #: sample_app/module/file.py:42
        msgid "Invoice {0} has been submitted"
        msgstr "Rechnung {0} wurde eingereicht"

        #: sample_app/module/file.py:55
        msgid "Cancel"
        msgstr "Abbrechen"

        #: sample_app/module/file.py:60
        msgid "Save"
        msgstr ""

        #: sample_app/module/file.py:70
        msgid "Hello World"
        msgstr ""
    """)
    (locale_dir / "de.po").write_text(de_content)

    # Create French PO file (empty translations)
    fr_content = textwrap.dedent("""\
        # French translations for sample_app.
        msgid ""
        msgstr ""
        "Content-Type: text/plain; charset=utf-8\\n"
        "Content-Transfer-Encoding: 8bit\\n"

        #: sample_app/module/file.py:42
        msgid "Invoice {0} has been submitted"
        msgstr ""

        #: sample_app/module/file.py:55
        msgid "Cancel"
        msgstr ""

        #: sample_app/module/file.py:60
        msgid "Save"
        msgstr ""

        #: sample_app/module/file.py:70
        msgid "Hello World"
        msgstr ""
    """)
    (locale_dir / "fr.po").write_text(fr_content)

    # Create a sample source file for snippet extraction
    src_dir = tmp_path / "apps" / app_name / app_name / "module"
    src_dir.mkdir(parents=True, exist_ok=True)
    source_content = textwrap.dedent("""\
        import frappe

        def validate_invoice(doc):
            \"\"\"Validate an invoice before submission.\"\"\"
            if not doc.items:
                frappe.throw("No items found")

            for item in doc.items:
                if item.qty <= 0:
                    frappe.throw("Invalid quantity")

            # Check submission status
            if doc.docstatus == 1:
                frappe.throw(_("Invoice {0} has been submitted").format(doc.name))

            # Handle cancellation
            if doc.docstatus == 2:
                frappe.throw(_("Cancel"))
    """)
    (src_dir / "file.py").write_text(source_content)

    return tmp_path


@pytest.fixture
def tmp_bench_uneven(tmp_path: Path) -> Path:
    """Create a bench with two apps that have uneven locale coverage.

    - ``app_full``: has ``de`` and ``fr`` PO files.
    - ``app_de_only``: has only a ``de`` PO file.
    """

    def _write_po(path: Path, msgid: str) -> None:
        content = textwrap.dedent(f"""\
            msgid ""
            msgstr ""
            "Content-Type: text/plain; charset=utf-8\\n"
            "Content-Transfer-Encoding: 8bit\\n"

            msgid "{msgid}"
            msgstr ""
        """)
        path.write_text(content)

    full_locale = tmp_path / "apps" / "app_full" / "app_full" / "locale"
    full_locale.mkdir(parents=True)
    _write_po(full_locale / "main.pot", "Hello")
    _write_po(full_locale / "de.po", "Hello")
    _write_po(full_locale / "fr.po", "Hello")

    de_only_locale = tmp_path / "apps" / "app_de_only" / "app_de_only" / "locale"
    de_only_locale.mkdir(parents=True)
    _write_po(de_only_locale / "main.pot", "Goodbye")
    _write_po(de_only_locale / "de.po", "Goodbye")

    return tmp_path
