"""The admin upload path end to end, as a user does it: upload the pack at /admin/packs, open the
store pages, and load every <img src> from the real content app (../magic_stores_content).

- A .zip of the pack folder publishes media/ (and copies storefront/) like the command line.
- A .json upload has no images to publish: the success message says which images are missing.

Every test uses its own CONTENT_ROOT / STOREFRONTS_ROOT; nothing is written to the real folders."""

from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import Path

import pytest
from markupsafe import escape
from sqlalchemy import select

from app.models import PackImport, PackImportStatus
from app.routers import admin_packs


PROJECT = Path(__file__).resolve().parent.parent
CONTENT_PROJECT = PROJECT.parent / "magic_stores_content"
DEMO_DIR = PROJECT / "fixtures" / "packs" / "storefront-demo"
DEMO = DEMO_DIR / "storefront-demo.store-pack.v1.json"
BASE = "https://media.example.test"  # CONTENT_BASE_URL under the content_root fixture (conftest.py)
UPLOAD = "/admin/packs"


def _zip_folder(folder: Path, *, top: str = "storefront-demo", extra: dict[str, bytes] | None = None) -> bytes:
    """A zip like `zip -r x.zip storefront-demo/` (one top-level folder), plus macOS junk."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(folder.rglob("*")):
            name = f"{top}/{path.relative_to(folder).as_posix()}" if top else path.relative_to(folder).as_posix()
            archive.write(path, name)
        archive.writestr(f"__MACOSX/{top}/._logo.png" if top else "__MACOSX/._logo.png", b"resource fork")
        archive.writestr(f"{top}/.DS_Store" if top else ".DS_Store", b"finder")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload(client, content: bytes, filename: str, *, follow: bool = True, update: bool = False):
    data = {"update_existing": "1"} if update else {}
    return client.post(UPLOAD, files={"pack": (filename, content, "application/octet-stream")}, data=data,
                       follow_redirects=follow)


def _img_srcs(html: str) -> list[str]:
    return re.findall(r'<img src="([^"]*)"', html)


def _assert_images_load(client, content_root: Path, content_app) -> list[str]:
    """Open the store pages like a browser and load every image from the content app."""
    content = content_app(content_root)
    srcs = []
    for page in ("/stores/storefront-demo", "/stores/storefront-demo/p/tiny-gadget"):
        html = client.get(page).text
        for src in _img_srcs(html):
            assert src.startswith(f"{BASE}/content/storefront-demo/"), src
            response = content.get(src.removeprefix(BASE))
            assert response.status_code == 200, f"{src} -> {response.status_code} (broken image on {page})"
            assert response.headers["content-type"] == "image/png"
            srcs.append(src)
    assert sorted(set(srcs)) == [f"{BASE}/content/storefront-demo/logo.png",
                                 f"{BASE}/content/storefront-demo/tiny-gadget.png"]
    return srcs


def _missing_warning_start() -> str:
    """The fixed text after the count in the "images missing" message (present whatever the count)."""
    text = en_html("flash.pack_media_missing", count=7, paths="PATHS")
    return text.split("7", 1)[1].split("PATHS", 1)[0]


# --------------------------------------------------------------------- .zip upload


def test_zip_upload_publishes_images_and_they_load(admin_client, content_root, storefronts, content_app):
    response = _upload(admin_client, _zip_folder(DEMO_DIR), "storefront-demo.zip")
    assert response.status_code == 200 and "storefront-demo" in str(response.url)
    assert en_html("flash.pack_storefront_skipped") not in response.text  # the zip has the folder
    assert _missing_warning_start() not in response.text

    _assert_images_load(admin_client, content_root, content_app)
    assert (storefronts / "storefront-demo" / "templates" / "store.html").is_file()  # storefront/ copied too
    assert 'data-storefront="pack-override"' in admin_client.get("/stores/storefront-demo").text
    assert sorted(p.name for p in (content_root / "storefront-demo").iterdir()) == ["logo.png", "tiny-gadget.png"]


def test_zip_without_top_folder_works_too(admin_client, content_root, storefronts, content_app):
    assert _upload(admin_client, _zip_folder(DEMO_DIR, top=""), "pack.zip").status_code == 200
    _assert_images_load(admin_client, content_root, content_app)


def test_zip_upload_is_recorded_with_the_zip_file(admin_client, db_session, content_root, storefronts):
    raw = _zip_folder(DEMO_DIR)
    _upload(admin_client, raw, "storefront-demo.zip")
    record = db_session.scalar(select(PackImport))
    assert record.status == PackImportStatus.succeeded and record.filename == "storefront-demo.zip"
    assert record.file_bytes == len(raw)
    assert record.report["media_files"] == 2 and record.report["storefront_files"] == 2
    assert record.report["missing_media"] == []


# -------------------------------------------------------------- .json upload warning


def test_json_upload_warns_that_images_are_missing(admin_client, db_session, content_root, storefronts):
    page = _upload(admin_client, DEMO.read_bytes(), DEMO.name)
    assert page.status_code == 200
    missing = "/content/storefront-demo/logo.png, /content/storefront-demo/tiny-gadget.png"
    assert en_html("flash.pack_media_missing", count=2, paths=missing) in page.text
    record = db_session.scalar(select(PackImport))
    assert record.report["missing_media"] == ["/content/storefront-demo/logo.png",
                                              "/content/storefront-demo/tiny-gadget.png"]


def test_json_upload_after_the_images_exist_has_no_warning(admin_client, content_root, storefronts, content_app):
    _upload(admin_client, _zip_folder(DEMO_DIR), "storefront-demo.zip")  # images published once
    page = _upload(admin_client, DEMO.read_bytes(), DEMO.name, update=True)  # then a JSON-only update
    assert page.status_code == 200 and _missing_warning_start() not in page.text
    _assert_images_load(admin_client, content_root, content_app)


# ------------------------------------------------------------------ bad archives


def _refused(client, raw: bytes, message: str):
    response = _upload(client, raw, "pack.zip", follow=False)
    assert response.status_code == 400, response.text
    assert en_html("admin.packs.error.invalid") in response.text
    assert str(escape(message)) in response.text, response.text
    return response


def _zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("../evil.png", "unsafe path in the zip"),
        ("pack/../../evil.png", "unsafe path in the zip"),
        ("/etc/evil.png", "unsafe path in the zip"),
        ("C:/evil.png", "unsafe path in the zip"),
        ("media\\evil.png", "unsafe path in the zip"),
    ],
)
def test_zip_entries_cannot_escape(admin_client, content_root, storefronts, tmp_path, name, message):
    raw = _zip({DEMO.name: DEMO.read_bytes(), name: b"x"})
    _refused(admin_client, raw, message)
    assert list(content_root.iterdir()) == [] and sorted(p.name for p in storefronts.iterdir()) == ["_default"]
    assert not (tmp_path.parent / "evil.png").exists()


def test_zip_symlinks_are_refused(admin_client, content_root, storefronts):
    raw = _zip({DEMO.name: DEMO.read_bytes()}, symlinks={"media/logo.png": "/etc/passwd"})
    _refused(admin_client, raw, "media/logo.png: symbolic links are not allowed in the zip")
    assert list(content_root.iterdir()) == []


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ({"media/logo.png": b"x"}, "no store-pack file"),
        ({"a.store-pack.v1.json": b"{}", "b.store-pack.v1.json": b"{}"}, "more than one"),
        ({"one/x.store-pack.v1.json": b"{}", "two/y.png": b"x"}, "no store-pack file"),
    ],
)
def test_zip_must_hold_exactly_one_pack_file_at_its_top(admin_client, content_root, entries, message):
    _refused(admin_client, _zip(entries), message)


def test_not_a_zip_and_too_many_files(admin_client, content_root, monkeypatch):
    _refused(admin_client, b"PK\x03\x04 not really a zip", "not a valid zip file")
    monkeypatch.setattr("app.services.pack_archive.MAX_ENTRIES", 3)
    _refused(admin_client, _zip({DEMO.name: DEMO.read_bytes(), **{f"media/{i}.png": b"x" for i in range(3)}}),
             "more than 3 files")


def test_zip_bomb_is_refused(admin_client, content_root, monkeypatch):
    monkeypatch.setattr("app.services.pack_archive.MAX_EXTRACTED_BYTES", 1000)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DEMO.name, DEMO.read_bytes())
        archive.writestr("media/big.png", b"\0" * 5000)  # compresses to a few bytes
    _refused(admin_client, buffer.getvalue(), "more than 1000 bytes once unpacked")
    assert list(content_root.iterdir()) == []


def test_zip_larger_than_the_archive_limit_is_413(admin_client, monkeypatch):
    monkeypatch.setattr(admin_packs, "MAX_ARCHIVE_BYTES", 100)
    response = _upload(admin_client, _zip_folder(DEMO_DIR), "big.zip", follow=False)
    assert response.status_code == 413
    assert en_html("admin.packs.error.too_large", max_mb=admin_packs.MAX_ARCHIVE_MB) in response.text


def test_upload_form_accepts_zip(admin_client):
    html = admin_client.get(UPLOAD).text
    assert 'accept=".json,.zip,application/json,application/zip"' in html


# ------------------------------------------------------- archive reader, unit level


from app.services.pack_archive import extract_pack_archive  # noqa: E402
from app.services.store_pack_validation import StorePackError  # noqa: E402
from tests.ui_text import en_html


def _extract_error(raw: bytes, **kwargs) -> str:
    with pytest.raises(StorePackError) as info:
        with extract_pack_archive(raw, **kwargs):
            pass
    [issue] = info.value.issues
    assert issue.path == "zip"
    return issue.message


def test_archive_special_entries_duplicates_and_clashes():
    fifo = zipfile.ZipInfo("media/pipe")
    fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(DEMO.name, DEMO.read_bytes())
        archive.writestr(fifo, b"")
    assert _extract_error(buffer.getvalue()) == "media/pipe: not a regular file"

    buffer = io.BytesIO()
    with pytest.warns(UserWarning), zipfile.ZipFile(buffer, "w") as archive:  # zipfile warns on duplicates
        archive.writestr("media/a.png", b"1")
        archive.writestr("media/a.png", b"2")
    assert _extract_error(buffer.getvalue()) == "media/a.png: appears twice in the zip"

    assert _extract_error(_zip({"media": b"file", "media/a.png": b"x"})) == \
        "media/a.png: a file and a folder have the same name"  # file first, then a file inside it
    assert _extract_error(_zip({"media/a.png": b"x", "media": b"file"})) == \
        "media: a file and a folder have the same name"  # folder first, then a file with its name


def test_archive_corrupt_entry_and_oversized_pack_file():
    raw = bytearray(_zip({DEMO.name: DEMO.read_bytes()}))
    start = raw.index(b'"version"')  # flip stored bytes: the CRC check fails while reading
    raw[start:start + 9] = b"XXXXXXXXX"
    assert "can't be read" in _extract_error(bytes(raw))
    assert _extract_error(_zip({DEMO.name: DEMO.read_bytes()}), max_pack_bytes=10) == \
        f"{DEMO.name}: larger than 10 bytes"


def test_unexpected_error_while_unpacking_is_recorded(admin_client, db_session, monkeypatch):
    def disk_full(raw, max_pack_bytes=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("app.services.pack_import_log.extract_pack_archive", disk_full)
    response = _upload(admin_client, _zip_folder(DEMO_DIR), "pack.zip", follow=False)
    assert response.status_code == 500
    record = db_session.scalar(select(PackImport))
    assert record.status == PackImportStatus.failed and "No space left on device" in record.exception
