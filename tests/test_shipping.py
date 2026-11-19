"""Shipping methods per store and flat-rate quotes (app/services/shipping.py, app/routers/shipping.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ShippingMethod, Store

QUOTE_URL = "/api/stores/{slug}/shipping/quote"


@pytest.fixture()
def make_store(db_session):
    """Factory: a bare store (no catalog needed to quote shipping)."""

    def make(slug: str = "ship-demo", currency: str = "EUR") -> Store:
        store = Store(slug=slug, name=slug.title(), currency=currency)
        db_session.add(store)
        db_session.commit()
        return store

    return make


@pytest.fixture()
def make_method(db_session):
    """Factory: a shipping method of ``store``; keyword arguments override the defaults."""

    def make(store: Store, code: str = "standard", **fields) -> ShippingMethod:
        values = {"name": code.title(), "rate_cents": 500, "is_active": True, "sort_order": 0, **fields}
        method = ShippingMethod(store_id=store.id, code=code, **values)
        db_session.add(method)
        db_session.commit()
        return method

    return make


def _quote(client, slug: str, **body):
    return client.post(QUOTE_URL.format(slug=slug), json=body)


def _error_type(response) -> str:
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    return error["type"]


# --- The three required cases --------------------------------------------------------------


def test_happy_quote(client, make_store, make_method):
    store = make_store()
    make_method(store, "standard", name="Standard", rate_cents=495)

    response = _quote(client, store.slug, method="standard", subtotal_cents=2000, country="es")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "store": "ship-demo",
        "currency": "EUR",
        "method": {"code": "standard", "name": "Standard"},
        "country": "ES",
        "subtotal_cents": 2000,
        "amount_cents": 495,
        "free_shipping": False,
    }


def test_unknown_method_rejected(client, make_store, make_method):
    store = make_store()
    make_method(store, "standard")

    response = _quote(client, store.slug, method="teleport", subtotal_cents=2000)

    assert _error_type(response) == "unknown_method"
    assert response.json()["detail"][0]["loc"] == ["body", "method"]


def test_inactive_method_rejected(client, make_store, make_method):
    store = make_store()
    make_method(store, "express", is_active=False)

    response = _quote(client, store.slug, method="express", subtotal_cents=2000)

    assert _error_type(response) == "inactive_method"
    assert response.json()["detail"][0]["loc"] == ["body", "method"]


# --- Rules around them -----------------------------------------------------------------------


def test_free_shipping_from_threshold(client, make_store, make_method):
    store = make_store()
    make_method(store, "standard", rate_cents=500, free_shipping_threshold_cents=5000)

    below = _quote(client, store.slug, method="standard", subtotal_cents=4999).json()
    at = _quote(client, store.slug, method="standard", subtotal_cents=5000).json()
    without_subtotal = _quote(client, store.slug, method="standard").json()

    assert (below["amount_cents"], below["free_shipping"]) == (500, False)
    assert (at["amount_cents"], at["free_shipping"]) == (0, True)
    assert (without_subtotal["amount_cents"], without_subtotal["subtotal_cents"]) == (500, None)


def test_country_restriction(client, make_store, make_method):
    store = make_store()
    make_method(store, "peninsula", countries=["ES", "PT"])

    assert _quote(client, store.slug, method="peninsula", country="pt").json()["amount_cents"] == 500
    assert _error_type(_quote(client, store.slug, method="peninsula", country="FR")) == "country_not_served"
    assert _error_type(_quote(client, store.slug, method="peninsula")) == "country_required"


def test_method_of_another_store_is_unknown(client, make_store, make_method):
    mine, other = make_store("mine"), make_store("other")
    make_method(other, "pickup")
    make_method(mine, "standard")

    assert _error_type(_quote(client, "mine", method="pickup")) == "unknown_method"


def test_unknown_store_is_404(client):
    assert _quote(client, "nope", method="standard").status_code == 404
    assert client.get("/api/stores/nope/shipping/methods").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},  # method missing
        {"method": ""},
        {"method": "standard", "subtotal_cents": -1},
        {"method": "standard", "country": "ESP"},
    ],
)
def test_malformed_body_is_422(client, make_store, make_method, body):
    store = make_store()
    make_method(store, "standard")
    assert client.post(QUOTE_URL.format(slug=store.slug), json=body).status_code == 422


def test_methods_list_active_only_in_sort_order(client, make_store, make_method):
    store = make_store()
    make_method(store, "express", name="Express", sort_order=2, rate_cents=1200)
    make_method(store, "standard", name="Standard", sort_order=1, free_shipping_threshold_cents=5000)
    make_method(store, "courier", name="Courier", sort_order=0, is_active=False)

    response = client.get(f"/api/stores/{store.slug}/shipping/methods")

    assert response.status_code == 200
    assert response.json() == [
        {"code": "standard", "name": "Standard", "sort_order": 1, "rate_cents": 500,
         "free_shipping_threshold_cents": 5000, "countries": []},
        {"code": "express", "name": "Express", "sort_order": 2, "rate_cents": 1200,
         "free_shipping_threshold_cents": None, "countries": []},
    ]


def test_deleting_a_store_deletes_its_methods(db_session, make_store, make_method):
    store = make_store()
    make_method(store, "standard")

    db_session.delete(store)
    db_session.commit()

    assert db_session.scalars(select(ShippingMethod)).all() == []
