"""The main README keeps up with the code: settings, test files, fixture paths, the two import ways."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

PROJECT = Path(__file__).resolve().parent.parent
README = (PROJECT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (PROJECT / ".env.example").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """A ``## heading`` section (exact heading line), up to the next ``## `` heading."""
    return README.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def test_every_setting_is_documented():
    rows = [line for line in _section("## Settings").splitlines() if line.startswith("| `")]
    for name in Settings.model_fields:
        assert any(f"`{name.upper()}`" in row.split("|")[1] for row in rows), name.upper()  # first column
        assert name.upper() in ENV_EXAMPLE, name.upper()


def test_every_test_file_is_in_the_tests_table():
    tests_section = _section("## Tests")
    for path in sorted((PROJECT / "tests").glob("test_*.py")):
        listed = f"`tests/{path.name}`" in tests_section
        listed = listed or (path.name.startswith("test_admin_") and "`tests/test_admin_*.py`" in tests_section)
        assert listed, path.name


def test_every_fixture_path_in_the_readme_exists():
    paths = set(re.findall(r"fixtures/packs/[\w./-]+\.json", README))
    assert len(paths) >= 3
    for path in paths:
        assert (PROJECT / path).is_file(), path


def test_both_import_ways_are_compared():
    section = README.split("### Importing a pack: command line or admin upload", 1)[1].split("\n### ", 1)[0]
    header = next(line for line in section.splitlines() if line.startswith("| |"))
    assert "**Command line**" in header and "`.json`" in header and "`.zip`" in header
    for row in ("Catalog", "`storefront/`", "`media/`", "Existing store", "Limits", "Record"):
        assert any(line.startswith(f"| {row}") for line in section.splitlines()), row
    assert "python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json" in section
    assert "python -m zipfile -c /tmp/solares.zip solares/" in section
