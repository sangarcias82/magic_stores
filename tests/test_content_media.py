"""CONTENT_BASE_URL + media_url: image values that are content-app paths render with that base."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.services.media import media_url

BASE = "https://media.example.test"  # not the default, so the tests prove the setting is used
PRODUCTS = "/admin/stores/demo-widgets/products"
PUBLIC = "/stores/demo-widgets/p/widget-blue-m"


@pytest.fixture()
def content_base(monkeypatch):
    """CONTENT_BASE_URL set (with a trailing slash, which is dropped) for one test."""
    monkeypatch.setenv("CONTENT_BASE_URL", BASE + "/")
    get_settings.cache_clear()
    yield BASE
    monkeypatch.undo()
    get_settings.cache_clear()  # back to the pinned test settings


# --------------------------------------------------------------------------- helper


def test_content_path_gets_the_configured_base(content_base):
    assert get_settings().content_base_url == BASE
    assert media_url("/content/demo-widgets/widget-blue.svg") == f"{BASE}/content/demo-widgets/widget-blue.svg"


@pytest.mark.parametrize(
    "value",
    [
        "https://cdn.example.com/w.png",  # absolute URLs pass through unchanged
        "http://cdn.example.com/content/w.png",
        "/static/widget-blue.png",  # this site's own paths stay on this site
        "/stores/demo-widgets/assets/logo.png",
        "/contentious.png",  # only the /content/ folder prefix counts
    ],
)
def test_other_values_pass_through(content_base, value):
    assert media_url(value) == value


def test_empty_values_and_explicit_base():
    assert media_url(None) == ""
    assert media_url("") == ""
    assert media_url("/content/s/a.jpg", base_url="http://h:9/") == "http://h:9/content/s/a.jpg"
    assert media_url("/content/s/a.jpg", base_url="") == "/content/s/a.jpg"  # empty base: relative


def test_default_base_is_the_local_content_app():
    assert Settings(_env_file=None).content_base_url == "http://127.0.0.1:8001"


@pytest.mark.parametrize("value", ["127.0.0.1:8001", "ftp://h", "javascript:alert(1)", "http://", "http://h/?x=1"])
def test_invalid_base_is_rejected(monkeypatch, value):
    monkeypatch.setenv("CONTENT_BASE_URL", value)
    with pytest.raises(ValidationError, match="CONTENT_BASE_URL"):
        Settings(_env_file=None)


def test_empty_base_is_allowed(monkeypatch):
    monkeypatch.setenv("CONTENT_BASE_URL", "  ")
    assert Settings(_env_file=None).content_base_url == ""


# ----------------------------------------------------------------- product page <img>


def _product_img(client, category, photo: str) -> str:
    data = {
        "category_id": str(category.id),
        "slug": "widget-blue-m",
        "sku": "WB-100-M", "stock": "12",
        "name": "Widget Blue M",
        "price": "19.99",
        "prop_color": "Blue",
        "prop_size": "M",
        "prop_photo": photo,
    }
    response = client.post(PRODUCTS, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    page = client.get(PUBLIC)
    assert page.status_code == 200
    match = re.search(r'data-key="photo">\s*(<img [^>]*>)', page.text)
    assert match, page.text
    return match.group(1)


def test_product_page_img_uses_content_base(admin_client, widgets_category, content_base):
    img = _product_img(admin_client, widgets_category, "/content/demo-widgets/widget-blue.svg")
    assert img == f'<img src="{BASE}/content/demo-widgets/widget-blue.svg" alt="Photo" class="prop-image">'


def test_product_page_img_absolute_url_unchanged(admin_client, widgets_category, content_base):
    img = _product_img(admin_client, widgets_category, "https://cdn.example.com/w.png")
    assert img == '<img src="https://cdn.example.com/w.png" alt="Photo" class="prop-image">'


def test_product_page_img_site_path_unchanged(admin_client, widgets_category, content_base):
    img = _product_img(admin_client, widgets_category, "/static/widget-blue.png")
    assert img == '<img src="/static/widget-blue.png" alt="Photo" class="prop-image">'
