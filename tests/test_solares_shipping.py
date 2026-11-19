"""Solares ships its own shipping methods in its store pack (``shipping_methods``): a fresh database gets
them at the first start (SEED_PACKS), and any import of the pack (command line, upload, code) brings them.

Expected values (codes, names, rates, free-shipping amounts, countries) are read from the pack itself and UI
text from the JSON catalogs, so editing either doesn't break these tests; they check that the app shows what
the pack and the catalogs say. Names are store content in the store's language (Spanish), with English
translations in the pack's i18n."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models import PackImport, ShippingMethod, Store, Translation, TranslationEntity
from app.services import store_pack_import
from app.services.catalog import format_price
from app.services.store_pack_import import StorePackError, import_store_pack
from app.services.store_pack_validation import read_store_pack, validate_store_pack
from tests.ui_text import en, page_text

SOLARES = "fixtures/packs/solares/solares.store-pack.v1.json"
SOLARES_FILE = Path(__file__).resolve().parent.parent / SOLARES
METHODS = "/api/stores/solares/shipping/methods"
QUOTE = "/api/stores/solares/shipping/quote"

PACK = read_store_pack(SOLARES_FILE)
CURRENCY = PACK["store"]["currency"]
PACK_METHODS = {m["code"]: m for m in PACK["shipping_methods"]}
STANDARD, EXPRESS = PACK_METHODS["standard"], PACK_METHODS["express"]
# What GET …/shipping/methods must return: the pack's methods, in order, with the pack's defaults.
EXPECTED_METHODS = [
    {"code": m["code"], "name": m["name"], "sort_order": m.get("sort_order", 0), "rate_cents": m["rate_cents"],
     "free_shipping_threshold_cents": m.get("free_shipping_threshold_cents"), "countries": m.get("countries", [])}
    for m in sorted(PACK["shipping_methods"], key=lambda m: (m.get("sort_order", 0), m["name"]))
]
SPANISH = {code: m["name"] for code, m in PACK_METHODS.items()}  # as entered: the store's language
ENGLISH = {code: m["i18n"]["en"]["name"] for code, m in PACK_METHODS.items()}
COUNTRY = STANDARD["countries"][0]  # one the methods ship to
ABROAD = "US"
assert all(ABROAD not in m["countries"] for m in PACK_METHODS.values())  # the pack's methods are national


def _expected_quote(method: dict, subtotal_cents: int) -> tuple[int, bool]:
    """(amount_cents, free_shipping) the pack's rules give for this subtotal."""
    threshold = method.get("free_shipping_threshold_cents")
    free = threshold is not None and subtotal_cents >= threshold
    return (0 if free else method["rate_cents"]), free


@pytest.fixture()
def seed(monkeypatch):
    def set_seed(value: str) -> None:
        monkeypatch.setenv("SEED_PACKS", value)
        get_settings.cache_clear()

    yield set_seed
    get_settings.cache_clear()


def _start(engine) -> TestClient:
    """The app started against ``engine`` (its lifespan runs SEED_PACKS), like `uvicorn app.main:app`."""
    sessions = sessionmaker(bind=engine, autoflush=False)
    app = create_app()

    def override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def _quote(client, method: str, subtotal_cents: int, country: str = COUNTRY) -> dict:
    response = client.post(QUOTE, json={"method": method, "subtotal_cents": subtotal_cents, "country": country})
    assert response.status_code == 200, response.text
    return response.json()


def _check_solares_shipping(client) -> None:
    """GET methods returns the pack's codes, and quotes are in MXN with the pack's rules."""
    assert client.get(METHODS).json() == EXPECTED_METHODS

    threshold = STANDARD["free_shipping_threshold_cents"]  # the pack has a free-shipping amount on standard
    for method in (STANDARD, EXPRESS):
        for subtotal in (threshold - 1, threshold):  # just below / at standard's free-shipping amount
            quote = _quote(client, method["code"], subtotal)
            assert (quote["currency"], quote["amount_cents"], quote["free_shipping"]) == (
                CURRENCY, *_expected_quote(method, subtotal)), (method["code"], subtotal)
    assert _quote(client, "standard", threshold)["free_shipping"] is True  # the rule really applies

    abroad = client.post(QUOTE, json={"method": "express", "subtotal_cents": 100, "country": ABROAD})
    assert (abroad.status_code, abroad.json()["detail"][0]["type"]) == (422, "country_not_served")


# --- Requested: after seed / import, the methods are listed and a quote in MXN works ------------------------


def test_fresh_database_gets_the_methods_at_first_start(engine, seed, content_root, storefronts):
    seed(SOLARES)  # the default setting
    with _start(engine) as client:
        _check_solares_shipping(client)
        client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
        detail = page_text(client.get("/admin/imports/1?lang=en"))  # the startup import's record
        assert f"<tr><td>{en('admin.tabs.shipping')}</td><td>{len(PACK_METHODS)}</td><td>0</td></tr>" in detail
    with sessionmaker(bind=engine)() as db:
        record = db.scalar(select(PackImport))
        assert record.report["shipping_methods"] == {"created": len(PACK_METHODS), "updated": 0}


def test_importing_the_pack_brings_the_methods(client, db_session):
    report = import_store_pack(db_session, read_store_pack(SOLARES_FILE))  # what upload / CLI / seed call
    assert (report.shipping_methods.created, report.shipping_methods.updated) == (len(PACK_METHODS), 0)
    assert f"shipping methods: {len(PACK_METHODS)} created, 0 updated" in report.summary()
    _check_solares_shipping(client)


def test_operator_demo_uses_them_without_any_admin_setup(admin_client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    subtotal = 519900  # the gas boiler, below standard's free-shipping amount
    assert subtotal < STANDARD["free_shipping_threshold_cents"]
    page = admin_client.get("/admin/stores/solares/try-payment",
                            params={"subtotal": f"{subtotal / 100:.2f}", "method": "standard", "country": COUNTRY})
    assert page.status_code == 200
    html = page_text(page)
    assert format_price(STANDARD["rate_cents"], CURRENCY) in html
    assert format_price(subtotal + STANDARD["rate_cents"], CURRENCY) in html  # the total
    assert 'value="express"' in html  # both methods offered


# --- Existing databases and re-imports ------------------------------------------------------------------------


def test_solares_from_before_gets_them_with_the_documented_command(engine, seed, content_root, storefronts,
                                                                  capsys):
    """A database where solares was seeded before the pack had methods: restarting doesn't add them (later
    starts never change a seeded store); the documented re-import command does, and keeps admin changes
    to everything the pack doesn't name."""
    pack = read_store_pack(SOLARES_FILE)
    old_pack = copy.deepcopy(pack)
    del old_pack["shipping_methods"]
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        import_store_pack(db, old_pack)
        store = db.scalar(select(Store).where(Store.slug == "solares"))
        db.add(ShippingMethod(store_id=store.id, code="pickup", name="Warehouse pickup", rate_cents=0))
        db.commit()

    seed(SOLARES)
    with _start(engine) as client:
        assert [m["code"] for m in client.get(METHODS).json()] == ["pickup"]  # restart: unchanged

    assert store_pack_import.main([str(SOLARES_FILE)], sessions) == 0  # python -m app.services.store_pack_import
    assert f"shipping methods: {len(PACK_METHODS)} created, 0 updated" in capsys.readouterr().out
    with _start(engine) as client:
        codes = [m["code"] for m in client.get(METHODS).json()]
        assert sorted(codes) == ["express", "pickup", "standard"]  # the admin one is kept
        assert _quote(client, "standard", 100)["amount_cents"] == STANDARD["rate_cents"]


def test_reimport_overwrites_the_packs_methods_and_keeps_others(db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    standard = db_session.scalar(select(ShippingMethod).where(ShippingMethod.code == "standard"))
    standard_id = standard.id
    standard.rate_cents, standard.is_active = 1, False  # edited in admin
    store_id = standard.store_id
    db_session.add(ShippingMethod(store_id=store_id, code="pickup", name="Warehouse pickup", rate_cents=0))
    db_session.commit()

    report = import_store_pack(db_session, read_store_pack(SOLARES_FILE))

    assert (report.shipping_methods.created, report.shipping_methods.updated) == (0, len(PACK_METHODS))
    db_session.expire_all()
    methods = {m.code: m for m in db_session.scalars(select(ShippingMethod))}
    assert set(methods) == {"standard", "express", "pickup"}
    assert methods["standard"].id == standard_id  # updated in place
    assert (methods["standard"].rate_cents, methods["standard"].is_active) == (
        STANDARD["rate_cents"], STANDARD.get("is_active", True))  # the pack wins


def test_packs_without_shipping_methods_leave_them_alone(db_session):
    pack = read_store_pack(SOLARES_FILE)
    import_store_pack(db_session, pack)
    without = copy.deepcopy(pack)
    del without["shipping_methods"]
    report = import_store_pack(db_session, without)
    assert (report.shipping_methods.created, report.shipping_methods.updated) == (0, 0)
    assert "shipping methods" not in report.summary()
    assert len(db_session.scalars(select(ShippingMethod)).all()) == len(PACK_METHODS)


# --- The contract ---------------------------------------------------------------------------------------------


def _method(**changes):
    method = {"code": "standard", "name": "Standard", "rate_cents": 45000}
    method.update(changes)
    return {key: value for key, value in method.items() if value is not ...}


@pytest.mark.parametrize(
    "methods, path, message",
    [
        ("nope", "shipping_methods", "must be an array, got string"),
        ([_method(code=...)], "shipping_methods[0].code", "required"),
        ([_method(rate_cents=...)], "shipping_methods[0].rate_cents", "required"),
        ([_method(code="Express 24h")], "shipping_methods[0].code", "invalid slug"),
        ([_method(), _method(name="Other")], "shipping_methods[1].code", 'duplicate code "standard"'),
        ([_method(name=" ")], "shipping_methods[0].name", "must not be empty"),
        ([_method(rate_cents=4.5)], "shipping_methods[0].rate_cents", "must be a whole number of cents"),
        ([_method(rate_cents=True)], "shipping_methods[0].rate_cents", "must be a whole number of cents"),
        ([_method(rate_cents=-1)], "shipping_methods[0].rate_cents", "must be 0 to 2,147,483,647 cents"),
        ([_method(free_shipping_threshold_cents=2**31)], "shipping_methods[0].free_shipping_threshold_cents",
         "must be 0 to"),
        ([_method(countries="MX")], "shipping_methods[0].countries", "must be an array"),
        ([_method(countries=["mx"])], "shipping_methods[0].countries[0]", "2-letter upper-case country code"),
        ([_method(countries=["MX", 52])], "shipping_methods[0].countries[1]", "2-letter upper-case country code"),
        ([_method(countries=["MX", "MX"])], "shipping_methods[0].countries[1]", 'duplicate country "MX"'),
        ([_method(sort_order="1")], "shipping_methods[0].sort_order", "must be a whole number"),
        ([_method(is_active="yes")], "shipping_methods[0].is_active", "must be true or false"),
        ([_method(price=1)], "shipping_methods[0].price", "unknown field"),
        (["standard"], "shipping_methods[0]", "must be an object"),
        ([_method(i18n={"en": {"label": "x"}})], "shipping_methods[0].i18n.en.label", "unknown field"),
        ([_method(i18n={"en": {"name": ""}})], "shipping_methods[0].i18n.en.name", "must not be empty"),
    ],
)
def test_invalid_shipping_methods_are_rejected_with_their_location(db_session, methods, path, message):
    pack = read_store_pack(SOLARES_FILE)
    pack["shipping_methods"] = methods
    issues = [(issue.path, issue.message) for issue in validate_store_pack(pack)]
    assert any(p == path and message in m for p, m in issues), issues
    with pytest.raises(StorePackError):
        import_store_pack(db_session, pack)
    assert db_session.scalars(select(Store)).all() == []  # nothing written


def test_optional_fields_get_their_defaults(db_session):
    pack = read_store_pack(SOLARES_FILE)
    pack["shipping_methods"] = [_method(), _method(code="no-threshold", free_shipping_threshold_cents=None)]
    assert validate_store_pack(pack) == []
    import_store_pack(db_session, pack)
    method = db_session.scalar(select(ShippingMethod).where(ShippingMethod.code == "standard"))
    assert (method.free_shipping_threshold_cents, method.countries, method.sort_order, method.is_active) == (
        None, [], 0, True)


# --- English names: pack i18n, admin pages in the page language, Translations tab -------------------------------

def test_pack_brings_english_names_as_translations(db_session):
    report = import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    rows = db_session.execute(
        select(ShippingMethod.code, Translation.locale, Translation.text)
        .join(Translation, (Translation.entity_id == ShippingMethod.id)
              & (Translation.entity_type == TranslationEntity.shipping_method))
        .where(Translation.field == "name")
    ).all()
    assert sorted(rows) == [("express", "en", ENGLISH["express"]), ("standard", "en", ENGLISH["standard"])]
    assert report.translations_written >= 2


def test_admin_shipping_page_in_english_shows_english_names(admin_client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    page = page_text(admin_client.get("/admin/stores/solares/shipping"))  # client: ?lang=en
    for code in PACK_METHODS:
        assert f">{ENGLISH[code]}</a>" in page  # the label, in English
        assert f'<span class="meta" lang="es">{SPANISH[code]}</span>' in page  # the original, as a note
    edit_url = re.search(rf'href="(/admin/stores/solares/shipping/\d+)">{re.escape(ENGLISH["standard"])}',
                         page).group(1)
    form = page_text(admin_client.get(edit_url))
    assert f"<h1>{en('admin.shipping.heading', name=ENGLISH['standard'])}</h1>" in form
    assert f'name="name" value="{SPANISH["standard"]}"' in form  # the form edits the name as entered
    assert en("admin.shipping.hint.name", locale=PACK["store"]["locale_default"]) in form


def test_admin_shipping_page_in_spanish_shows_the_names_as_entered(default_lang_client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    default_lang_client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    page = page_text(default_lang_client.get("/admin/stores/solares/shipping"))
    assert f">{SPANISH['standard']}</a>" in page and ENGLISH["standard"] not in page
    assert 'class="meta" lang="es"' not in page  # no note: it is the original


def test_try_payment_in_english_uses_english_names(admin_client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    page = page_text(admin_client.get("/admin/stores/solares/try-payment",
                                      params={"subtotal": "5199", "method": "standard", "country": COUNTRY}))
    assert f">{ENGLISH['standard']} ({format_price(STANDARD['rate_cents'], CURRENCY)})</option>" in page
    assert en("admin.try_payment.shipping_row", method=ENGLISH["standard"]) in page
    assert not any(name in page for name in SPANISH.values())  # no Spanish labels on the English page


def test_translations_tab_lists_and_saves_shipping_names(admin_client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    tab = page_text(admin_client.get("/admin/stores/solares/translations?locale=en"))
    assert en("admin.translations.group.shipping") in tab and SPANISH["express"] in tab and ENGLISH["express"] in tab
    express_id = db_session.scalar(select(ShippingMethod.id).where(ShippingMethod.code == "express"))
    field = f"tr|shipping_method|{express_id}|name"
    saved = admin_client.post("/admin/stores/solares/translations",
                              data={"locale": "en", field: "Express delivery"}, follow_redirects=False)
    assert saved.status_code == 303
    assert ">Express delivery</a>" in admin_client.get("/admin/stores/solares/shipping").text


def test_api_keeps_the_names_as_entered(client, db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    names = [m["name"] for m in client.get(f"{METHODS}?lang=en").json()]
    assert names == [m["name"] for m in EXPECTED_METHODS]  # as entered, like every JSON API content


def test_deleting_a_method_deletes_its_translations(db_session):
    import_store_pack(db_session, read_store_pack(SOLARES_FILE))
    db_session.delete(db_session.scalar(select(ShippingMethod).where(ShippingMethod.code == "express")))
    db_session.commit()
    texts = db_session.scalars(select(Translation.text).where(
        Translation.entity_type == TranslationEntity.shipping_method)).all()
    assert texts == [ENGLISH["standard"]]
