"""Store pack contract, version 1: validation only.

A store pack is one JSON document describing a store, its categories (with property
definitions), products and theme. This module only *checks* a pack: it never touches the
database and never writes files. Importing is done by ``app.services.store_pack_import``.

Usage::

    issues = validate_store_pack(data)     # [] when the pack is valid
    check_store_pack(data)                 # raises StorePackError listing every issue
    read_store_pack("x.store-pack.v1.json")  # parse the file strictly, then check it
    parse_store_pack(raw_bytes)            # same, for uploaded bytes

    python -m app.services.store_pack_validation fixtures/packs/*.store-pack.v1.json

Each issue has a ``path`` (``categories[2].property_definitions[0].key``) and a ``message``.
The field-level rules are the ones the admin forms apply, so a valid pack can be imported
without surprises. Every object is closed (unknown fields are rejected, which catches typos),
except ``store.theme``, which may carry extra keys for custom storefront templates.
The contract is documented in ``fixtures/packs/README.md``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from app.i18n import available_locales, normalize_locale
from app.models import MAX_AMOUNT_CENTS, MOCK_TERMINAL_PREFIX, PropertyType, TerminalKind
from app.services.category_admin import (
    DESCRIPTION_MAX as CATEGORY_DESCRIPTION_MAX,
    KEY_MAX,
    KEY_RE,
    OPTION_MAX_LEN,
    OPTIONS_MAX,
    SORT_MAX,
    SORT_MIN,
)
from app.services.product_admin import (
    DESCRIPTION_MAX as PRODUCT_DESCRIPTION_MAX,
    PRICE_MAX_CENTS,
    SKU_MAX,
    SKU_RE,
    TEXT_VALUE_MAX,
    URL_MAX,
    _is_safe_image_url as is_safe_image_url,  # same image-URL rule as the product form
    normalize_sku,
)
from app.services import bank_transfer
from app.services.stock import STOCK_MAX
from app.services.store_admin import CURRENCY_RE, NAME_MAX, RESERVED_SLUGS, SLUG_MAX, SLUG_RE
from app.services.storefront import THEME_COLOR_KEYS, normalize_hex_color
from app.services.translations import CONTENT_LOCALE_RE, TranslationError, check_ui_override, content_locale

# English texts of app/services/bank_transfer.validate_config's codes (the validator reports in English).
BANK_CONFIG_MESSAGES = {
    "required": "required",
    "too_long": "must be at most {max_len} characters",
    "clabe_format": "must be 18 digits",
    "clabe_check": "is not a valid CLABE (its control digit doesn't match)",
    "account_format": "must be 6 to 20 digits",
}

PACK_VERSION = 1
# Deepest JSON nesting accepted in a pack file. A valid pack needs about 7 levels
# (products[].i18n.<locale>.values.<key>); the limit leaves room for theme extras and keeps a
# hostile file ([[[[...]]]]) from exhausting the parser's recursion.
MAX_JSON_DEPTH = 64
PACK_FILE_SUFFIX = f".store-pack.v{PACK_VERSION}.json"

TOP_LEVEL_FIELDS = {"version", "store", "categories", "products", "shipping_methods", "payment_terminals", "storefront",
                    "ui_translations"}
STORE_FIELDS = {"slug", "name", "currency", "locale_default", "theme", "i18n"}
CATEGORY_FIELDS = {"slug", "name", "description", "sort_order", "parent_slug", "property_definitions", "i18n"}
PROPERTY_FIELDS = {"key", "label", "prop_type", "required", "sort_order", "config", "ref_category_slug", "i18n"}
PRODUCT_FIELDS = {"slug", "name", "description", "category_slug", "price_cents", "is_active", "values", "i18n",
                  "sku", "stock"}
STOREFRONT_FIELDS = {"copy_from_pack_dir", "notes"}
# Same rules as the admin shipping form (app/services/shipping_admin.py); amounts in cents.
SHIPPING_METHOD_FIELDS = {"code", "name", "rate_cents", "free_shipping_threshold_cents", "countries", "sort_order",
                          "is_active", "i18n"}
SHIPPING_METHOD_I18N_FIELDS = {"name"}
# The store's payment terminals (upserted by code). A mock's code starts with "mock_" (the admin
# simulator answers it); a bank_transfer terminal must not, and needs its deposit instructions.
PAYMENT_TERMINAL_FIELDS = {"code", "name", "kind", "enabled", "sort_order", "config", "i18n"}
PAYMENT_TERMINAL_I18N_FIELDS = {"name", "note"}
TERMINAL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TERMINAL_CODE_MAX = 64
COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
THEME_FIELDS = {*THEME_COLOR_KEYS, "logo_url"}

# Translatable fields per object, under "i18n": {"<locale>": {...}}.
STORE_I18N_FIELDS = {"name"}
CATEGORY_I18N_FIELDS = {"name", "description"}
PROPERTY_I18N_FIELDS = {"label", "config"}
PRODUCT_I18N_FIELDS = {"name", "description", "values"}
# Content translations may use any language code (CONTENT_LOCALE_RE), not only the UI languages;
# they are stored per language ("en-US" -> "en"), so one object can't have two blocks for it.
# Property types whose values are text that can be translated.
TRANSLATABLE_VALUE_TYPES = {PropertyType.text, PropertyType.enum}

PROP_TYPES = [t.value for t in PropertyType]
SLUG_HINT = 'lower-case letters, digits and single hyphens, e.g. "demo-widgets"'

Json = Any


@dataclass(frozen=True)
class PackIssue:
    """One problem in a pack. ``path`` locates the field ("" = the whole document)."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class StorePackError(ValueError):
    """A pack that does not meet the contract; ``issues`` lists every problem found."""

    def __init__(self, issues: list[PackIssue]) -> None:
        self.issues = list(issues)
        lines = "\n".join(f"- {issue}" for issue in self.issues)
        super().__init__(f"Invalid store pack ({len(self.issues)} issue(s)):\n{lines}")


# ------------------------------------------------------------------------------ public API


def validate_store_pack(data: Json) -> list[PackIssue]:
    """Every contract violation in ``data`` (parsed JSON). An empty list means valid."""
    return _Validator().run(data)


def check_store_pack(data: Json) -> dict[str, Any]:
    """Return ``data`` unchanged if it is a valid pack, else raise :class:`StorePackError`."""
    issues = validate_store_pack(data)
    if issues:
        raise StorePackError(issues)
    return data


def read_store_pack(path: Union[str, Path]) -> dict[str, Any]:
    """Parse a pack file strictly (UTF-8, no duplicate keys, no NaN / Infinity) and check it.
    Raises :class:`StorePackError` for invalid content; ``OSError`` if the file can't be read."""
    return parse_store_pack(Path(path).read_bytes())


def parse_store_pack(raw: bytes) -> dict[str, Any]:
    """Like :func:`read_store_pack`, for the bytes of a pack (e.g. an uploaded file)."""
    if not raw.strip():
        raise StorePackError([PackIssue("", "the file is empty")])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StorePackError([PackIssue("", f"file is not UTF-8 text ({exc.reason} at byte {exc.start})")]) from exc
    too_deep = PackIssue("", f"the JSON is nested too deeply (more than {MAX_JSON_DEPTH} levels)")
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except RecursionError as exc:  # [[[[...]]]] deeper than the parser can go
        raise StorePackError([too_deep]) from exc
    except _StrictJsonError as exc:
        raise StorePackError([PackIssue("", str(exc))]) from exc
    except json.JSONDecodeError as exc:
        raise StorePackError(
            [PackIssue("", f"not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")]
        ) from exc
    if _nesting_depth(data) > MAX_JSON_DEPTH:  # parsed, but too deep to store and render safely
        raise StorePackError([too_deep])
    return check_store_pack(data)


# ---------------------------------------------------------------------------- strict JSON


class _StrictJsonError(ValueError):
    pass


def _nesting_depth(data: Any) -> int:
    """Deepest array / object nesting of parsed JSON (iterative: no recursion)."""
    deepest = 0
    stack = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            deepest = max(deepest, depth)
            children = value.values() if isinstance(value, dict) else value
            stack.extend((child, depth + 1) for child in children)
    return deepest


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError(f'duplicate key "{key}" in a JSON object')
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise _StrictJsonError(f"{name} is not allowed in a store pack (not standard JSON)")


# ------------------------------------------------------------------------------ validator


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _show(value: Any) -> str:
    """A value as the pack author wrote it (JSON), shortened: "usd", true, null, 1.5."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _join(path: str, key: Union[str, int]) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


@dataclass
class _Category:
    """What later checks need to know about a category of the pack."""

    path: str
    parent_slug: Optional[str]
    definitions: dict[str, dict[str, Any]]  # key -> valid property definition
    definition_paths: dict[str, str]  # key -> "categories[i].property_definitions[j]"
    broken_keys: set[str]  # keys of definitions with errors (already reported): values not checked


class _Validator:
    def __init__(self) -> None:
        self.issues: list[PackIssue] = []
        self.categories: dict[str, _Category] = {}
        self.product_skus: dict[str, str] = {}  # normalized SKU -> path of the product that has it

    def error(self, path: str, message: str) -> None:
        self.issues.append(PackIssue(path, message))

    # ---------------------------------------------------------------- generic checks

    def obj(self, value: Any, path: str, allowed: set[str], required: tuple[str, ...] = (),
            open_: bool = False) -> bool:
        """Is ``value`` an object? Reports missing required and (unless ``open_``) unknown fields."""
        if not isinstance(value, dict):
            self.error(path, f"must be an object, got {_type_name(value)}")
            return False
        for name in required:
            if name not in value:
                self.error(_join(path, name), "required")
        if not open_:
            for name in value:
                if name not in allowed:
                    self.error(_join(path, name), f"unknown field (allowed: {', '.join(sorted(allowed))})")
        return True

    def array(self, value: Any, path: str) -> list[Any]:
        if not isinstance(value, list):
            self.error(path, f"must be an array, got {_type_name(value)}")
            return []
        return value

    def text(self, parent: dict[str, Any], name: str, path: str, max_len: int, *,
             required: bool = False, nullable: bool = False) -> Optional[str]:
        """A string field. Required strings must not be blank. Returns it if valid."""
        if name not in parent:
            return None  # missing required fields are reported by obj()
        value, field_path = parent[name], _join(path, name)
        if value is None and nullable and not required:
            return None
        if not isinstance(value, str):
            self.error(field_path, f"must be a string{' or null' if nullable else ''}, got {_type_name(value)}")
            return None
        if required and not value.strip():
            self.error(field_path, "must not be empty")
            return None
        if len(value) > max_len:
            self.error(field_path, f"must be at most {max_len} characters (got {len(value)})")
            return None
        return value

    def slug(self, parent: dict[str, Any], name: str, path: str) -> Optional[str]:
        value = self.text(parent, name, path, SLUG_MAX, required=True)
        if value is not None and not SLUG_RE.match(value):
            self.error(_join(path, name), f'invalid slug "{value}": use {SLUG_HINT}')
            return None
        return value

    def boolean(self, parent: dict[str, Any], name: str, path: str) -> None:
        if name in parent and not isinstance(parent[name], bool):
            self.error(_join(path, name), f"must be true or false, got {_type_name(parent[name])}")

    def sort_order(self, parent: dict[str, Any], path: str) -> None:
        if "sort_order" not in parent:
            return
        value, field_path = parent["sort_order"], _join(path, "sort_order")
        if not _is_int(value):
            self.error(field_path, f"must be a whole number, got {_type_name(value)}")
        elif not SORT_MIN <= value <= SORT_MAX:
            self.error(field_path, f"must be between {SORT_MIN:,} and {SORT_MAX:,}")

    def i18n(self, parent: dict[str, Any], path: str, fields: set[str],
             description_max: int = PRODUCT_DESCRIPTION_MAX) -> dict[str, dict[str, Any]]:
        """``"i18n": {"<locale>": {<translated fields>}}``; returns the valid locale blocks."""
        if "i18n" not in parent:
            return {}
        i18n_path = _join(path, "i18n")
        if not self.obj(parent["i18n"], i18n_path, set(), open_=True):
            return {}
        blocks = {}
        languages: dict[str, str] = {}
        for locale, block in parent["i18n"].items():
            block_path = _join(i18n_path, locale)
            if not CONTENT_LOCALE_RE.match(locale):
                self.error(block_path, 'locale must be a language code like "en" or "pt-BR"')
                continue
            language = content_locale(locale)
            if language in languages:
                self.error(block_path, f'same language as "{languages[language]}": translations are stored per '
                                       f'language ("{language}"), so use one block')
                continue
            languages[language] = locale
            if self.obj(block, block_path, fields):
                for name in fields & {"name", "label"}:
                    self.text(block, name, block_path, NAME_MAX, required=True)
                if "description" in fields:
                    self.text(block, "description", block_path, description_max, nullable=True)
                blocks[locale] = block
        return blocks

    # ------------------------------------------------------------------------ the pack

    def run(self, data: Json) -> list[PackIssue]:
        if not isinstance(data, dict):
            self.error("", f"a store pack must be a JSON object, got {_type_name(data)}")
            return self.issues
        if not self.version(data):
            return self.issues  # the rest of the document can't be read without a known version
        self.obj(data, "", TOP_LEVEL_FIELDS, required=("version", "store"))
        if "store" in data:
            self.store(data["store"])
        categories = self.array(data["categories"], "categories") if "categories" in data else []
        for index, category in enumerate(categories):
            self.category(category, f"categories[{index}]")
        self.category_links()
        products = self.array(data["products"], "products") if "products" in data else []
        seen_products: dict[str, str] = {}
        for index, product in enumerate(products):
            self.product(product, f"products[{index}]", seen_products)
        terminals = self.array(data["payment_terminals"], "payment_terminals") if "payment_terminals" in data else []
        seen_terminals: dict[str, str] = {}
        for index, terminal in enumerate(terminals):
            self.payment_terminal(terminal, f"payment_terminals[{index}]", seen_terminals)
        shipping_methods = self.array(data["shipping_methods"], "shipping_methods") if "shipping_methods" in data else []
        seen_codes: dict[str, str] = {}
        for index, method in enumerate(shipping_methods):
            self.shipping_method(method, f"shipping_methods[{index}]", seen_codes)
        if "storefront" in data:
            self.storefront(data["storefront"])
        if "ui_translations" in data:
            self.ui_translations(data["ui_translations"])
        return self.issues

    def version(self, data: dict[str, Any]) -> bool:
        expected = f'this engine reads store packs with "version": {PACK_VERSION}'
        if "version" not in data:
            self.error("version", f"required; {expected}")
            return False
        value = data["version"]
        if not _is_int(value):
            got = "null" if value is None else f"{_type_name(value)} {_show(value)}"
            self.error("version", f"must be the number {PACK_VERSION}, got {got}")
            return False
        if value != PACK_VERSION:
            self.error("version", f"unsupported version {value}; {expected}")
            return False
        return True

    def store(self, store: Any) -> None:
        path = "store"
        if not self.obj(store, path, STORE_FIELDS, required=("slug", "name", "currency")):
            return
        slug = self.slug(store, "slug", path)
        if slug in RESERVED_SLUGS:
            self.error("store.slug", f'"{slug}" is reserved; choose another slug')
        self.text(store, "name", path, NAME_MAX, required=True)
        if "currency" in store:
            currency = store["currency"]
            if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
                self.error("store.currency", f"must be a 3-letter upper-case ISO code like USD, got {_show(currency)}")
        if "locale_default" in store:
            locale = store["locale_default"]
            if not isinstance(locale, str) or normalize_locale(locale) is None:
                self.error(
                    "store.locale_default",
                    f"must be one of the UI languages ({', '.join(available_locales())}), got {_show(locale)}",
                )
        if "theme" in store:
            self.theme(store["theme"])
        self.i18n(store, path, STORE_I18N_FIELDS)

    def theme(self, theme: Any) -> None:
        path = "store.theme"
        if not self.obj(theme, path, THEME_FIELDS, open_=True):
            return
        for key in THEME_COLOR_KEYS:
            if key in theme and normalize_hex_color(theme[key]) is None:
                self.error(_join(path, key), f'must be a hex color like "#1a5f4a", got {_show(theme[key])}')
        logo = theme.get("logo_url")
        if logo is not None and (not isinstance(logo, str) or len(logo) > URL_MAX or not is_safe_image_url(logo)):
            self.error(_join(path, "logo_url"), "must be null, an http(s) URL or a path starting with /")

    # ----------------------------------------------------------------------- categories

    def category(self, category: Any, path: str) -> None:
        if not self.obj(category, path, CATEGORY_FIELDS, required=("slug", "name")):
            return
        slug = self.slug(category, "slug", path)
        self.text(category, "name", path, NAME_MAX, required=True)
        self.text(category, "description", path, CATEGORY_DESCRIPTION_MAX, nullable=True)
        self.sort_order(category, path)
        parent_slug = self.text(category, "parent_slug", path, SLUG_MAX, nullable=True)
        self.i18n(category, path, CATEGORY_I18N_FIELDS, CATEGORY_DESCRIPTION_MAX)

        definitions: dict[str, dict[str, Any]] = {}
        definition_paths: dict[str, str] = {}
        broken_keys: set[str] = set()
        if "property_definitions" in category:
            defs_path = _join(path, "property_definitions")
            for index, definition in enumerate(self.array(category["property_definitions"], defs_path)):
                def_path = f"{defs_path}[{index}]"
                key = self.property_definition(definition, def_path)
                if key is None:
                    if isinstance(definition, dict) and isinstance(definition.get("key"), str):
                        broken_keys.add(definition["key"])
                    continue
                if key in definitions:
                    self.error(_join(def_path, "key"), f'duplicate property key "{key}" in this category')
                else:
                    definitions[key] = definition
                    definition_paths[key] = def_path

        if slug is None:
            return
        if slug in self.categories:
            self.error(_join(path, "slug"), f'duplicate category slug "{slug}" (also {self.categories[slug].path})')
            return
        self.categories[slug] = _Category(path, parent_slug, definitions, definition_paths, broken_keys)

    def property_definition(self, definition: Any, path: str) -> Optional[str]:
        """Checks one definition; returns its key if the definition is usable by products."""
        if not self.obj(definition, path, PROPERTY_FIELDS, required=("key", "label", "prop_type")):
            return None
        usable = True
        key = self.text(definition, "key", path, KEY_MAX, required=True)
        if key is not None and not KEY_RE.match(key):
            self.error(_join(path, "key"), f'invalid key "{key}": start with a letter; use lower-case '
                                           "letters, digits and underscores")
            key = None
        if self.text(definition, "label", path, NAME_MAX, required=True) is None:
            usable = False
        prop_type = definition.get("prop_type")
        if "prop_type" not in definition:
            usable = False  # reported as required by obj(); products can't be checked against it
        elif prop_type not in PROP_TYPES:
            self.error(_join(path, "prop_type"), f"must be one of {', '.join(PROP_TYPES)}, got {_show(prop_type)}")
            usable = False
        self.boolean(definition, "required", path)
        self.sort_order(definition, path)

        options: Optional[list[str]] = None
        config = definition.get("config", {})
        if "config" in definition and not self.obj(config, _join(path, "config"), set(), open_=True):
            usable = False
        elif prop_type == PropertyType.enum.value:
            options = self.enum_options(config, _join(path, "config"))
            usable = usable and options is not None

        if "ref_category_slug" in definition:
            ref = definition["ref_category_slug"]
            if ref is not None and prop_type != PropertyType.category_ref.value:
                self.error(_join(path, "ref_category_slug"), "only allowed when prop_type is category_ref")
            elif ref is not None and not isinstance(ref, str):
                self.error(_join(path, "ref_category_slug"), f"must be a category slug or null, got {_type_name(ref)}")
                usable = False

        for locale, block in self.i18n(definition, path, PROPERTY_I18N_FIELDS).items():
            if "config" in block:
                self.translated_options(block["config"], _join(path, f"i18n.{locale}.config"), prop_type, options)
        return key if usable else None

    def enum_options(self, config: dict[str, Any], path: str) -> Optional[list[str]]:
        options_path = _join(path, "options")
        if "options" not in config:
            self.error(options_path, "required for an enum property (the list of choices)")
            return None
        options = config["options"]
        if not isinstance(options, list) or not options:
            self.error(options_path, "must be a non-empty array of strings")
            return None
        if len(options) > OPTIONS_MAX:
            self.error(options_path, f"at most {OPTIONS_MAX} options")
            return None
        for index, option in enumerate(options):
            if not isinstance(option, str) or not option.strip() or len(option) > OPTION_MAX_LEN:
                self.error(f"{options_path}[{index}]", f"must be a non-empty string of at most {OPTION_MAX_LEN} characters")
                return None
        duplicates = sorted({o for o in options if options.count(o) > 1})
        if duplicates:
            self.error(options_path, f"duplicate option(s): {', '.join(duplicates)}")
            return None
        return options

    def translated_options(self, config: Any, path: str, prop_type: Any, options: Optional[list[str]]) -> None:
        if not self.obj(config, path, {"options"}):
            return
        if "options" not in config:
            return
        if prop_type != PropertyType.enum.value:
            self.error(_join(path, "options"), "only allowed for enum properties")
            return
        translated = config["options"]
        if not isinstance(translated, list) or not all(isinstance(o, str) and o.strip() for o in translated):
            self.error(_join(path, "options"), "must be an array of non-empty strings")
        elif any(len(o) > OPTION_MAX_LEN for o in translated):
            self.error(_join(path, "options"), f"each option must be at most {OPTION_MAX_LEN} characters")
        elif options is not None and len(translated) != len(options):
            self.error(_join(path, "options"),
                       f"must translate every option in order ({len(options)} expected, got {len(translated)})")

    def category_links(self) -> None:
        """parent_slug and ref_category_slug must point at categories of the pack; no cycles."""
        for slug, info in self.categories.items():
            parent = info.parent_slug
            if parent is not None:
                if parent == slug:
                    self.error(_join(info.path, "parent_slug"), "a category cannot be its own parent")
                elif parent not in self.categories:
                    self.error(_join(info.path, "parent_slug"), f'no category with slug "{parent}" in this pack')
                elif self.in_cycle(slug):
                    self.error(_join(info.path, "parent_slug"), f'"{parent}" is a subcategory of "{slug}" (cycle)')
            for key, definition in info.definitions.items():
                ref = definition.get("ref_category_slug")
                if isinstance(ref, str) and ref not in self.categories:
                    self.error(_join(info.definition_paths[key], "ref_category_slug"),
                               f'no category with slug "{ref}" in this pack')

    def in_cycle(self, slug: str) -> bool:
        seen = {slug}
        current = self.categories[slug].parent_slug
        while current is not None and current in self.categories:
            if current in seen:
                return True
            seen.add(current)
            current = self.categories[current].parent_slug
        return False

    def subtree(self, root: str) -> set[str]:
        """``root`` and every category below it."""
        found = {root}
        changed = True
        while changed:
            changed = False
            for slug, info in self.categories.items():
                if slug not in found and info.parent_slug in found:
                    found.add(slug)
                    changed = True
        return found

    # ------------------------------------------------------------------------- products

    def product(self, product: Any, path: str, seen: dict[str, str]) -> None:
        if not self.obj(product, path, PRODUCT_FIELDS, required=("slug", "name", "category_slug", "price_cents")):
            return
        slug = self.slug(product, "slug", path)
        if slug is not None:
            if slug in seen:
                self.error(_join(path, "slug"), f'duplicate product slug "{slug}" (also {seen[slug]})')
            else:
                seen[slug] = path
        self.text(product, "name", path, NAME_MAX, required=True)
        self.text(product, "description", path, PRODUCT_DESCRIPTION_MAX, nullable=True)
        self.boolean(product, "is_active", path)
        if "price_cents" in product:
            price = product["price_cents"]
            if not _is_int(price):
                self.error(_join(path, "price_cents"), f"must be a whole number of cents (1999 = 19.99), "
                                                       f"got {_type_name(price)}")
            elif not 0 <= price <= PRICE_MAX_CENTS:
                self.error(_join(path, "price_cents"), f"must be between 0 and {PRICE_MAX_CENTS}")
        if "sku" in product:
            self.product_sku(product["sku"], _join(path, "sku"))
        if "stock" in product:
            stock = product["stock"]
            if not _is_int(stock):
                self.error(_join(path, "stock"), f"must be a whole number of units, got {_type_name(stock)}")
            elif not 0 <= stock <= STOCK_MAX:
                self.error(_join(path, "stock"), f"must be between 0 and {STOCK_MAX}")

        category = None
        category_slug = product.get("category_slug")
        if "category_slug" in product:
            if not isinstance(category_slug, str):
                self.error(_join(path, "category_slug"), f"must be a category slug, got {_type_name(category_slug)}")
            elif category_slug not in self.categories:
                self.error(_join(path, "category_slug"), f'no category with slug "{category_slug}" in this pack')
            else:
                category = self.categories[category_slug]

        values = product.get("values", {})
        if "values" in product and not self.obj(values, _join(path, "values"), set(), open_=True):
            values = None
        if category is not None and values is not None:
            self.product_values(values, _join(path, "values"), category, category_slug)

        for locale, block in self.i18n(product, path, PRODUCT_I18N_FIELDS).items():
            if "values" in block and category is not None:
                self.translated_values(block["values"], _join(path, f"i18n.{locale}.values"), category)

    def product_sku(self, sku: Any, path: str) -> None:
        """Same rules as the admin form's SKU; unique in the pack ignoring case (stored upper-case)."""
        if not isinstance(sku, str):
            self.error(path, f"must be a string, got {_type_name(sku)}")
            return
        value = sku.strip()
        if not value or len(value) > SKU_MAX or not SKU_RE.match(value):
            self.error(path, f"use letters, digits, hyphens, underscores and dots, starting with a letter or digit, "
                             f"no spaces (1 to {SKU_MAX} characters)")
            return
        normalized = normalize_sku(value)
        if normalized in self.product_skus:
            self.error(path, f'duplicate SKU "{normalized}" (also {self.product_skus[normalized]})')
        else:
            self.product_skus[normalized] = path

    def product_values(self, values: dict[str, Any], path: str, category: _Category, category_slug: str) -> None:
        for key in values:
            if key not in category.definitions and key not in category.broken_keys:
                known = ", ".join(sorted(category.definitions)) or "none"
                self.error(_join(path, key), f'category "{category_slug}" has no property "{key}" (properties: {known})')
        for key, definition in category.definitions.items():
            value = values.get(key)
            value_path = _join(path, key)
            if value is None:
                if definition.get("required"):
                    self.error(value_path, f'required by category "{category_slug}"')
                continue
            message = self.value_problem(definition, value)
            if message:
                self.error(value_path, message)

    def value_problem(self, definition: dict[str, Any], value: Any) -> Optional[str]:
        """Why ``value`` is not valid for the property, or None. (null = no value, checked before.)"""
        kind = PropertyType(definition["prop_type"])
        if kind == PropertyType.text:
            if not isinstance(value, str):
                return f"must be a string (text property), got {_type_name(value)}"
            if len(value) > TEXT_VALUE_MAX:
                return f"must be at most {TEXT_VALUE_MAX} characters"
        elif kind == PropertyType.number:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return f"must be a number (number property), got {_type_name(value)}"
        elif kind == PropertyType.boolean:
            if not isinstance(value, bool):
                return f"must be true or false (boolean property), got {_type_name(value)}"
        elif kind == PropertyType.enum:
            options = definition["config"]["options"]
            if value not in options:
                return f"must be one of the options ({', '.join(options)}), got {_show(value)}"
        elif kind == PropertyType.image:
            if not isinstance(value, str) or len(value) > URL_MAX or not is_safe_image_url(value):
                return "must be an http(s) URL or a path starting with / (image property)"
        else:  # category_ref: the slug of a category of the pack
            if not isinstance(value, str) or value not in self.categories:
                return f"must be the slug of a category in this pack (category_ref property), got {_show(value)}"
            ref = definition.get("ref_category_slug")
            if isinstance(ref, str) and ref in self.categories and value not in self.subtree(ref):
                return f'must be "{ref}" or one of its subcategories, got "{value}"'
        return None

    def translated_values(self, values: Any, path: str, category: _Category) -> None:
        if not self.obj(values, path, set(), open_=True):
            return
        for key, value in values.items():
            definition = category.definitions.get(key)
            if definition is None and key in category.broken_keys:
                continue  # the definition's own error is reported
            if definition is None:
                self.error(_join(path, key), f'no property "{key}" in the product\'s category')
            elif PropertyType(definition["prop_type"]) not in TRANSLATABLE_VALUE_TYPES:
                self.error(_join(path, key), "only text and enum values can be translated")
            elif not isinstance(value, str) or not value.strip():
                self.error(_join(path, key), "must be a non-empty string")
            elif len(value) > TEXT_VALUE_MAX:
                self.error(_join(path, key), f"must be at most {TEXT_VALUE_MAX} characters")

    # ------------------------------------------------------------------ ui_translations

    def ui_translations(self, value: Any) -> None:
        """``{"<UI language>": {"<key from app/locales/es.json>": "text"}}``: this store's UI
        overrides (level 2 of the translations)."""
        path = "ui_translations"
        if not self.obj(value, path, set(), open_=True):
            return
        for locale, texts in value.items():
            locale_path = _join(path, locale)
            if normalize_locale(locale) is None:
                self.error(locale_path, f"must be a UI language ({', '.join(available_locales())}), got {_show(locale)}")
                continue
            if not self.obj(texts, locale_path, set(), open_=True):
                continue
            for key, text in texts.items():
                try:
                    check_ui_override(locale, key, text)
                except TranslationError as exc:
                    self.error(_join(locale_path, key), str(exc))

    # ------------------------------------------------------------------ shipping methods

    def shipping_method(self, method: Any, path: str, seen_codes: dict[str, str]) -> None:
        if not self.obj(method, path, SHIPPING_METHOD_FIELDS, required=("code", "name", "rate_cents")):
            return
        code = self.slug(method, "code", path)
        if code is not None:
            if code in seen_codes:
                self.error(_join(path, "code"), f'duplicate code "{code}" (also {seen_codes[code]})')
            else:
                seen_codes[code] = path
        self.text(method, "name", path, NAME_MAX, required=True)
        self.amount_cents(method, "rate_cents", path)
        if method.get("free_shipping_threshold_cents") is not None:
            self.amount_cents(method, "free_shipping_threshold_cents", path)
        if "countries" in method:
            countries_path = _join(path, "countries")
            seen: set[str] = set()
            for index, country in enumerate(self.array(method["countries"], countries_path)):
                item_path = f"{countries_path}[{index}]"
                if not isinstance(country, str) or not COUNTRY_CODE_RE.match(country):
                    self.error(item_path, f"must be a 2-letter upper-case country code like MX, got {_show(country)}")
                elif country in seen:
                    self.error(item_path, f'duplicate country "{country}"')
                else:
                    seen.add(country)
        self.sort_order(method, path)
        self.boolean(method, "is_active", path)
        self.i18n(method, path, SHIPPING_METHOD_I18N_FIELDS)

    def payment_terminal(self, terminal: Any, path: str, seen_codes: dict[str, str]) -> None:
        if not self.obj(terminal, path, PAYMENT_TERMINAL_FIELDS, required=("code", "name", "kind")):
            return
        code = terminal.get("code")  # missing code / kind: reported by obj()
        code_path = _join(path, "code")
        if code is None:
            pass
        elif not isinstance(code, str) or len(code) > TERMINAL_CODE_MAX or not TERMINAL_CODE_RE.match(code):
            self.error(code_path, f"must be lower-case letters, digits and underscores, starting with a letter "
                                  f"(max {TERMINAL_CODE_MAX}), got {_show(code)}")
            code = None
        elif code in seen_codes:
            self.error(code_path, f'duplicate code "{code}" (also {seen_codes[code]})')
        else:
            seen_codes[code] = path
        self.text(terminal, "name", path, NAME_MAX, required=True)
        kind = terminal.get("kind")
        kinds = [k.value for k in TerminalKind]
        if kind is None:
            pass
        elif kind not in kinds:
            self.error(_join(path, "kind"), f"must be one of {', '.join(kinds)}, got {_show(kind)}")
            kind = None
        if code is not None and kind is not None:
            if (kind == TerminalKind.mock.value) != code.startswith(MOCK_TERMINAL_PREFIX):
                self.error(code_path, f'a "{kind}" terminal\'s code must {"" if kind == "mock" else "not "}'
                                      f'start with "{MOCK_TERMINAL_PREFIX}"')
        self.boolean(terminal, "enabled", path)
        self.sort_order(terminal, path)
        config = terminal.get("config")
        if kind == TerminalKind.bank_transfer.value:
            if not isinstance(config, dict):
                self.error(_join(path, "config"), "required for a bank_transfer terminal: its deposit instructions")
            else:
                unknown = sorted(set(config) - set(bank_transfer.CONFIG_FIELDS))
                for field in unknown:
                    self.error(_join(path, f"config.{field}"), "unknown field")
                _, problems = bank_transfer.validate_config(config)
                for field, (code_, params) in problems.items():
                    self.error(_join(path, f"config.{field}"), BANK_CONFIG_MESSAGES[code_].format(**params))
        elif kind == TerminalKind.mock.value and config not in (None, {}):
            self.error(_join(path, "config"), "a mock terminal has no config")
        for locale, block in self.i18n(terminal, path, PAYMENT_TERMINAL_I18N_FIELDS).items():
            block_path = _join(path, f"i18n.{locale}")
            if "note" in block and kind is not None and kind != TerminalKind.bank_transfer.value:
                self.error(_join(block_path, "note"), "only a bank_transfer terminal has a note")
            else:
                self.text(block, "note", block_path, bank_transfer.NOTE_MAX, required=True)

    def amount_cents(self, parent: dict[str, Any], name: str, path: str) -> None:
        if name not in parent:
            return  # missing required fields are reported by obj()
        value, field_path = parent[name], _join(path, name)
        if not _is_int(value):
            self.error(field_path, f"must be a whole number of cents, got {_type_name(value)}")
        elif not 0 <= value <= MAX_AMOUNT_CENTS:
            self.error(field_path, f"must be 0 to {MAX_AMOUNT_CENTS:,} cents")

    # ----------------------------------------------------------------------- storefront

    def storefront(self, storefront: Any) -> None:
        path = "storefront"
        if not self.obj(storefront, path, STOREFRONT_FIELDS):
            return
        self.text(storefront, "notes", path, 2000, nullable=True)
        folder = storefront.get("copy_from_pack_dir")
        if folder is None:
            return
        problem = None
        if not isinstance(folder, str) or not folder.strip():
            problem = "must be null or a folder path inside the pack"
        elif folder.startswith(("/", "~")) or "\\" in folder or ":" in folder:
            problem = 'must be a relative folder inside the pack, like "storefront/"'
        elif ".." in Path(folder).parts:
            problem = 'must not leave the pack folder ("..")'
        if problem:
            self.error(_join(path, "copy_from_pack_dir"), problem)


# ----------------------------------------------------------------------------------- CLI


CLI_NAME = "python -m app.services.store_pack_validation"


def _cli_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=CLI_NAME,
        description=f"Check store pack files against the store pack contract v{PACK_VERSION} "
                    "(fixtures/packs/README.md). Validation only: nothing is written to the database.",
        epilog="Exit status: 0 = every file is valid, 1 = a file is invalid or unreadable, 2 = usage error. "
               "To import valid packs: python -m app.services.store_pack_import FILE...",
    )


def main(argv: list[str]) -> int:
    """``python -m app.services.store_pack_validation FILE...``: validate pack files; nothing is
    imported. Prints one line per file (``valid`` / ``INVALID (n issues)`` + the issues)."""
    parser = _cli_parser()
    parser.add_argument("files", nargs="+", metavar="FILE", help="store pack file (.json)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # usage error or --help: argparse already printed the message
        return int(exc.code or 0)

    valid = 0
    for name in args.files:
        try:
            read_store_pack(name)
        except StorePackError as exc:
            count = len(exc.issues)
            print(f"{name}: INVALID ({count} issue{'s' if count != 1 else ''})")
            for issue in exc.issues:
                print(f"  - {issue}")
        except OSError as exc:
            print(f"{name}: cannot read ({exc.strerror})")
        else:
            valid += 1
            print(f"{name}: valid")
    total = len(args.files)
    print(f"Checked {total} file(s): {valid} valid, {total - valid} invalid or unreadable. Nothing was imported.")
    return 0 if valid == total else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
