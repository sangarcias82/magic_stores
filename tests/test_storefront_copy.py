"""storefront.copy_from_pack_dir: a pack imported from its folder copies that folder to
app/storefronts/<store-slug>/, and the public pages use it; stores without one use _default.

Every test that copies uses the ``storefronts`` fixture (conftest.py): STOREFRONTS_ROOT points at
a temporary folder with a copy of the real _default, so the real app/storefronts/ is never written to."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.services.storefront as sf
from app.models import Store
from app.services import store_pack_import
from app.services.store_pack_import import (
    STOREFRONT_NOT_COPIED,
    StorePackImportError,
    import_store_pack,
    import_store_pack_file,
)
from app.services.storefront_install import StorefrontCopyError, destination, stage_storefront
from tests.ui_text import en_html

PROJECT = Path(__file__).resolve().parent.parent
PACKS_DIR = PROJECT / "fixtures" / "packs"
DEMO_DIR = PACKS_DIR / "storefront-demo"
DEMO = DEMO_DIR / "storefront-demo.store-pack.v1.json"
DEMO_SOURCE = DEMO_DIR / "storefront"
CLOTHING = PACKS_DIR / "clothing-demo.store-pack.v1.json"  # no "storefront" field
WIDGETS = PACKS_DIR / "demo-widgets.store-pack.v1.json"  # "copy_from_pack_dir": null
REAL_ROOT = sf.STOREFRONTS_ROOT
MARKER = 'data-storefront="pack-override"'


def _demo() -> dict:
    return json.loads(DEMO.read_text(encoding="utf-8"))


def _entries(root: Path) -> set[str]:
    """Names in the storefronts root: no staging / backup folders may be left behind."""
    return {p.name for p in root.iterdir()}


def _stores(db) -> int:
    return db.scalar(select(func.count()).select_from(Store))


# ------------------------------------------------------------------------- happy path


def test_destination_is_app_storefronts_slug():
    assert REAL_ROOT == PROJECT / "app" / "storefronts"
    assert destination("storefront-demo") == PROJECT / "app" / "storefronts" / "storefront-demo"


def test_pack_storefront_lands_under_the_store_folder_and_pages_use_it(client, db_session, storefronts):
    report = import_store_pack_file(db_session, DEMO)

    dest = storefronts / "storefront-demo"
    assert report.storefront_path == str(dest) and report.storefront_files == 2
    assert report.skipped == []  # copied: not reported as skipped
    assert f"storefront: 2 file(s) copied to {dest}" in report.summary().splitlines()
    assert "skipped" not in report.summary()
    for rel in ("templates/store.html", "static/extra.css"):
        assert (dest / rel).read_bytes() == (DEMO_SOURCE / rel).read_bytes()
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "storefront-demo"}

    home = client.get("/stores/storefront-demo")
    assert home.status_code == 200
    assert MARKER in home.text and "Storefront Demo: pack storefront" in home.text
    assert '<a href="/stores/storefront-demo/p/tiny-gadget">Tiny gadget</a>' in home.text
    assert '<link rel="stylesheet" href="/stores/storefront-demo/assets/extra.css">' in home.text

    css = client.get("/stores/storefront-demo/assets/extra.css")  # static file of the pack
    assert css.status_code == 200 and css.content == (DEMO_SOURCE / "static" / "extra.css").read_bytes()
    style = client.get("/stores/storefront-demo/assets/style.css")  # not in the pack: _default
    assert style.content == (storefronts / sf.DEFAULT_KEY / "static" / "style.css").read_bytes()

    product = client.get("/stores/storefront-demo/p/tiny-gadget")  # product.html not overridden
    assert product.status_code == 200 and "<h1>Tiny gadget</h1>" in product.text and MARKER not in product.text


def test_store_without_override_still_uses_default(client, db_session, storefronts):
    import_store_pack_file(db_session, DEMO)
    import_store_pack_file(db_session, CLOTHING)  # no storefront field
    import_store_pack_file(db_session, WIDGETS)  # copy_from_pack_dir: null
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "storefront-demo"}  # no folder for the others

    for slug in ("clothing-demo", "demo-widgets"):
        html = client.get(f"/stores/{slug}").text
        assert MARKER not in html and "extra.css" not in html
        assert '<section class="grid-two">' in html  # _default/templates/store.html
    assert MARKER in client.get("/stores/storefront-demo").text


def test_copy_is_picked_up_without_restart(client, db_session, storefronts):
    # The store page is rendered (and its templates cached) before the folder exists.
    import_store_pack(db_session, _demo())  # no pack folder: nothing copied
    assert MARKER not in client.get("/stores/storefront-demo").text
    import_store_pack_file(db_session, DEMO)  # e.g. the command line, while the app runs
    assert MARKER in client.get("/stores/storefront-demo").text


def test_reimport_replaces_the_previous_copy(client, db_session, storefronts):
    import_store_pack_file(db_session, DEMO)
    stale = storefronts / "storefront-demo" / "templates" / "product.html"  # not in the pack (any more)
    stale.write_text("{% extends 'base.html' %}{% block content %}STALE{% endblock %}", encoding="utf-8")
    assert "STALE" in client.get("/stores/storefront-demo/p/tiny-gadget").text

    report = import_store_pack_file(db_session, DEMO)
    assert report.storefront_files == 2 and not stale.exists()
    assert "STALE" not in client.get("/stores/storefront-demo/p/tiny-gadget").text
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "storefront-demo"}


def test_null_folder_leaves_an_existing_store_folder_alone(client, db_session, storefronts):
    import_store_pack_file(db_session, DEMO)
    pack = _demo()
    pack["storefront"]["copy_from_pack_dir"] = None
    report = import_store_pack(db_session, pack, pack_dir=DEMO_DIR)
    assert report.storefront_files is None and report.skipped == []
    assert MARKER in client.get("/stores/storefront-demo").text  # still there, still used


def test_command_line_copies_and_records_it(engine, storefronts, capsys):
    assert store_pack_import.main([str(DEMO)], sessionmaker(bind=engine, autoflush=False)) == 0
    out = capsys.readouterr().out
    assert f"  storefront: 2 file(s) copied to {storefronts / 'storefront-demo'}" in out
    assert "skipped" not in out
    assert (storefronts / "storefront-demo" / "templates" / "store.html").is_file()

    from app.models import PackImport

    record = sessionmaker(bind=engine)().scalar(select(PackImport))
    assert record.report["storefront_files"] == 2 and record.report["skipped"] == []


def test_upload_has_no_folder_so_it_is_reported_as_skipped(admin_client, db_session, storefronts):
    response = admin_client.post("/admin/packs", files={"pack": ("p.json", DEMO.read_bytes(), "application/json")},
                                 follow_redirects=True)
    assert response.status_code == 200
    assert en_html("flash.pack_storefront_skipped") in response.text
    assert _entries(storefronts) == {sf.DEFAULT_KEY}  # nothing copied
    assert MARKER not in admin_client.get("/stores/storefront-demo").text


# ------------------------------------------------------------------------- path safety


def _pack_dir(tmp_path: Path) -> Path:
    pack_dir = tmp_path / "pack"
    (pack_dir / "storefront" / "templates").mkdir(parents=True)
    (pack_dir / "storefront" / "templates" / "store.html").write_text("ok", encoding="utf-8")
    return pack_dir


@pytest.mark.parametrize(
    ("folder", "message"),
    [
        ("../outside", '("..")'),
        ("storefront/../../outside", '("..")'),
        ("/etc", "relative folder"),
        ("~/x", "relative folder"),
        ("storefront\\templates", "relative folder"),
        ("c:/windows", "relative folder"),
        (".", "not the pack folder itself"),
        ("./", "not the pack folder itself"),
        ("", "folder path inside the pack"),
        ("missing/", "folder not found"),
    ],
)
def test_unsafe_or_missing_folders_are_refused(tmp_path, storefronts, folder, message):
    # Checked by the installer itself, not only by the pack validator.
    with pytest.raises(StorefrontCopyError, match=re.escape(message)):
        stage_storefront(_pack_dir(tmp_path), folder, "acme")
    assert _entries(storefronts) == {sf.DEFAULT_KEY}


@pytest.mark.parametrize("slug", ["../escape", "a/b", "_default", "Acme", "acme\n", "", "x" * 65])
def test_destination_must_be_a_store_slug(tmp_path, storefronts, slug):
    with pytest.raises(StorefrontCopyError, match="invalid store slug"):
        stage_storefront(_pack_dir(tmp_path), "storefront/", slug)
    assert _entries(storefronts) == {sf.DEFAULT_KEY}


def test_symlinked_folder_leaving_the_pack_is_refused(tmp_path, storefronts, db_session):
    outside = tmp_path / "outside"
    (outside / "templates").mkdir(parents=True)
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    os.symlink(outside, pack_dir / "storefront")
    shutil.copy(DEMO, pack_dir / DEMO.name)

    with pytest.raises(StorePackImportError) as info:
        import_store_pack_file(db_session, pack_dir / DEMO.name)
    [issue] = info.value.issues
    assert issue.path == "storefront.copy_from_pack_dir" and "resolves outside" in issue.message
    assert _stores(db_session) == 0 and _entries(storefronts) == {sf.DEFAULT_KEY}  # nothing written


def test_symlinks_inside_the_folder_are_refused(tmp_path, storefronts):
    pack_dir = _pack_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("server secret", encoding="utf-8")
    os.symlink(secret, pack_dir / "storefront" / "templates" / "leak.txt")
    with pytest.raises(StorefrontCopyError, match="templates/leak.txt: symbolic links are not copied"):
        stage_storefront(pack_dir, "storefront/", "acme")
    assert _entries(storefronts) == {sf.DEFAULT_KEY}  # the partial staging copy is removed


def test_hidden_files_are_not_copied(tmp_path, storefronts, db_session):
    pack_dir = tmp_path / "pack"
    shutil.copytree(DEMO_DIR, pack_dir)
    (pack_dir / "storefront" / ".DS_Store").write_bytes(b"\0finder")
    (pack_dir / "storefront" / ".git").mkdir()
    (pack_dir / "storefront" / ".git" / "config").write_text("x", encoding="utf-8")
    report = import_store_pack_file(db_session, pack_dir / DEMO.name)
    dest = storefronts / "storefront-demo"
    assert report.storefront_files == 2
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == [
        "static", "static/extra.css", "templates", "templates/store.html",
    ]


# --------------------------------------------------------------------- all-or-nothing


def test_failed_import_installs_nothing_and_keeps_the_old_copy(tmp_path, storefronts, db_session, client,
                                                              monkeypatch):
    pack_dir = tmp_path / "pack"
    shutil.copytree(DEMO_DIR, pack_dir)
    import_store_pack_file(db_session, pack_dir / DEMO.name)
    (pack_dir / "storefront" / "templates" / "product.html").write_text("NEW", encoding="utf-8")

    def boom(self, store, report):
        raise RuntimeError("database went away")

    monkeypatch.setattr(store_pack_import._Importer, "import_products", boom)
    with pytest.raises(RuntimeError):
        import_store_pack_file(db_session, pack_dir / DEMO.name)
    dest = storefronts / "storefront-demo"
    assert not (dest / "templates" / "product.html").exists()  # the new copy was not installed
    assert (dest / "templates" / "store.html").is_file()  # the previous one is intact
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "storefront-demo"}  # staging folder removed


def test_pack_without_folder_reports_it_as_skipped(db_session, storefronts):
    report = import_store_pack(db_session, _demo())  # data only, like an upload
    assert report.skipped == [STOREFRONT_NOT_COPIED] and report.storefront_files is None
    assert f"skipped: {STOREFRONT_NOT_COPIED}" in report.summary()
    assert _entries(storefronts) == {sf.DEFAULT_KEY}


def test_special_files_are_refused(tmp_path, storefronts):
    pack_dir = _pack_dir(tmp_path)
    os.mkfifo(pack_dir / "storefront" / "templates" / "pipe")
    with pytest.raises(StorefrontCopyError, match="templates/pipe: not a regular file"):
        stage_storefront(pack_dir, "storefront/", "acme")
    assert _entries(storefronts) == {sf.DEFAULT_KEY}


def test_failed_swap_puts_the_previous_folder_back(tmp_path, storefronts, monkeypatch):
    from app.services import storefront_install

    previous = storefronts / "acme" / "templates"
    previous.mkdir(parents=True)
    (previous / "store.html").write_text("previous", encoding="utf-8")
    staged = stage_storefront(_pack_dir(tmp_path), "storefront/", "acme")
    real_replace = os.replace

    def replace(src, dst):
        if Path(src) == staged.path:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(storefront_install.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        storefront_install.publish_storefront(staged)
    assert (previous / "store.html").read_text(encoding="utf-8") == "previous"
    storefront_install.discard_staged(staged)
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "acme"}


def test_replacing_a_symlinked_store_folder_only_removes_the_link(tmp_path, storefronts):
    from app.services import storefront_install

    target = tmp_path / "hand-made"
    (target / "templates").mkdir(parents=True)
    (target / "templates" / "store.html").write_text("keep me", encoding="utf-8")
    os.symlink(target, storefronts / "acme")
    dest = storefront_install.publish_storefront(stage_storefront(_pack_dir(tmp_path), "storefront/", "acme"))
    assert not dest.is_symlink() and (dest / "templates" / "store.html").read_text(encoding="utf-8") == "ok"
    assert (target / "templates" / "store.html").read_text(encoding="utf-8") == "keep me"  # not deleted
    assert _entries(storefronts) == {sf.DEFAULT_KEY, "acme"}
