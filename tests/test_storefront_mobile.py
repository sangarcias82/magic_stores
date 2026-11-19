"""Phone-sized storefront (solares and the cart): done with CSS only, in the shared stylesheets
(app/static/cart/cart.css, app/storefronts/_default/static/style.css) and the store's own
(app/storefronts/solares/static/style.css, identical to the pack's copy). These tests pin the rules
that make it work; the screenshots in the README are the visual check."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
CART_CSS = PROJECT / "app" / "static" / "cart" / "cart.css"
CART_JS = PROJECT / "app" / "static" / "cart" / "cart.js"
DEFAULT_CSS = PROJECT / "app" / "storefronts" / "_default" / "static" / "style.css"
SOLARES = PROJECT / "app" / "storefronts" / "solares"
SOLARES_PACK = PROJECT / "fixtures" / "packs" / "solares" / "storefront"
PHONE = "@media (max-width: 640px)"
TAP = 44  # px: the smallest comfortable touch target


def _phone_block(path: Path) -> str:
    """The body of the stylesheet's phone media query."""
    css = path.read_text(encoding="utf-8")
    start = css.index(PHONE)
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if depth == 0:
            return css[i + 1:j]
    raise AssertionError("unclosed media query")


def _rules(block: str) -> dict[str, str]:
    """{selector: declarations} of a block (each selector of a group on its own)."""
    rules: dict[str, str] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", block, flags=re.S)):
        for selector in selectors.split(","):
            rules[" ".join(selector.split())] = rules.get(" ".join(selector.split()), "") + body
    return rules


def _px(declarations: str, prop: str) -> int:
    value = re.search(rf"(?<![-\w]){prop}:\s*([0-9.]+)(px|rem)", declarations)
    assert value, f"{prop} not set in: {declarations}"
    number, unit = float(value.group(1)), value.group(2)
    return round(number * 16 if unit == "rem" else number)


# ---------------------------------------------------------------------------------- the cart


@pytest.mark.parametrize("selector", [".cart-step", ".cart-pay", ".cart-add button", ".cart-remove",
                                      ".cart-clear", ".cart-add-ref button", ".cart-qty-input",
                                      ".cart-checkout-form select", ".cart-checkout-form input"])
def test_cart_tap_targets_are_large_on_phones(selector):
    rules = _rules(_phone_block(CART_CSS))
    assert _px(rules[selector], "min-height") >= TAP, selector


@pytest.mark.parametrize("selector", [".cart-qty-input", ".cart-checkout-form select", ".cart-checkout-form input",
                                      ".cart-add-fields input"])
def test_form_fields_are_16px_so_phones_dont_zoom(selector):
    assert _px(_rules(_phone_block(CART_CSS))[selector], "font-size") >= 16


def test_the_cart_is_one_column_of_cards_on_phones():
    rules = _rules(_phone_block(CART_CSS))
    assert "display: grid" in rules[".cart-lines tr"] and "grid-template-areas" in rules[".cart-lines tr"]
    assert "display: block" in rules[".cart-lines td"]
    assert "grid-template-columns: minmax(0, 1fr)" in rules[".cart-checkout-form"]  # one column: label, field
    assert "width: 100%" in rules[".cart-pay"] and "width: 100%" in rules[".cart-checkout-form select"]
    # The cells the card layout places, and the labels it shows, are in the markup cart.js renders.
    js = CART_JS.read_text(encoding="utf-8")
    for name in ("cart-pic", "cart-name", "cart-price", "cart-qty", "cart-total", "cart-remove-cell"):
        assert f'"{name}' in js or f" {name}\"" in js, name
        assert f".{name}" in _phone_block(CART_CSS), name
    assert js.count('"data-label": text(') == 3  # price, quantity, line total


def test_the_desktop_cart_is_unchanged():
    """Outside the phone block the cart keeps its table and two-column checkout form."""
    css = CART_CSS.read_text(encoding="utf-8").split(PHONE)[0]
    assert ".cart-checkout-form { display: grid; grid-template-columns: max-content minmax(10rem, 20rem);" in css
    assert "display: grid" not in _rules(css).get(".cart-lines tr", "")


# ------------------------------------------------------------------------------ the stores


@pytest.mark.parametrize("path", [SOLARES / "static" / "style.css", DEFAULT_CSS], ids=["solares", "_default"])
def test_store_pages_fit_a_phone(path):
    css = path.read_text(encoding="utf-8")
    assert re.search(r"(?m)^img \{ max-width: 100%; height: auto; \}", css)  # images never wider than the screen
    rules = _rules(_phone_block(path))
    assert rules[".wrap"].strip() == "width: calc(100% - 1.25rem);"  # 10px sides instead of 16px
    assert _px(rules["body"], "font-size") >= 17  # readable
    assert "position: static" in rules[".site-header"]  # the header scrolls away
    assert "flex-wrap: wrap" in rules[".site-header .wrap"]
    assert _px(rules["nav a"], "min-height") >= TAP
    single = re.search(r"\.props \{[^}]*grid-template-columns: (minmax\(0, )?1fr\)?;", css[css.index("@media"):])
    assert single, "property list in one column on small screens"


def test_solares_phone_rules():
    rules = _rules(_phone_block(SOLARES / "static" / "style.css"))
    for selector in (".product-grid", ".category-cards"):
        assert "grid-template-columns: minmax(0, 1fr)" in rules[selector], selector
    assert _px(rules[".product-grid li a"], "min-height") >= TAP
    assert _px(rules[".solar-product"], "padding") <= 12  # the photo gets the width


def test_the_pages_declare_a_phone_viewport():
    base = (PROJECT / "app" / "storefronts" / "_default" / "templates" / "base.html").read_text(encoding="utf-8")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in base
    assert '<link rel="stylesheet" href="/static/cart/cart.css">' in base


@pytest.mark.parametrize("name", ["templates/product.html", "templates/store.html", "static/style.css"])
def test_solares_files_are_the_pack_copies(name):
    """The store's files are what the pack installs: change one, copy it to the other."""
    assert (SOLARES / name).read_bytes() == (SOLARES_PACK / name).read_bytes()
