"""The README's documentation of shipping, payment terminals, payments and their admin pages is checked
against the code, so the two can't drift apart (like tests/test_pack_docs.py does for the pack contract):

- every route of these features is documented, and every /api or /admin path the README shows exists;
- documented error types, statuses, request fields and JSON examples are exactly the code's;
- quoted numbers and names (limits, windows, page sizes, headers, default terminals) are the constants;
- the solares shipping table and the operator demo's amounts are what the solares pack gives;
- the store tabs, the schema table and the file paths the README mentions are real;
- the callback signing recipe (bash + openssl) produces the signature the app accepts.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import models
from app.config import PAYMENT_CALLBACK_SECRET_MIN
from app.db import Base
from app.i18n import catalogs
from app.main import create_app
from app.routers import admin_imports, admin_payments, admin_try_payment
from app.routers.admin import MAX_PAGE
from app.schemas import (
    PaymentCallbackIn,
    PaymentCreateIn,
    PaymentOut,
    PaymentTerminalOut,
    ShippingMethodOut,
    ShippingQuoteIn,
    ShippingQuoteOut,
)
from app.services import payment_callbacks, payments, shipping, shipping_admin, terminal_admin
from app.services.catalog import format_price
from app.services.store_pack_validation import read_store_pack
from tests.markdown_doc import code_blocks, section, table_rows

PROJECT = Path(__file__).resolve().parent.parent
README = (PROJECT / "README.md").read_text(encoding="utf-8")
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")

# Route prefixes of the features documented here (route templates as FastAPI writes them).
FEATURE_ROUTES = (
    "/api/stores/{store_slug}/shipping",
    "/api/stores/{store_slug}/payment",
    "/admin/stores/{store_slug}/shipping",
    "/admin/stores/{store_slug}/terminals",
    "/admin/stores/{store_slug}/try-payment",
    "/admin/stores/{store_slug}/payments",
)


def _routes() -> list[tuple[str, str]]:
    """(method, path template) of every route of the app."""
    return [(method.upper(), path) for path, operations in create_app().openapi()["paths"].items()
            for method in operations]


def _as_regex(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(template)) + "$")


def _documented_paths() -> set[str]:
    """Every `/api/…` or `/admin/…` path written in backticks in the README, query string dropped and
    placeholders (<slug>, <id>, pay_…) turned into a sample segment."""
    paths = set()
    for quoted in re.findall(r"`((?:GET |POST )?/(?:api|admin)[^`\s]*)", README):
        path = quoted.split(" ", 1)[-1].split("?")[0]
        if "…" in path.replace("pay_…", ""):  # "…/terminals/<id>/enable": a partial path, shown with its full form
            continue
        if path == "/api":  # the prefix named in prose ("the `/api` routes"), not a page
            continue
        path = re.sub(r"<[^>]+>|pay_…", "x", path).rstrip("/")
        paths.add(path)
    return paths


def _error_kinds(source: str, exception: str) -> set[str]:
    """The ``kind`` codes a module can raise with ``exception("<kind>", …)``."""
    return set(re.findall(rf'{exception}\(\s*"(\w+)"', source))


def _json_example(heading: str) -> object:
    [block] = [b for b in code_blocks(section(README, heading), "json")]
    return json.loads(block)


# --- Routes ----------------------------------------------------------------------------------------------------


def test_every_route_of_these_features_is_documented():
    undocumented = []
    for method, template in _routes():
        if not template.startswith(FEATURE_ROUTES):
            continue
        readme_form = re.sub(r"\{store_slug\}", "<slug>", template)
        readme_form = re.sub(r"\{\w+_id\}", "<id>", readme_form)
        tail = readme_form.split("<slug>", 1)[1]  # compare from the slug on ("…/terminals/<id>/enable")
        if readme_form not in README and tail not in README.replace("<payment id>", "<id>"):
            undocumented.append(f"{method} {readme_form}")
    assert not undocumented, f"routes missing from the README: {undocumented}"


def test_every_documented_path_exists():
    """A path left in the README after its route was removed (e.g. the old …/succeed API) fails here."""
    patterns = [_as_regex(template) for _, template in _routes()]
    missing = sorted(path for path in _documented_paths() if not any(p.match(path) for p in patterns))
    assert not missing, f"documented but not routes of the app: {missing}"
    assert len(_documented_paths()) > 20  # the scan really found the paths


def test_the_old_browser_callable_payment_routes_are_not_documented():
    assert not re.search(r"payments/<[^>]+>/(succeed|fail)`", README)
    assert "/payments/<id>/succeed" not in README


# --- Errors, statuses, request and response shapes ----------------------------------------------------------------


def test_quote_error_types_are_the_codes_the_quote_can_return():
    table = section(README, "`POST /api/stores/<slug>/shipping/quote`")
    documented = set(re.findall(r"`type` `(\w+)`", table))
    assert documented == _error_kinds(inspect.getsource(shipping), "ShippingQuoteError")


def test_payment_error_types_are_the_codes_the_api_can_return():
    table = section(README, "Terminal callback").split("Errors of the create / callback bodies", 1)[1]
    documented = {re.sub(r"[`()]", "", row[2]) for row in table_rows(table) if row[2].startswith("`")}
    raised = _error_kinds(inspect.getsource(payments), "PaymentError")
    # invalid_amount can't reach the API: the request model refuses such amounts first (FastAPI's 422).
    assert documented == raised - {"invalid_amount"}
    assert PaymentCreateIn.model_fields["amount_cents"].metadata  # …because the model bounds the amount


def test_documented_statuses_and_transitions_are_the_code():
    statuses = section(README, "Statuses")
    assert set(re.findall(r"\b(pending|succeeded|failed)\b", code_blocks(statuses)[0])) == {s.value for s in models.PaymentStatus}
    assert models.PAYMENT_TRANSITIONS[models.PaymentStatus.pending] == {models.PaymentStatus.succeeded,
                                                                       models.PaymentStatus.failed}
    assert all(not models.PAYMENT_TRANSITIONS[s] for s in (models.PaymentStatus.succeeded, models.PaymentStatus.failed))
    assert "`PaymentStatus` and `PAYMENT_TRANSITIONS` (`app/models.py`)" in statuses


def test_quote_request_table_lists_the_request_model_fields():
    rows = table_rows(section(README, "`POST /api/stores/<slug>/shipping/quote`").split("```", 1)[0])
    assert {row[0].strip("`") for row in rows} == set(ShippingQuoteIn.model_fields)


@pytest.mark.parametrize(
    "heading, model",
    [
        ("`POST /api/stores/<slug>/shipping/quote`", ShippingQuoteOut),
        ("`GET /api/stores/<slug>/payment/terminals`", PaymentTerminalOut),
        ("Terminal callback", PaymentOut),  # "Payment (every response)"
    ],
    ids=["quote", "terminals", "payment"],
)
def test_json_examples_have_exactly_the_response_fields(heading, model):
    example = _json_example(heading)
    for item in example if isinstance(example, list) else [example]:
        assert set(item) == set(model.model_fields), heading
        model.model_validate(item)  # and valid values


def test_shipping_methods_response_keys_are_the_model_fields():
    text = section(README, "`GET /api/stores/<slug>/shipping/methods`")
    keys = re.findall(r'"(\w+)"', text.split("[{", 1)[1].split("}]", 1)[0])
    assert keys == list(ShippingMethodOut.model_fields)


def test_request_bodies_shown_are_the_request_models():
    access = section(README, "Who may call what")
    create = json.loads(re.search(r"with `(\{[^`]+\})`", access).group(1))
    assert set(create) == set(PaymentCreateIn.model_fields) and PaymentCreateIn.model_validate(create)
    callback = section(README, "Terminal callback")
    body = json.loads(re.search(r'^(\{"payment".*\})$', callback, re.M).group(1).replace("pay_…", "pay_x"))
    assert set(body) == set(PaymentCallbackIn.model_fields) and PaymentCallbackIn.model_validate(body)


# --- Quoted numbers and names ------------------------------------------------------------------------------------------


def test_callback_rules_quote_the_constants():
    callback = section(README, "Terminal callback")
    assert f"{payment_callbacks.TIMESTAMP_HEADER}: <unix time in seconds>" in callback
    assert f"{payment_callbacks.SIGNATURE_HEADER}: {payment_callbacks.SIGNATURE_PREFIX}<hex HMAC-SHA256" in callback
    assert f"more than {payment_callbacks.TOLERANCE_SECONDS} seconds from now" in callback
    assert f"Body over {payment_callbacks.MAX_BODY_BYTES // 1024} KB" in callback
    reference_max = PaymentCallbackIn.model_fields["mock_reference"].metadata
    assert f"reference over {_max_length(reference_max)}" in callback


def _max_length(metadata) -> int:
    return next(m.max_length for m in metadata if getattr(m, "max_length", None))


def test_payment_columns_quote_the_constants():
    table = section(README, "Payments (mock)")
    sample = models.new_payment_public_id()
    random_part = len(sample) - len(models.PAYMENT_ID_PREFIX)
    assert f"`{models.PAYMENT_ID_PREFIX}` + {random_part} random characters ({random_part * 6} bits)" in table
    assert f"1 to {models.MAX_AMOUNT_CENTS:,}" in table
    assert f"up to {_max_length(PaymentCallbackIn.model_fields['mock_reference'].metadata)} characters" in table
    example_id = _json_example("Terminal callback")["id"]
    assert example_id.startswith(models.PAYMENT_ID_PREFIX) and len(example_id) == len(sample)


def test_settings_and_pages_quote_the_constants():
    assert f"at least {PAYMENT_CALLBACK_SECRET_MIN} characters" in section(README, "Settings")
    assert f"most recent first, {admin_payments.PAGE_SIZE} per page" in section(README, "Payments (admin list)")
    assert admin_imports.PAGE_SIZE == admin_payments.PAGE_SIZE  # "like the Imports list"
    assert "(`MAX_PAGE`, shared with the Imports list)" in README and MAX_PAGE > 1
    assert f"at most {terminal_admin.NAME_MAX} characters" in section(README, "Payment terminals (admin)")
    assert f"Required, at most {shipping_admin.NAME_MAX} characters" in section(README, "Shipping methods")
    assert admin_try_payment.REFERENCE_MAX == _max_length(PaymentCallbackIn.model_fields["mock_reference"].metadata)


def test_default_terminals_table_is_the_seeded_list():
    rows = table_rows(section(README, "Payment terminals").split("###", 1)[0])
    documented = [(row[0].strip("`"), row[1], int(row[2])) for row in rows if row[0].startswith("`mock")]
    assert documented == list(models.DEFAULT_PAYMENT_TERMINALS)


# --- Solares and the operator demo ------------------------------------------------------------------------------------


def test_solares_shipping_table_is_the_pack():
    currency = SOLARES["store"]["currency"]
    rows = table_rows(section(README, "Solares shipping methods").split("How they load", 1)[0])
    documented = {row[0].strip("`"): row for row in rows}
    assert set(documented) == {m["code"] for m in SOLARES["shipping_methods"]}
    for method in SOLARES["shipping_methods"]:
        row = documented[method["code"]]
        assert row[2] == format_price(method["rate_cents"], currency), method["code"]
        threshold = method.get("free_shipping_threshold_cents")
        assert row[3] == ("never" if threshold is None else f"subtotal ≥ {format_price(threshold, currency)}")
        assert row[4] == ", ".join(method["countries"])
        english = method["i18n"]["en"]["name"]  # "Standard shipping (3 to 7 business days)"
        assert row[1] == english.replace(" (", ", ").rstrip(")"), method["code"]


def test_operator_demo_amounts_follow_the_pack():
    demo = section(README, "Try payment (operator demo)")
    methods = {m["code"]: m for m in SOLARES["shipping_methods"]}
    currency = SOLARES["store"]["currency"]
    subtotal = int(re.search(r"subtotal `(\d+)` \(the gas boiler\)", demo).group(1)) * 100
    gas_boiler = next(p for p in SOLARES["products"] if p["slug"].startswith("boiler"))
    assert subtotal == gas_boiler["price_cents"]
    total = subtotal + methods["standard"]["rate_cents"]
    assert (f"{format_price(subtotal, currency)} + {format_price(methods['standard']['rate_cents'], currency)} = "
            f"total {format_price(total, currency)}") in demo
    assert f'Create payment of {format_price(total, currency)}' in demo
    assert f'"amount_cents": {total}, "currency": "{currency}"' in demo
    free_subtotal = int(re.search(r"Subtotal `(\d+)`: shipping is free", demo).group(1)) * 100
    assert free_subtotal >= methods["standard"]["free_shipping_threshold_cents"]
    assert f"the express\n   method: {format_price(methods['express']['rate_cents'], currency)}, never free" in demo
    assert methods["express"].get("free_shipping_threshold_cents") is None


def test_quote_curl_comment_is_the_real_quote():
    text = section(README, "Solares shipping methods")
    request = json.loads(re.search(r"-d '(\{\"method\": \"standard\"[^']+)'", text).group(1))
    standard = next(m for m in SOLARES["shipping_methods"] if m["code"] == request["method"])
    assert request["subtotal_cents"] < standard["free_shipping_threshold_cents"]
    assert f'"amount_cents": {standard["rate_cents"]}, "currency": "{SOLARES["store"]["currency"]}"' in text


# --- Tabs, schema table, file paths ---------------------------------------------------------------------------------


def test_documented_store_tabs_are_the_template_tabs():
    template = (PROJECT / "app" / "templates" / "admin" / "_store_tabs.html").read_text(encoding="utf-8")
    english = catalogs()["en"]
    tabs = [english[key] for key in re.findall(r'\{\{ t\("(admin\.tabs\.\w+)"\) \}\}', template)]
    documented = re.search(r"Each store in admin has tabs: \*\*(.+?)\*\*", README).group(1).split(" · ")
    assert documented == tabs


def test_schema_table_lists_every_table():
    rows = table_rows(section(README, "Schema (summary)"))
    assert {row[0].strip("`") for row in rows} == set(Base.metadata.tables)


def test_every_file_path_in_the_readme_exists():
    paths = set(re.findall(r"`((?:app|tests|fixtures|scripts)/[\w./-]+\.(?:py|html|json|css|md|sh))`", README))
    assert len(paths) > 20
    missing = sorted(p for p in paths if not (PROJECT / p).is_file())
    assert not missing, missing


# --- The callback recipe really signs ----------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("openssl") is None, reason="needs bash and openssl")
def test_the_documented_signing_recipe_produces_the_accepted_signature():
    [recipe] = [b for b in code_blocks(section(README, "Terminal callback"), "bash") if "openssl" in b]
    lines = [line for line in recipe.splitlines() if re.match(r"^(BODY|SIG)=", line)]
    assert len(lines) == 2  # BODY=… and SIG=$(… openssl …)
    secret, payment, timestamp = "s" * PAYMENT_CALLBACK_SECRET_MIN, "pay_abc", "1700000000"
    script = "\n".join([f"SECRET={secret}", f"PAYMENT={payment}", f"TS={timestamp}", *lines,
                        'printf "%s\\n%s" "$BODY" "$SIG"'])
    body, signature = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                                     timeout=30).stdout.split("\n")
    assert json.loads(body)["payment"] == payment
    assert payment_callbacks.SIGNATURE_PREFIX + signature == payment_callbacks.sign(secret, timestamp, body.encode())
