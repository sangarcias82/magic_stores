"""The store pack documentation is checked against the code, so docs and behavior can't drift:

- the example commands in fixtures/packs/README.md print exactly the output shown there;
- the quoted error messages are what the validator says;
- the contract tables list exactly the fields the validator accepts, with the right ones
  marked required, and the limits they quote are the code's constants;
- the file table lists the fixtures, and the status list the import record statuses.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import MAX_AMOUNT_CENTS, PackImportStatus, PropertyType
from app.routers.admin_packs import MAX_PACK_MB
from app.services import bank_transfer
from app.services import store_pack_validation as contract
from app.services.category_admin import DESCRIPTION_MAX as CATEGORY_DESCRIPTION_MAX
from app.services.category_admin import KEY_MAX, OPTION_MAX_LEN, OPTIONS_MAX, SORT_MAX
from app.services.product_admin import DESCRIPTION_MAX as PRODUCT_DESCRIPTION_MAX
from app.services.product_admin import PRICE_MAX_CENTS, SKU_MAX, TEXT_VALUE_MAX, URL_MAX
from app.services.stock import STOCK_MAX
from app.services.store_admin import NAME_MAX, SLUG_MAX
from tests.markdown_doc import code_blocks as _code_blocks
from tests.markdown_doc import section as _section
from tests.markdown_doc import table_rows as _table_rows

PROJECT = Path(__file__).resolve().parent.parent
PACKS_DIR = PROJECT / "fixtures" / "packs"
PACKS_README = (PACKS_DIR / "README.md").read_text(encoding="utf-8")
MAIN_README = (PROJECT / "README.md").read_text(encoding="utf-8")
CLOTHING = PACKS_DIR / f"clothing-demo{contract.PACK_FILE_SUFFIX}"


# ------------------------------------------------------------------------ markdown helpers


def _documented_fields(heading: str) -> tuple[set[str], set[str]]:
    """(all fields, required fields) of a contract table: `name` cells, **bold** = required."""
    fields, required = set(), set()
    table = _section(PACKS_README, heading).split("Value types in `values`:")[0]  # products: first table only
    for row in _table_rows(table):
        name = re.search(r"`([^`]+)`", row[0]).group(1)
        fields.add(name)
        if row[0].startswith("**"):
            required.add(name)
    return fields, required


def _run_documented_command(block: str, env: dict[str, str]) -> tuple[str, list[str]]:
    """Run the command of a ```bash block and return (stdout, expected lines from its # comments)."""
    lines = block.strip().splitlines()
    command, expected = lines[0], [line[2:] for line in lines[1:] if line.startswith("# ")]
    assert command.startswith("python -m "), command
    argv = shlex.split(command)
    files = [str(p.relative_to(PROJECT)) for a in argv[3:] for p in sorted(PROJECT.glob(a))]  # shell glob
    result = subprocess.run([sys.executable, "-m", argv[2], *files], cwd=PROJECT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout, expected


def _env(**extra: str) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(PROJECT), **extra}


# ----------------------------------------------------------------------- example commands


def test_validation_example_prints_what_the_readme_shows():
    block = next(b for b in _code_blocks(_section(PACKS_README, "Validating a pack")) if b.startswith("python -m"))
    stdout, expected = _run_documented_command(block, _env())
    assert stdout.splitlines() == expected


def test_import_example_prints_what_the_readme_shows(tmp_path):
    block = next(b for b in _code_blocks(_section(PACKS_README, "Importing a pack")) if b.startswith("python -m"))
    stdout, expected = _run_documented_command(block, _env(DATABASE_URL=f"sqlite:///{tmp_path / 'docs.db'}"))
    assert stdout.splitlines() == expected


def test_every_documented_command_is_a_runnable_module():
    texts = [PACKS_README, MAIN_README] + [p.read_text(encoding="utf-8") for p in (PROJECT / "app").rglob("*.py")]
    modules = {m for text in texts for m in re.findall(r"python -m (app(?:\.\w+)+)", text)}
    assert modules == {"app.services.store_pack_validation", "app.services.store_pack_import",
                       "app.services.inventory_export", "app.services.inventory_import"}
    for module in modules:
        result = subprocess.run([sys.executable, "-m", module, "--help"], cwd=PROJECT, env=_env(),
                                capture_output=True, text=True, timeout=60)
        assert result.returncode in (0, 2) and "usage: python -m " + module in result.stdout + result.stderr


# -------------------------------------------------------------------------- quoted errors


def _clothing() -> dict:
    return json.loads(CLOTHING.read_text(encoding="utf-8"))


def test_quoted_validation_errors_are_the_real_messages():
    section = _section(PACKS_README, "Validating a pack")
    several, version_only = [b for b in _code_blocks(section) if not b.startswith(("python", "from"))]

    pack = _clothing()  # the example described in the README
    del pack["store"]["slug"]
    pack["store"]["currency"] = "usd"
    pack["products"][0]["values"]["size"] = "XXL"
    assert [str(i) for i in contract.validate_store_pack(pack)] == several.strip().splitlines()

    pack = {**_clothing(), "version": 2}
    assert [str(i) for i in contract.validate_store_pack(pack)] == version_only.strip().splitlines()


def test_main_readme_error_examples_are_real():
    pack = _clothing()
    del pack["store"]["slug"]
    for message in re.findall(r"e\.g\. `(store\.slug: required)`", MAIN_README):
        assert message in [str(i) for i in contract.validate_store_pack(pack)]
    version = "version: unsupported version 2; this engine reads store packs with \"version\": 1"
    assert f"`{version}`" in MAIN_README
    assert [str(i) for i in contract.validate_store_pack({**_clothing(), "version": 2})] == [version]


# ----------------------------------------------------------------------- contract tables


def _at(data, path: str):
    """The object at a path like "categories[0].property_definitions[0]" ("" = the pack)."""
    for part in re.findall(r"[^.\[\]]+", path):
        data = data[int(part)] if part.isdigit() else data[part]
    return data


def _required_by_validator(pack: dict, path: str, fields: set[str]) -> set[str]:
    """Delete each present field in turn from a valid pack; the validator says which are required."""
    required = set()
    for name in fields & set(_at(pack, path)):
        trial = copy.deepcopy(pack)
        del _at(trial, path)[name]
        full = f"{path}.{name}" if path else name
        if any(i.path == full and i.message.startswith("required") for i in contract.validate_store_pack(trial)):
            required.add(name)
    return required


TABLES = [
    # heading, validator field set, where to probe in the clothing pack
    ("Top level", contract.TOP_LEVEL_FIELDS, ""),
    ("`store`", contract.STORE_FIELDS, "store"),
    ("`categories[]`", contract.CATEGORY_FIELDS, "categories[0]"),
    ("`categories[].property_definitions[]`", contract.PROPERTY_FIELDS, "categories[0].property_definitions[0]"),
    ("`products[]`", contract.PRODUCT_FIELDS, "products[0]"),
    ("`shipping_methods[]`", contract.SHIPPING_METHOD_FIELDS, "shipping_methods[0]"),
    ("`payment_terminals[]`", contract.PAYMENT_TERMINAL_FIELDS, "payment_terminals[0]"),
]


@pytest.mark.parametrize("heading,fields,path", TABLES, ids=[t[0] for t in TABLES])
def test_contract_table_lists_exactly_the_accepted_fields(heading, fields, path):
    documented, _ = _documented_fields(heading)
    assert documented == set(fields)


@pytest.mark.parametrize("heading,fields,path", TABLES, ids=[t[0] for t in TABLES])
def test_contract_table_marks_exactly_the_required_fields(heading, fields, path):
    _, documented_required = _documented_fields(heading)
    pack = _clothing()
    pack["storefront"] = {"copy_from_pack_dir": None}  # give every optional top-level field a value
    pack["ui_translations"] = {}
    pack["shipping_methods"] = [{"code": "standard", "name": "Standard", "rate_cents": 495,
                                 "free_shipping_threshold_cents": 5000, "countries": ["US"], "sort_order": 0,
                                 "is_active": True}]
    # A mock terminal: `config` is only required for kind bank_transfer (the table's Rules say so; a
    # bold field is one every item needs).
    pack["payment_terminals"] = [{"code": "mock_card", "name": "Card", "kind": "mock", "enabled": False,
                                  "sort_order": 0}]
    assert contract.validate_store_pack(pack) == []
    for name in set(fields) - set(_at(pack, path)):  # absent from a valid pack: optional by definition
        assert name not in documented_required, f"{heading}: {name} is documented as required"
    assert _required_by_validator(pack, path, set(fields)) == documented_required


def test_enum_options_are_documented_as_required():
    row = next(r for r in _table_rows(_section(PACKS_README, "`categories[].property_definitions[]`"))
               if r[0] == "`config`")
    assert "**`options`** is required" in row[2]
    pack = _clothing()
    del pack["categories"][0]["property_definitions"][0]["config"]["options"]  # "size" is an enum
    assert ("categories[0].property_definitions[0].config.options",
            "required for an enum property (the list of choices)") in [
        (i.path, i.message) for i in contract.validate_store_pack(pack)]


def test_property_types_are_documented():
    types = {t.value for t in PropertyType}
    value_rows = _table_rows(_section(PACKS_README, "`products[]`").split("Value types in `values`:")[1])
    assert {re.search(r"`(\w+)`", r[0]).group(1) for r in value_rows} == types
    prop_type_row = next(r for r in _table_rows(_section(PACKS_README, "`categories[].property_definitions[]`"))
                         if "prop_type" in r[0])
    assert set(re.findall(r"`(\w+)`", prop_type_row[2])) == types


@pytest.mark.parametrize(
    "heading,field,expected",
    [
        ("`store`", "slug", f"max {SLUG_MAX}"),
        ("`store`", "name", f"max {NAME_MAX}"),
        ("`categories[]`", "name", f"max {NAME_MAX}"),
        ("`categories[]`", "description", f"Max {CATEGORY_DESCRIPTION_MAX}"),
        ("`categories[]`", "sort_order", f"±{SORT_MAX:,}"),
        ("`categories[].property_definitions[]`", "key", f"max {KEY_MAX}"),
        ("`categories[].property_definitions[]`", "label", f"max {NAME_MAX}"),
        ("`categories[].property_definitions[]`", "config", f"1–{OPTIONS_MAX} unique"),
        ("`categories[].property_definitions[]`", "config", f"max {OPTION_MAX_LEN} characters"),
        ("`products[]`", "name", f"max {NAME_MAX}"),
        ("`products[]`", "description", f"Max {PRODUCT_DESCRIPTION_MAX}"),
        ("`products[]`", "price_cents", f"0 to {PRICE_MAX_CENTS:,}"),
        ("`products[]`", "sku", f"max {SKU_MAX}"),
        ("`products[]`", "stock", f"0 to {STOCK_MAX:,}"),
        ("`products[]`", "text", f"max {TEXT_VALUE_MAX}"),
        ("`products[]`", "image", f"max {URL_MAX}"),
        ("`shipping_methods[]`", "code", f"max {SLUG_MAX}"),
        ("`shipping_methods[]`", "name", f"max {NAME_MAX}"),
        ("`shipping_methods[]`", "rate_cents", f"0 to {MAX_AMOUNT_CENTS:,}"),
        ("`shipping_methods[]`", "sort_order", f"±{SORT_MAX:,}"),
        ("`payment_terminals[]`", "code", f"max {contract.TERMINAL_CODE_MAX}"),
        ("`payment_terminals[]`", "name", f"max {NAME_MAX}"),
        ("`payment_terminals[]`", "i18n", f"note max {bank_transfer.NOTE_MAX}"),
        ("`payment_terminals[]`", "sort_order", f"±{SORT_MAX:,}"),
        ("`payment_terminals[]`", "config", f"**`bank`** (max {bank_transfer.BANK_MAX})"),
        ("`payment_terminals[]`", "config", f"**`beneficiary`** (max {bank_transfer.BENEFICIARY_MAX})"),
        ("`payment_terminals[]`", "config", f"{bank_transfer.CLABE_LENGTH}-digit"),
        ("`payment_terminals[]`", "config", f"{bank_transfer.ACCOUNT_MIN} to {bank_transfer.ACCOUNT_MAX} digits"),
        ("`payment_terminals[]`", "config", f"`note` (max {bank_transfer.NOTE_MAX})"),
    ],
)
def test_documented_limits_are_the_code_constants(heading, field, expected):
    rows = _table_rows(_section(PACKS_README, heading))
    row = next(r for r in rows if re.search(r"`([^`]+)`", r[0]).group(1) == field)
    assert expected in " | ".join(row)


def test_other_documented_numbers_are_the_code_constants():
    assert f"nested at most {contract.MAX_JSON_DEPTH} levels deep" in PACKS_README
    assert f"at most {MAX_PACK_MB} MB" in MAIN_README and f"larger than {MAX_PACK_MB} MB" in MAIN_README
    assert f'"version": {contract.PACK_VERSION}' in PACKS_README


# ------------------------------------------------------------------- files and statuses


def test_files_table_lists_exactly_the_fixtures():
    rows = _table_rows(_section(PACKS_README, "Files in this folder"))
    documented = {re.search(r"`([^`]+)`", r[0]).group(1) for r in rows}
    assert documented == {p.name for p in PACKS_DIR.glob("*.json")}
    for name in documented:
        assert name in MAIN_README


def test_fixture_names_follow_the_documented_pattern():
    for path in PACKS_DIR.glob(f"*{contract.PACK_FILE_SUFFIX}"):
        pack = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == f"{pack['store']['slug']}{contract.PACK_FILE_SUFFIX}"


def test_import_record_statuses_are_documented():
    row = next(r for r in _table_rows(_section(MAIN_README, "Import records (`pack_imports`)")) if r[0] == "`status`")
    assert set(re.findall(r"`(\w+)`", row[1])) == {s.value for s in PackImportStatus}
