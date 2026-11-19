"""Store pack v1 contract: the validator accepts the fixtures and rejects broken packs with
clear, located errors. Validation only: nothing touches the database."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services.store_pack_validation import (
    PACK_FILE_SUFFIX,
    PACK_VERSION,
    PackIssue,
    StorePackError,
    check_store_pack,
    main,
    read_store_pack,
    validate_store_pack,
)

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
PACK_FILES = sorted(PACKS_DIR.glob("*.json"))
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def clothing() -> dict:
    return _load(CLOTHING)


@pytest.fixture()
def widgets() -> dict:
    return _load(WIDGETS)


def _issues(data) -> list[tuple[str, str]]:
    return [(issue.path, issue.message) for issue in validate_store_pack(data)]


# --------------------------------------------------------------------------- valid packs


def test_fixture_folder_has_the_schema_example_and_real_packs():
    names = {path.name for path in PACK_FILES}
    assert "store-pack.v1.schema.example.json" in names
    assert {CLOTHING.name, WIDGETS.name} <= names


@pytest.mark.parametrize("path", PACK_FILES, ids=lambda p: p.name)
def test_every_fixture_pack_is_valid(path):
    data = read_store_pack(path)
    assert data["version"] == PACK_VERSION
    assert validate_store_pack(data) == []


def test_clothing_fixture_covers_the_contract(clothing):
    """The generic fixture keeps a store with theme, a category, an enum property and a product."""
    store = clothing["store"]
    assert store["slug"] == "clothing-demo" and set(store["theme"]) >= {"primary", "secondary", "accent"}
    (shirts,) = clothing["categories"]
    types = {d["key"]: d["prop_type"] for d in shirts["property_definitions"]}
    assert types["size"] == "enum" and "S" in shirts["property_definitions"][0]["config"]["options"]
    (product,) = clothing["products"]
    assert product["category_slug"] == "shirts"
    assert set(product["values"]) == set(types)


def test_minimal_pack_is_valid():
    assert _issues({"version": 1, "store": {"slug": "s", "name": "S", "currency": "EUR"}}) == []


def test_check_returns_the_pack_unchanged_and_does_not_modify_it(clothing):
    before = copy.deepcopy(clothing)
    assert check_store_pack(clothing) is clothing
    assert clothing == before


def test_theme_may_carry_extra_keys_for_custom_storefronts(clothing):
    clothing["store"]["theme"]["font_family"] = "Georgia, serif"
    assert _issues(clothing) == []


def test_optional_values_may_be_null_or_missing(clothing):
    values = clothing["products"][0]["values"]
    values["material"] = None
    del values["sleeve_length_cm"]
    assert _issues(clothing) == []


# ------------------------------------------------------------------------------- version


@pytest.mark.parametrize(
    "version,message",
    [
        (2, 'unsupported version 2; this engine reads store packs with "version": 1'),
        (0, 'unsupported version 0; this engine reads store packs with "version": 1'),
        ("1", 'must be the number 1, got string "1"'),
        (1.0, "must be the number 1, got number 1.0"),
        (True, "must be the number 1, got boolean true"),
        (None, "must be the number 1, got null"),
    ],
)
def test_bad_version_is_rejected_with_a_clear_error(clothing, version, message):
    clothing["version"] = version
    assert _issues(clothing) == [("version", message)]


def test_missing_version_is_rejected(clothing):
    del clothing["version"]
    assert _issues(clothing) == [("version", 'required; this engine reads store packs with "version": 1')]


def test_bad_version_stops_validation(clothing):
    """Other fields can't be interpreted without a known version: only the version is reported."""
    clothing["version"] = 2
    del clothing["store"]["slug"]
    assert [path for path, _ in _issues(clothing)] == ["version"]


# ---------------------------------------------------------------------------- store.slug


def test_missing_store_slug_is_rejected(clothing):
    del clothing["store"]["slug"]
    assert _issues(clothing) == [("store.slug", "required")]


@pytest.mark.parametrize(
    "slug,message",
    [
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        (None, "must be a string, got null"),
        (42, "must be a string, got number"),
        ("Demo Shop", 'invalid slug "Demo Shop": use lower-case letters, digits and single hyphens, '
                      'e.g. "demo-widgets"'),
        ("demo--shop", 'invalid slug "demo--shop": use lower-case letters, digits and single hyphens, '
                       'e.g. "demo-widgets"'),
        ("new", '"new" is reserved; choose another slug'),
        ("a" * 65, "must be at most 64 characters (got 65)"),
    ],
)
def test_invalid_store_slug_is_rejected(clothing, slug, message):
    clothing["store"]["slug"] = slug
    assert _issues(clothing) == [("store.slug", message)]


def test_missing_store_is_rejected(clothing):
    del clothing["store"]
    assert ("store", "required") in _issues(clothing)


@pytest.mark.parametrize("data", [[], "pack", None, 1])
def test_pack_must_be_an_object(data):
    assert len(_issues(data)) == 1
    assert _issues(data)[0][1].startswith("a store pack must be a JSON object")


# ------------------------------------------------------------------------- other rules


def _set(path: str, value):
    """Mutation that sets a dotted path (``[n]`` for list items) in the pack."""
    def mutate(pack):
        *parents, last = path.replace("[", ".").replace("]", "").split(".")
        target = pack
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[int(last) if last.isdigit() else last] = value
    return mutate


def _delete(path: str):
    def mutate(pack):
        *parents, last = path.replace("[", ".").replace("]", "").split(".")
        target = pack
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        del target[last]
    return mutate


def _append(path: str, item_from: str):
    """Append a copy of the item at ``item_from`` to the list at ``path``."""
    def mutate(pack):
        def get(p):
            target = pack
            for part in p.replace("[", ".").replace("]", "").split("."):
                target = target[int(part)] if part.isdigit() else target[part]
            return target
        get(path).append(copy.deepcopy(get(item_from)))
    return mutate


DEF = "categories[0].property_definitions"
VALUES = "products[0].values"

CLOTHING_CASES = [
    # top level / store
    (_set("categoires", []), "categoires", "unknown field (allowed: categories, payment_terminals, products, shipping_methods, store, storefront, ui_translations, version)"),
    (_delete("store.name"), "store.name", "required"),
    (_delete("store.currency"), "store.currency", "required"),
    (_set("store.currency", "usd"), "store.currency", 'must be a 3-letter upper-case ISO code like USD, got "usd"'),
    (_set("store.locale_default", "fr"), "store.locale_default", 'must be one of the UI languages (es, en), got "fr"'),
    (_set("store.theme.primary", "red"), "store.theme.primary", 'must be a hex color like "#1a5f4a", got "red"'),
    (_set("store.theme.logo_url", "javascript:alert(1)"), "store.theme.logo_url",
     "must be null, an http(s) URL or a path starting with /"),
    (_set("store.theme", []), "store.theme", "must be an object, got array"),
    (_set("store.i18n", {"EN": {"name": "X"}}), "store.i18n.EN", 'locale must be a language code like "en" or "pt-BR"'),
    # categories
    (_set("categories", {}), "categories", "must be an array, got object"),
    (_append("categories", "categories[0]"), "categories[1].slug",
     'duplicate category slug "shirts" (also categories[0])'),
    (_set("categories[0].parent_slug", "tops"), "categories[0].parent_slug",
     'no category with slug "tops" in this pack'),
    (_set("categories[0].parent_slug", "shirts"), "categories[0].parent_slug", "a category cannot be its own parent"),
    (_set("categories[0].sort_order", "1"), "categories[0].sort_order", "must be a whole number, got string"),
    # property definitions
    (_set(f"{DEF}[0].prop_type", "color"), f"{DEF}[0].prop_type",
     'must be one of text, number, boolean, enum, image, category_ref, got "color"'),
    (_set(f"{DEF}[0].key", "Size"), f"{DEF}[0].key",
     'invalid key "Size": start with a letter; use lower-case letters, digits and underscores'),
    (_set(f"{DEF}[1].key", "size"), f"{DEF}[1].key", 'duplicate property key "size" in this category'),
    (_set(f"{DEF}[0].config", {}), f"{DEF}[0].config.options", "required for an enum property (the list of choices)"),
    (_set(f"{DEF}[0].config.options", ["S", "S"]), f"{DEF}[0].config.options", "duplicate option(s): S"),
    (_set(f"{DEF}[0].config.options", []), f"{DEF}[0].config.options", "must be a non-empty array of strings"),
    (_set(f"{DEF}[0].required", "yes"), f"{DEF}[0].required", "must be true or false, got string"),
    (_set(f"{DEF}[2].ref_category_slug", "shirts"), f"{DEF}[2].ref_category_slug",
     "only allowed when prop_type is category_ref"),
    # products
    (_set("products[0].category_slug", "pants"), "products[0].category_slug",
     'no category with slug "pants" in this pack'),
    (_delete("products[0].price_cents"), "products[0].price_cents", "required"),
    (_set("products[0].price_cents", 499.0), "products[0].price_cents",
     "must be a whole number of cents (1999 = 19.99), got number"),
    (_set("products[0].price_cents", -1), "products[0].price_cents", "must be between 0 and 99999999999"),
    (_set("products[0].is_active", 1), "products[0].is_active", "must be true or false, got number"),
    (_append("products", "products[0]"), "products[1].slug",
     'duplicate product slug "linen-shirt-m" (also products[0])'),
    (_set(f"{VALUES}.fabric", "linen"), f"{VALUES}.fabric",
     'category "shirts" has no property "fabric" '
     "(properties: color, machine_washable, material, size, sleeve_length_cm)"),
    (_delete(f"{VALUES}.size"), f"{VALUES}.size", 'required by category "shirts"'),
    (_set(f"{VALUES}.size", None), f"{VALUES}.size", 'required by category "shirts"'),
    (_set(f"{VALUES}.size", "XXL"), f"{VALUES}.size", 'must be one of the options (S, M, L, XL), got "XXL"'),
    (_set(f"{VALUES}.sleeve_length_cm", "62"), f"{VALUES}.sleeve_length_cm",
     "must be a number (number property), got string"),
    (_set(f"{VALUES}.machine_washable", "yes"), f"{VALUES}.machine_washable",
     "must be true or false (boolean property), got string"),
    (_set(f"{VALUES}.material", 100), f"{VALUES}.material", "must be a string (text property), got number"),
    # storefront
    (_set("storefront", {"copy_from_pack_dir": "../other-pack"}), "storefront.copy_from_pack_dir",
     'must not leave the pack folder ("..")'),
    (_set("storefront", {"copy_from_pack_dir": "/etc"}), "storefront.copy_from_pack_dir",
     'must be a relative folder inside the pack, like "storefront/"'),
    (_set("storefront", {"copy": "storefront/"}), "storefront.copy",
     "unknown field (allowed: copy_from_pack_dir, notes)"),
]


@pytest.mark.parametrize("mutate,path,message", CLOTHING_CASES, ids=[c[1] + ":" + c[2][:30] for c in CLOTHING_CASES])
def test_invalid_clothing_pack_is_rejected(clothing, mutate, path, message):
    mutate(clothing)
    assert (path, message) in _issues(clothing)


WIDGET_CASES = [
    (_set("products[0].values.brand", "widgets"), "products[0].values.brand",
     'must be "brands" or one of its subcategories, got "widgets"'),
    (_set("products[0].values.brand", "nike"), "products[0].values.brand",
     'must be the slug of a category in this pack (category_ref property), got "nike"'),
    (_set("categories[2].property_definitions[5].ref_category_slug", "makers"),
     "categories[2].property_definitions[5].ref_category_slug", 'no category with slug "makers" in this pack'),
    (_set("categories[0].parent_slug", "acme"), "categories[0].parent_slug",
     '"acme" is a subcategory of "brands" (cycle)'),
    (_set("categories[2].property_definitions[0].i18n.en.config.options", ["Red", "Blue"]),
     "categories[2].property_definitions[0].i18n.en.config.options",
     "must translate every option in order (3 expected, got 2)"),
    (_set("products[0].i18n.en.values.weight_g", "250 g"), "products[0].i18n.en.values.weight_g",
     "only text and enum values can be translated"),
    (_set("products[0].i18n.en.price_cents", 100), "products[0].i18n.en.price_cents",
     "unknown field (allowed: description, name, values)"),
]


@pytest.mark.parametrize("mutate,path,message", WIDGET_CASES, ids=[c[1] + ":" + c[2][:30] for c in WIDGET_CASES])
def test_invalid_widgets_pack_is_rejected(widgets, mutate, path, message):
    mutate(widgets)
    assert (path, message) in _issues(widgets)


def test_every_issue_is_reported_at_once(clothing):
    del clothing["store"]["slug"]
    clothing["store"]["currency"] = "dollars"
    clothing["products"][0]["values"]["size"] = "XXL"
    paths = [path for path, _ in _issues(clothing)]
    assert paths == ["store.slug", "store.currency", "products[0].values.size"]


def test_check_raises_with_a_readable_message(clothing):
    del clothing["store"]["slug"]
    with pytest.raises(StorePackError) as caught:
        check_store_pack(clothing)
    assert caught.value.issues == [PackIssue("store.slug", "required")]
    assert str(caught.value) == "Invalid store pack (1 issue(s)):\n- store.slug: required"


# ---------------------------------------------------------------------------- pack files


@pytest.mark.parametrize(
    "content,message",
    [
        (b'{"version": 1,', "not valid JSON: Expecting property name enclosed in double quotes (line 1, column 15)"),
        (b'{"version": 1, "version": 2}', 'duplicate key "version" in a JSON object'),
        (b'{"version": NaN}', "NaN is not allowed in a store pack (not standard JSON)"),
        (b'{"version": 1, "store": "\xff"}', "file is not UTF-8 text (invalid start byte at byte 25)"),
    ],
)
def test_broken_files_are_rejected(tmp_path, content, message):
    path = tmp_path / f"broken{PACK_FILE_SUFFIX}"
    path.write_bytes(content)
    with pytest.raises(StorePackError) as caught:
        read_store_pack(path)
    assert caught.value.issues == [PackIssue("", message)]


def test_read_checks_the_contract(tmp_path, clothing):
    clothing["version"] = 2
    path = tmp_path / "future.json"
    path.write_text(json.dumps(clothing), encoding="utf-8")
    with pytest.raises(StorePackError, match='unsupported version 2'):
        read_store_pack(path)


def test_command_line_reports_each_file(tmp_path, capsys, clothing):
    del clothing["store"]["slug"]
    clothing["store"]["currency"] = "usd"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(clothing), encoding="utf-8")

    assert main([str(CLOTHING), str(bad), str(tmp_path / "missing.json")]) == 1
    out = capsys.readouterr().out
    assert f"{CLOTHING}: valid\n" in out
    assert f"{bad}: INVALID (2 issues)\n  - store.slug: required\n  - store.currency: " in out
    assert "missing.json: cannot read" in out
    assert out.endswith("Checked 3 file(s): 1 valid, 2 invalid or unreadable. Nothing was imported.\n")


def test_command_line_succeeds_when_every_file_is_valid(capsys):
    assert main([str(path) for path in PACK_FILES]) == 0
    out = capsys.readouterr().out
    assert out.count(": valid\n") == len(PACK_FILES)
    assert f"{len(PACK_FILES)} valid, 0 invalid or unreadable. Nothing was imported." in out


def test_command_line_help_and_usage(capsys):
    assert main(["--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert help_text.startswith("usage: python -m app.services.store_pack_validation [-h] FILE [FILE ...]")
    assert "Validation only: nothing is written to the database." in help_text
    assert "python -m app.services.store_pack_import" in help_text
    assert main([]) == 2  # no file: usage error
    assert "the following arguments are required: FILE" in capsys.readouterr().err


def test_command_line_runs_as_a_module(tmp_path):
    """The documented command works from a shell (python -m, from the project folder)."""
    import subprocess
    import sys

    project = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "app.services.store_pack_validation", str(CLOTHING)],
        cwd=project, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"{CLOTHING}: valid" in result.stdout


# ----------------------------------------------------------------------------- robustness


@pytest.mark.parametrize("fixture", [CLOTHING, WIDGETS], ids=["clothing", "widgets"])
def test_validator_never_crashes_on_any_single_field_change(fixture):
    """Every field at every depth deleted or given a wrong type: issues, never an exception."""
    from tests.pack_variants import variants

    checked = 0
    for description, pack in variants(_load(fixture)):
        try:
            issues = validate_store_pack(pack)
        except Exception as exc:  # pragma: no cover - this is the failure being tested for
            pytest.fail(f"validator crashed on {description}: {type(exc).__name__}: {exc}")
        assert all(isinstance(i, PackIssue) for i in issues)
        checked += 1
    assert checked > 500


def test_missing_prop_type_is_reported_not_a_crash(clothing):
    """Regression: a definition without prop_type used to crash the product value checks."""
    del clothing["categories"][0]["property_definitions"][0]["prop_type"]
    assert _issues(clothing) == [("categories[0].property_definitions[0].prop_type", "required")]
