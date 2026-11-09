from __future__ import annotations

from pathlib import Path

import app.services.storefront as sf


def test_default_static_resolves():
    path = sf.resolve_static_file("any-missing-store", "style.css")
    assert path is not None
    assert path.name == "style.css"
    assert "_default" in str(path)


def test_store_override_static(tmp_path, monkeypatch):
    root = tmp_path / "storefronts"
    default = root / "_default" / "static"
    default.mkdir(parents=True)
    (default / "style.css").write_text("body{color:black}", encoding="utf-8")

    store = root / "acme" / "static"
    store.mkdir(parents=True)
    (store / "style.css").write_text("body{color:red}", encoding="utf-8")

    monkeypatch.setattr(sf, "STOREFRONTS_ROOT", root)

    path = sf.resolve_static_file("acme", "style.css")
    assert path is not None
    assert path.read_text(encoding="utf-8") == "body{color:red}"

    path_default = sf.resolve_static_file("other", "style.css")
    assert path_default is not None
    assert path_default.read_text(encoding="utf-8") == "body{color:black}"


def test_template_choice_prefers_store(tmp_path, monkeypatch, client, clothing_fixture):
    root = Path(sf.STOREFRONTS_ROOT)
    override = root / "clothing-demo" / "templates"
    override.mkdir(parents=True, exist_ok=True)
    (override / "store.html").write_text(
        "{% extends 'base.html' %}\n{% block content %}"
        "<h1 data-testid='override'>OVERRIDE {{ store.name }}</h1>"
        "{% endblock %}",
        encoding="utf-8",
    )
    sf.templates_for.cache_clear()
    try:
        response = client.get("/stores/clothing-demo")
        assert response.status_code == 200
        assert "OVERRIDE" in response.text
        assert "Demo Clothing" in response.text
        assert "data-testid=" in response.text
    finally:
        for path in sorted(override.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        store_root = root / "clothing-demo"
        if store_root.exists():
            for path in sorted(store_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            if store_root.exists():
                store_root.rmdir()
        sf.templates_for.cache_clear()


def test_custom_page_from_default_then_store(tmp_path, monkeypatch, client, clothing_fixture):
    root = Path(sf.STOREFRONTS_ROOT)
    default_page = root / "_default" / "pages" / "notice.html"
    default_page.write_text(
        "<html><body><h1>Default notice {{ store.name }}</h1></body></html>",
        encoding="utf-8",
    )
    try:
        response = client.get("/stores/clothing-demo/pages/notice")
        assert response.status_code == 200
        assert "Default notice Demo Clothing" in response.text

        store_pages = root / "clothing-demo" / "pages"
        store_pages.mkdir(parents=True, exist_ok=True)
        (store_pages / "notice.html").write_text(
            "<html><body><h1>Store notice</h1></body></html>",
            encoding="utf-8",
        )
        response = client.get("/stores/clothing-demo/pages/notice")
        assert response.status_code == 200
        assert "Store notice" in response.text
    finally:
        if default_page.exists():
            default_page.unlink()
        store_root = root / "clothing-demo"
        if store_root.exists():
            for path in sorted(store_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            if store_root.exists():
                store_root.rmdir()
