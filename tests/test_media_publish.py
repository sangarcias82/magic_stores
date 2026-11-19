"""Pack images: a pack imported from its folder publishes media/ to CONTENT_ROOT/<store-slug>/
(the content app's folder); image values and theme.logo_url render through CONTENT_BASE_URL;
bad paths can't write outside the content root.

Every test here uses its own CONTENT_ROOT (``content_root`` fixture) and STOREFRONTS_ROOT
(``storefronts``, conftest.py): nothing is written to the real folders."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import PackImport, Store
from app.services import media_publish, store_pack_import
from app.services.media_publish import IMAGE_SUFFIXES, stage_media
from app.services.pack_files import PackFilesError
from app.services.store_pack_import import (
    MEDIA_NOT_PUBLISHED,
    StorePackImportError,
    import_store_pack,
    import_store_pack_file,
)
from tests.ui_text import en_html


PROJECT = Path(__file__).resolve().parent.parent
CONTENT_PROJECT = PROJECT.parent / "magic_stores_content"
DEMO_DIR = PROJECT / "fixtures" / "packs" / "storefront-demo"
DEMO = DEMO_DIR / "storefront-demo.store-pack.v1.json"
MEDIA = DEMO_DIR / "media"
CLOTHING = PROJECT / "fixtures" / "packs" / "clothing-demo.store-pack.v1.json"
BASE = "https://media.example.test"  # CONTENT_BASE_URL under the content_root fixture (conftest.py)


def _entries(root: Path) -> set[str]:
    return {p.name for p in root.iterdir()}


def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _stores(db) -> int:
    return db.scalar(select(func.count()).select_from(Store))


def _pack_copy(tmp_path: Path) -> Path:
    """A writable copy of the storefront-demo pack folder; returns the pack file."""
    shutil.copytree(DEMO_DIR, tmp_path / "pack")
    return tmp_path / "pack" / DEMO.name


def _src(html: str, css_class: str) -> str:
    match = re.search(rf'<img src="([^"]*)"[^>]*class="{css_class}"', html)
    assert match, html
    return match.group(1)


# ------------------------------------------------------------------------- happy path


def test_import_publishes_media_to_the_content_root(db_session, content_root, storefronts):
    report = import_store_pack_file(db_session, DEMO)

    dest = content_root / "storefront-demo"
    assert _files(dest) == _files(MEDIA)  # logo.png + tiny-gadget.png, same bytes
    assert report.media_files == 2 and report.media_path == str(dest)
    assert f"media: 2 image(s) published to {dest}" in report.summary().splitlines()
    assert report.skipped == []
    assert _entries(content_root) == {"storefront-demo"}  # no staging folder left behind


def test_image_and_logo_urls_go_through_content_base_url(client, db_session, content_root, storefronts,
                                                        content_app):
    import_store_pack_file(db_session, DEMO)

    product = client.get("/stores/storefront-demo/p/tiny-gadget").text
    photo = _src(product, "prop-image")
    assert photo == f"{BASE}/content/storefront-demo/tiny-gadget.png"
    home = client.get("/stores/storefront-demo").text  # the pack's own store.html extends base.html
    logo = _src(home, "brand-logo")
    assert logo == f"{BASE}/content/storefront-demo/logo.png"
    assert _src(product, "brand-logo") == logo  # every store page shows the logo

    # The content app serves exactly those paths from the content root.
    content = content_app(content_root)
    for url, name in ((photo, "tiny-gadget.png"), (logo, "logo.png")):
        response = content.get(url.removeprefix(BASE))
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.content == (MEDIA / name).read_bytes()


def test_logo_absolute_url_is_unchanged_and_no_logo_no_img(client, db_session, content_root):
    pack = json.loads(DEMO.read_text(encoding="utf-8"))
    pack["store"]["theme"]["logo_url"] = "https://cdn.example.com/logo.png"
    pack["storefront"]["copy_from_pack_dir"] = None
    import_store_pack(db_session, pack)
    assert _src(client.get("/stores/storefront-demo").text, "brand-logo") == "https://cdn.example.com/logo.png"

    import_store_pack_file(db_session, CLOTHING)  # no logo_url
    html = client.get("/stores/clothing-demo").text
    assert "brand-logo" not in html and '<a class="brand" href="/stores/clothing-demo">Demo Clothing</a>' in html


def test_pack_without_media_publishes_nothing(db_session, content_root):
    report = import_store_pack_file(db_session, CLOTHING)
    assert report.media_files is None and report.skipped == [] and _entries(content_root) == set()


def test_reimport_merges_and_keeps_other_files(db_session, content_root, storefronts):
    import_store_pack_file(db_session, DEMO)
    dest = content_root / "storefront-demo"
    (dest / "tiny-gadget.png").write_bytes(b"edited by hand")  # will be overwritten by the pack
    (dest / "extra-by-hand.jpg").write_bytes(b"kept")  # not in the pack: kept

    report = import_store_pack_file(db_session, DEMO)
    assert report.media_files == 2
    assert (dest / "tiny-gadget.png").read_bytes() == (MEDIA / "tiny-gadget.png").read_bytes()
    assert (dest / "extra-by-hand.jpg").read_bytes() == b"kept"
    assert _entries(content_root) == {"storefront-demo"}


def test_subfolders_and_hidden_files(tmp_path, db_session, content_root, storefronts):
    pack_file = _pack_copy(tmp_path)
    media = pack_file.parent / "media"
    (media / "products" / "blue").mkdir(parents=True)
    (media / "products" / "blue" / "front.JPG").write_bytes(b"jpeg")  # suffix check ignores case
    (media / ".DS_Store").write_bytes(b"finder")
    (media / ".cache").mkdir()
    (media / ".cache" / "x.png").write_bytes(b"x")

    report = import_store_pack_file(db_session, pack_file)
    assert report.media_files == 3
    assert sorted(_files(content_root / "storefront-demo")) == ["logo.png", "products/blue/front.JPG", "tiny-gadget.png"]


# ------------------------------------------------------------------ existing import paths


def test_command_line_publishes_and_records_it(engine, content_root, storefronts, capsys):
    assert store_pack_import.main([str(DEMO)], sessionmaker(bind=engine, autoflush=False)) == 0
    out = capsys.readouterr().out
    assert f"  media: 2 image(s) published to {content_root / 'storefront-demo'}" in out
    assert "skipped" not in out
    record = sessionmaker(bind=engine)().scalar(select(PackImport))
    assert record.report["media_files"] == 2


def test_admin_upload_imports_the_pack_but_has_no_files_to_publish(admin_client, content_root, storefronts):
    response = admin_client.post("/admin/packs", files={"pack": ("p.json", DEMO.read_bytes(), "application/json")},
                                 follow_redirects=True)
    assert response.status_code == 200
    assert en_html("flash.pack_storefront_skipped") in response.text
    assert _entries(content_root) == set()  # nothing published (an upload is only the JSON file)
    # The values still point at the content app: files published earlier (command line) are used.
    product = admin_client.get("/stores/storefront-demo/p/tiny-gadget").text
    assert _src(product, "prop-image") == f"{BASE}/content/storefront-demo/tiny-gadget.png"


def test_empty_content_root_setting_skips_publishing(db_session, content_root, storefronts, monkeypatch):
    monkeypatch.setenv("CONTENT_ROOT", "")
    get_settings.cache_clear()
    report = import_store_pack_file(db_session, DEMO)
    assert report.media_files is None and report.skipped == [MEDIA_NOT_PUBLISHED]
    assert _entries(content_root) == set() and _stores(db_session) == 1  # imported, media not published


def test_missing_content_root_stops_the_import(db_session, content_root, storefronts, monkeypatch):
    monkeypatch.setenv("CONTENT_ROOT", str(content_root / "typo"))
    get_settings.cache_clear()
    with pytest.raises(StorePackImportError) as info:
        import_store_pack_file(db_session, DEMO)
    [issue] = info.value.issues
    assert issue.path == "media/" and issue.message.startswith("content root not found:")
    assert _stores(db_session) == 0 and not (content_root / "typo").exists()
    assert _entries(storefronts) == {"_default"}  # the storefront folder wasn't installed either


# ------------------------------------------------------------------------- path safety


def _refused(db, pack_file: Path, content_root: Path, storefronts: Path, message: str) -> None:
    with pytest.raises(StorePackImportError) as info:
        import_store_pack_file(db, pack_file)
    [issue] = info.value.issues
    assert issue.path == "media/" and message in issue.message, issue.message
    assert _stores(db) == 0  # nothing written: no store, no files
    assert _entries(storefronts) == {"_default"}


def test_media_folder_symlinked_outside_the_pack_is_refused(tmp_path, db_session, content_root, storefronts):
    pack_file = _pack_copy(tmp_path)
    shutil.rmtree(pack_file.parent / "media")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"secret")
    os.symlink(outside, pack_file.parent / "media")
    _refused(db_session, pack_file, content_root, storefronts, "resolves outside")
    assert _entries(content_root) == set()


def test_symlink_inside_media_is_refused(tmp_path, db_session, content_root, storefronts):
    pack_file = _pack_copy(tmp_path)
    secret = tmp_path / "server-secret.png"
    secret.write_bytes(b"secret")
    os.symlink(secret, pack_file.parent / "media" / "leak.png")
    _refused(db_session, pack_file, content_root, storefronts, "leak.png: symbolic links are not copied")
    assert _entries(content_root) == set()  # the staging copy is removed


def test_only_images_are_published(tmp_path, db_session, content_root, storefronts):
    pack_file = _pack_copy(tmp_path)
    (pack_file.parent / "media" / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    _refused(db_session, pack_file, content_root, storefronts, "page.html: only image files are published")
    assert _entries(content_root) == set()
    assert ".png" in IMAGE_SUFFIXES and ".html" not in IMAGE_SUFFIXES


def test_store_folder_symlinked_elsewhere_is_refused(tmp_path, db_session, content_root, storefronts):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, content_root / "storefront-demo")
    _refused(db_session, DEMO, content_root, storefronts, "must be a real folder")
    assert list(elsewhere.iterdir()) == []  # nothing written through the link


def test_subfolder_symlinked_elsewhere_is_refused(tmp_path, db_session, content_root, storefronts):
    pack_file = _pack_copy(tmp_path)
    (pack_file.parent / "media" / "products").mkdir()
    (pack_file.parent / "media" / "products" / "a.png").write_bytes(b"png")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (content_root / "storefront-demo").mkdir()
    os.symlink(elsewhere, content_root / "storefront-demo" / "products")
    _refused(db_session, pack_file, content_root, storefronts, "must be a real folder")
    assert list(elsewhere.iterdir()) == []


def test_folder_in_the_way_of_a_file_is_refused(tmp_path, db_session, content_root, storefronts):
    (content_root / "storefront-demo" / "logo.png").mkdir(parents=True)
    _refused(db_session, DEMO, content_root, storefronts, "logo.png: a folder with that name exists")


@pytest.mark.parametrize("slug", ["../escape", "a/b", "..", "Acme", "acme\n", ""])
def test_destination_must_be_a_store_slug(content_root, slug):
    with pytest.raises(PackFilesError, match="invalid store slug"):
        stage_media(DEMO_DIR, slug, content_root)
    assert _entries(content_root) == set()


# --------------------------------------------------------------------- all-or-nothing


def test_failed_import_publishes_nothing(tmp_path, db_session, content_root, storefronts, monkeypatch):
    def boom(self, store, report):
        raise RuntimeError("database went away")

    monkeypatch.setattr(store_pack_import._Importer, "import_products", boom)
    with pytest.raises(RuntimeError):
        import_store_pack_file(db_session, DEMO)
    assert _entries(content_root) == set() and _entries(storefronts) == {"_default"}


def test_folder_changed_after_staging_is_checked_again(tmp_path, content_root):
    staged = stage_media(DEMO_DIR, "storefront-demo", content_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, content_root / "storefront-demo")  # appears between staging and publishing
    with pytest.raises(PackFilesError, match="must be a real folder"):
        media_publish.publish_media(staged)
    assert list(elsewhere.iterdir()) == [] and _entries(content_root) == {"storefront-demo"}  # staging removed


def test_subfolder_changed_after_staging_is_checked_again(tmp_path, content_root):
    pack_file = _pack_copy(tmp_path)
    (pack_file.parent / "media" / "products").mkdir()
    (pack_file.parent / "media" / "products" / "a.png").write_bytes(b"png")
    staged = stage_media(pack_file.parent, "storefront-demo", content_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (content_root / "storefront-demo").mkdir()
    os.symlink(elsewhere, content_root / "storefront-demo" / "products")  # appears after staging
    with pytest.raises(PackFilesError, match="products/a.png: .* must be a real folder"):
        media_publish.publish_media(staged)
    assert list(elsewhere.iterdir()) == []  # nothing written through the link
    assert _files(content_root / "storefront-demo") == {}  # and nothing else either: checks run first
    assert _entries(content_root) == {"storefront-demo"}  # staging removed


# ---------------------------------------------------------------------- missing images


def test_cli_reports_images_the_pack_uses_but_does_not_ship(tmp_path, engine, content_root, storefronts, capsys):
    pack_file = _pack_copy(tmp_path)
    pack = json.loads(pack_file.read_text(encoding="utf-8"))
    pack["products"][0]["values"]["photo"] = "/content/storefront-demo/Tiny-Gadget.png"  # typo: wrong case
    pack_file.write_text(json.dumps(pack), encoding="utf-8")

    assert store_pack_import.main([str(pack_file)], sessionmaker(bind=engine, autoflush=False)) == 0  # imported
    out = capsys.readouterr().out
    assert (f"  missing media: 1 image(s) used by the pack are not in {content_root} (broken images): "
            "/content/storefront-demo/Tiny-Gadget.png") in out
    record = sessionmaker(bind=engine)().scalar(select(PackImport))
    assert record.report["missing_media"] == ["/content/storefront-demo/Tiny-Gadget.png"]


@pytest.fixture()
def case_insensitive_fs(monkeypatch):
    """Make ``os.stat`` ignore case, like macOS (APFS) and Windows do by default: ``Tiny-Gadget.png``
    then finds ``tiny-gadget.png``. ``Path.is_file`` and ``os.path.isfile`` go through ``os.stat``, so
    this reproduces on Linux what a Mac does (``os.listdir`` still returns the real names, as on a Mac)."""
    real_stat = os.stat

    def real_name(path: Path) -> Path:
        if path.parent == path or os.path.lexists(path):
            return path
        parent = real_name(path.parent)
        try:
            names = os.listdir(parent)
        except OSError:
            return parent / path.name
        return parent / next((n for n in names if n.casefold() == path.name.casefold()), path.name)

    def stat(path, *args, **kwargs):
        if isinstance(path, int):  # a file descriptor
            return real_stat(path, *args, **kwargs)
        return real_stat(real_name(Path(os.fsdecode(path))), *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_missing_check_is_case_exact_on_case_insensitive_file_systems(content_root, case_insensitive_fs):
    (content_root / "shop").mkdir()
    (content_root / "shop" / "tiny-gadget.png").write_bytes(b"png")
    assert (content_root / "Shop" / "Tiny-Gadget.png").is_file()  # the simulation: found, like on a Mac
    pack = {"store": {"theme": {"logo_url": "/content/shop/Tiny-Gadget.png"}}, "categories": [], "products": []}
    assert media_publish.missing_content_files(pack, content_root) == ["/content/shop/Tiny-Gadget.png"]
    pack["store"]["theme"]["logo_url"] = "/content/Shop/tiny-gadget.png"  # folder in the wrong case
    assert media_publish.missing_content_files(pack, content_root) == ["/content/Shop/tiny-gadget.png"]
    pack["store"]["theme"]["logo_url"] = "/content/shop/tiny-gadget.png"
    assert media_publish.missing_content_files(pack, content_root) == []
    pack["store"]["theme"]["logo_url"] = "/content/shop/tiny-gadget.png/x.png"  # below a file
    assert media_publish.missing_content_files(pack, content_root) == ["/content/shop/tiny-gadget.png/x.png"]


def test_missing_check_never_looks_outside_the_content_root(tmp_path, content_root):
    (tmp_path / "secret.png").write_bytes(b"exists, but outside the root")
    pack = {"store": {"theme": {"logo_url": "/content/../secret.png"}}, "categories": [], "products": []}
    assert media_publish.missing_content_files(pack, content_root) == ["/content/../secret.png"]
    (content_root / "s").mkdir()
    (content_root / "s" / "ok.png").write_bytes(b"png")
    pack = {
        "store": {"theme": {"logo_url": "https://cdn.example.com/logo.png"}},  # not a content path: not checked
        "categories": [{"slug": "c", "property_definitions": [{"key": "photo", "prop_type": "image"},
                                                               {"key": "note", "prop_type": "text"}]}],
        "products": [{"category_slug": "c", "values": {"photo": "/content/s/ok.png", "note": "/content/s/no.png"}}],
    }
    assert media_publish.missing_content_files(pack, content_root) == []  # text values are not images
