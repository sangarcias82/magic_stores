"""What a shopper browsing in another language than the store's sees at checkout: shipping method and
payment terminal names, product names in the cart and the order, the bank transfer note (content
translations, level 2: app/services/translations.py). Solares is Spanish with English translations."""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import PaymentTerminal, Store, Translation, TranslationEntity
from app.services import payments, translations
from app.services.pack_seed import fill_terminal_translations
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack, validate_store_pack
from tests.ui_text import en, es, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
PACK_BANK = next(t for t in SOLARES["payment_terminals"] if t["code"] == "bank_transfer")
ENGLISH_NOTE = PACK_BANK["i18n"]["en"]["note"]
METHODS = {m["code"]: m for m in SOLARES["shipping_methods"]}
EIGHT = next(p for p in SOLARES["products"] if p.get("sku") == "SOL-8T-ALTA")
API = "/api/stores/solares"
BODY = {"shipping_method": "standard", "country": "MX", "terminal": "bank_transfer"}


@pytest.fixture()
def solares(db_session) -> Store:
    import_store_pack(db_session, SOLARES)
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


def _config(html: str) -> dict:
    return json.loads(re.search(r'id="cart-config">(.*?)</script>', html, re.S).group(1))


def _terminal(db_session, code: str = "bank_transfer") -> PaymentTerminal:
    db_session.expire_all()
    return db_session.scalar(select(PaymentTerminal).where(PaymentTerminal.code == code))


def _terminal_translations(db_session, terminal: PaymentTerminal) -> dict[tuple[str, str], str]:
    db_session.expire_all()
    return {(t.locale, t.field): t.text for t in db_session.scalars(select(Translation).where(
        Translation.entity_type == TranslationEntity.payment_terminal, Translation.entity_id == terminal.id))}


def _order(client) -> dict:
    assert client.post(f"{API}/cart/lines", json={"sku": "SOL-8T-ALTA"}).status_code == 200
    response = client.post(f"{API}/cart/checkout", json=BODY)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------------- the cart page


def test_checkout_choices_are_in_english_on_an_english_page(client, solares):
    config = _config(client.get("/stores/solares/cart").text)  # client: ?lang=en
    assert config["lang"] == "en"
    assert config["names"]["shipping"] == {code: m["i18n"]["en"]["name"] for code, m in METHODS.items()}
    assert config["names"]["terminals"]["bank_transfer"] == "Bank transfer"
    assert config["names"]["terminals"]["mock_card"] == "Mock card"  # no translation: as entered


def test_and_in_spanish_by_default(default_lang_client, solares):
    config = _config(default_lang_client.get("/stores/solares/cart").text)
    assert config["lang"] == "es"
    assert config["names"]["shipping"]["standard"] == METHODS["standard"]["name"]  # "Envío estándar (…)"
    assert config["names"]["terminals"]["bank_transfer"] == "Transferencia bancaria"


def test_other_pages_have_no_checkout_names(client, solares):
    assert _config(client.get("/stores/solares").text)["names"] == {}


# ------------------------------------------------------------------ the JSON answers carry the translations


def test_cart_and_order_lines_carry_the_name_translations(client, solares):
    body = _order(client)
    [line] = body["order"]["lines"]
    assert (line["name"], line["name_translations"]) == (EIGHT["name"], {"en": EIGHT["i18n"]["en"]["name"]})
    assert body["payment"]["instructions"]["note"] == PACK_BANK["config"]["note"]  # as entered (Spanish)
    assert body["payment"]["instructions"]["note_translations"] == {"en": ENGLISH_NOTE}


def test_the_note_translation_is_a_copy(client, db_session, solares):
    body = _order(client)
    translations.set_content_translation(db_session, _terminal(db_session), "en", "note", "A new note.")
    seen = client.get(f"{API}/orders/{body['order']['id']}").json()["payment"]["instructions"]
    assert seen["note_translations"] == {"en": ENGLISH_NOTE}  # what that shopper was told
    payment = payments.create_payment(db_session, db_session.scalar(select(Store)), "bank_transfer", 1000)
    assert payment.instructions["note_translations"] == {"en": "A new note."}  # new payments: the new one


def test_no_note_no_note_translations(db_session, solares):
    terminal = _terminal(db_session)
    terminal.config = {**terminal.config, "note": ""}
    db_session.commit()
    payment = payments.create_payment(db_session, db_session.scalar(select(Store)), "bank_transfer", 1000)
    assert payment.instructions["note_translations"] == {}


# ------------------------------------------------------------------------- the order page


def test_order_page_in_english(client, solares):
    body = _order(client)
    html = page_text(client.get(f"/stores/solares/orders/{body['order']['id']}"))
    assert f'<p class="order-transfer-note">{ENGLISH_NOTE}</p>' in html
    assert en("order.shipping", name=METHODS["standard"]["i18n"]["en"]["name"]) in html
    assert EIGHT["i18n"]["en"]["name"] in html
    assert PACK_BANK["config"]["note"] not in html and METHODS["standard"]["name"] not in html


def test_order_page_in_spanish(default_lang_client, solares):
    body = _order(default_lang_client)
    html = page_text(default_lang_client.get(f"/stores/solares/orders/{body['order']['id']}"))
    assert f'<p class="order-transfer-note">{PACK_BANK["config"]["note"]}</p>' in html
    assert es("order.shipping", name=METHODS["standard"]["name"]) in html
    assert EIGHT["name"] in html and ENGLISH_NOTE not in html


def test_a_deleted_method_shows_the_name_the_order_kept(client, db_session, solares):
    body = _order(client)
    from app.models import ShippingMethod

    db_session.delete(db_session.scalar(select(ShippingMethod).where(ShippingMethod.code == "standard")))
    db_session.commit()
    html = page_text(client.get(f"/stores/solares/orders/{body['order']['id']}"))
    assert en("order.shipping", name=METHODS["standard"]["name"]) in html


# ------------------------------------------------------------------ translations tab (admin)


def test_the_translations_tab_has_the_terminal_name_and_note(admin_client, db_session, solares):
    page = page_text(admin_client.get("/admin/stores/solares/translations", params={"locale": "en"}))
    assert en("admin.translations.group.terminal") in page and en("admin.translations.field.note") in page
    assert "Bank transfer" in page and ENGLISH_NOTE in page
    fields = {(f.entity_type, f.entity.code, f.field) for f in translations.translatable_fields(
        db_session, db_session.scalar(select(Store))) if f.entity_type == TranslationEntity.payment_terminal}
    assert fields == {(TranslationEntity.payment_terminal, "bank_transfer", "name"),
                      (TranslationEntity.payment_terminal, "bank_transfer", "note"),
                      (TranslationEntity.payment_terminal, "mock_card", "name"),
                      (TranslationEntity.payment_terminal, "mock_transfer", "name")}  # mocks have no note


def test_a_note_translation_is_limited_like_the_note(db_session, solares):
    with pytest.raises(translations.TranslationError):
        translations.set_content_translation(db_session, _terminal(db_session), "en", "note", "n" * 501)
    translations.set_content_translation(db_session, _terminal(db_session), "en", "note", "n" * 500)


# ------------------------------------------------------------------------------ the pack


def test_import_writes_the_terminal_translations(db_session, solares):
    assert _terminal_translations(db_session, _terminal(db_session)) == {
        ("en", "name"): "Bank transfer", ("en", "note"): ENGLISH_NOTE}


def test_reimport_replaces_the_name_but_keeps_the_note_with_the_details(db_session, solares):
    terminal = _terminal(db_session)
    translations.set_content_translation(db_session, terminal, "en", "name", "Wire")
    translations.set_content_translation(db_session, terminal, "en", "note", "Our own English note.")
    translations.set_content_translation(db_session, terminal, "pt", "name", "Transferência")

    import_store_pack(db_session, SOLARES)

    assert _terminal_translations(db_session, _terminal(db_session)) == {
        ("en", "name"): "Bank transfer",  # the pack's names replace the name translations
        ("en", "note"): "Our own English note.",  # the config is kept, so is the note's translation
    }


def test_a_terminal_getting_its_details_gets_the_note_translations(db_session, solares):
    terminal = _terminal(db_session)
    terminal.config = None
    translations.set_content_translation(db_session, terminal, "en", "note", "Old note.")
    import_store_pack(db_session, SOLARES)
    assert _terminal_translations(db_session, _terminal(db_session))[("en", "note")] == ENGLISH_NOTE


def _pack_terminal_issues(i18n, kind="bank_transfer") -> list[tuple[str, str]]:
    pack = copy.deepcopy(SOLARES)
    terminal = pack["payment_terminals"][0 if kind == "bank_transfer" else 1]
    terminal["i18n"] = i18n
    return [(i.path, i.message) for i in validate_store_pack(pack)]


@pytest.mark.parametrize("i18n, kind, issue", [
    ({"en": {"name": "Card", "note": "x"}}, "mock", ("payment_terminals[1].i18n.en.note",
                                                     "only a bank_transfer terminal has a note")),
    ({"en": {"name": "Bank", "note": "n" * 501}}, "bank_transfer",
     ("payment_terminals[0].i18n.en.note", "must be at most 500 characters (got 501)")),
    ({"en": {"name": "Bank", "note": " "}}, "bank_transfer", ("payment_terminals[0].i18n.en.note", "must not be empty")),
    ({"en": {"name": " "}}, "bank_transfer", ("payment_terminals[0].i18n.en.name", "must not be empty")),
    ({"en": {"name": "Bank", "label": "x"}}, "bank_transfer",
     ("payment_terminals[0].i18n.en.label", "unknown field (allowed: name, note)")),
])
def test_pack_terminal_i18n_errors(i18n, kind, issue):
    assert issue in _pack_terminal_issues(i18n, kind)


# --------------------------------------------------------------- older databases (startup)


def _forget_translations(db_session, terminal):
    for row in db_session.scalars(select(Translation).where(Translation.entity_type == TranslationEntity.payment_terminal,
                                                            Translation.entity_id == terminal.id)):
        db_session.delete(row)
    db_session.commit()


def test_startup_fills_the_missing_translations(engine, db_session, solares, monkeypatch, caplog):
    """A database seeded with bank_transfer before it had translations gets the English name and note."""
    terminal = _terminal(db_session)
    _forget_translations(db_session, terminal)
    monkeypatch.setenv("SEED_PACKS", "fixtures/packs/solares/solares.store-pack.v1.json")
    get_settings.cache_clear()
    sessions = sessionmaker(bind=engine, autoflush=False)
    app = create_app()

    def override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        with caplog.at_level(logging.INFO, logger="app.services.pack_seed"), TestClient(app) as started:
            config = _config(started.get("/stores/solares/cart?lang=en").text)
    finally:
        get_settings.cache_clear()
    assert config["names"]["terminals"]["bank_transfer"] == "Bank transfer"
    assert "got 2 missing payment terminal translation(s)" in caplog.text
    assert _terminal_translations(db_session, terminal) == {("en", "name"): "Bank transfer", ("en", "note"): ENGLISH_NOTE}


def test_filling_never_replaces_and_skips_edited_texts(db_session, solares):
    store = db_session.scalar(select(Store))
    terminal = _terminal(db_session)
    _forget_translations(db_session, terminal)
    terminal.name = "Transferencia SPEI"  # renamed in admin: the pack's English name no longer fits
    db_session.commit()
    assert fill_terminal_translations(db_session, store, SOLARES) == 1  # only the note (still the pack's)
    assert _terminal_translations(db_session, terminal) == {("en", "note"): ENGLISH_NOTE}

    translations.set_content_translation(db_session, _terminal(db_session), "en", "note", "Mine.")
    assert fill_terminal_translations(db_session, store, SOLARES) == 0  # never replaced
    assert _terminal_translations(db_session, terminal)[("en", "note")] == "Mine."
    pack = copy.deepcopy(SOLARES)
    pack["payment_terminals"] = [{**PACK_BANK, "code": "not_in_the_store"}]
    assert fill_terminal_translations(db_session, store, pack) == 0
