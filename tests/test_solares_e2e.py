"""The solares pack end to end with real processes and real HTTP, the way it is run by hand:

1. ``python -m app.services.store_pack_validation`` and ``python -m app.services.store_pack_import``
   (the documented commands, run from the project folder) import the pack from its folder;
2. the content app (../magic_stores_content, its own process) serves CONTENT_ROOT;
3. Magic Stores (its own process) runs with CONTENT_BASE_URL = the content app's address;
4. the JSON API, the public pages and every <img src> are requested over HTTP.

Isolation: the commands and Magic Stores run from a temporary copy of this project's ``app/``
(the same code) with a temporary database and CONTENT_ROOT, so the storefront folder the import
copies lands in the copy's ``app/storefronts/solares/``, never in the real one. Nothing outside
the test's temporary folder is written.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PROJECT = Path(__file__).resolve().parent.parent
CONTENT_PROJECT = PROJECT.parent / "magic_stores_content"
PACK = "fixtures/packs/solares/solares.store-pack.v1.json"
CLOTHING = "fixtures/packs/clothing-demo.store-pack.v1.json"
MEDIA = PROJECT / "fixtures" / "packs" / "solares" / "media"
PACK_STOREFRONT = PROJECT / "fixtures" / "packs" / "solares" / "storefront"

pytestmark = pytest.mark.skipif(
    not (CONTENT_PROJECT / "content_app").is_dir(), reason="magic_stores_content is not next to this project"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, process: subprocess.Popen, log: Path, timeout: float = 30) -> None:
    """Until ``url`` answers at all (any HTTP status): the server is listening."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"{url}: server exited ({process.returncode}):\n{log.read_text()}")
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    pytest.fail(f"{url}: no answer after {timeout}s:\n{log.read_text()}")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """Copy of the project, the two commands run, both servers up. Yields the addresses."""
    tmp = tmp_path_factory.mktemp("solares-e2e")
    project = tmp / "magic_stores"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(PROJECT / "app", project / "app", ignore=ignore)
    for extra in [p for p in (project / "app" / "storefronts").iterdir()
                  if p.is_dir() and p.name not in ("_default",)]:
        shutil.rmtree(extra)  # only the engine's own storefront; no folder from an earlier import
    shutil.copytree(PROJECT / "fixtures" / "packs" / "solares", project / "fixtures" / "packs" / "solares")
    shutil.copy(PROJECT / CLOTHING, project / CLOTHING)
    content_root = tmp / "content"
    content_root.mkdir()

    store_port, content_port = _free_port(), _free_port()
    content_base = f"http://127.0.0.1:{content_port}"
    # No PYTHONPATH: the commands import the copy's app/. No COV_CORE_* / COVERAGE_*: under
    # `pytest --cov` the copy must not be measured as a second, mostly unexecuted `app` package.
    env = {k: v for k, v in os.environ.items()
           if k != "PYTHONPATH" and not k.startswith(("COV_CORE_", "COVERAGE_"))}
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp / 'e2e.db'}",
        "CONTENT_ROOT": str(content_root),
        "CONTENT_BASE_URL": content_base,
        "CONTENT_DIR": str(content_root),  # the content app serves the same folder
        "SECRET_KEY": "e2e-secret",
    })

    def command(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", *args], cwd=project, env=env, capture_output=True, text=True,
                              timeout=120)

    validation = command("app.services.store_pack_validation", PACK)
    imports = [command("app.services.store_pack_import", path) for path in (PACK, CLOTHING)]

    processes = []
    try:
        for name, cwd, target, port, ready in (
            ("content", CONTENT_PROJECT, "content_app.main:app", content_port, f"{content_base}/content/"),
            ("stores", project, "app.main:app", store_port, f"http://127.0.0.1:{store_port}/api/health"),
        ):
            log = tmp / f"{name}.log"
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)],
                cwd=cwd, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
            )
            processes.append(process)
            _wait(ready, process, log)
        with httpx.Client(base_url=f"http://127.0.0.1:{store_port}", timeout=10) as stores:
            yield {
                "stores": stores, "content_base": content_base, "project": project, "content_root": content_root,
                "validation": validation, "imports": imports,
            }
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck server
                process.kill()


# ------------------------------------------------------------------------- the commands


def test_cli_validates_and_imports_from_the_pack_folder(live):
    assert live["validation"].returncode == 0 and f"{PACK}: valid" in live["validation"].stdout
    solares, clothing = live["imports"]
    assert solares.returncode == 0, solares.stdout + solares.stderr
    out = solares.stdout
    assert "store solares: created" in out and "categories: 2 created" in out and "products: 5 created" in out
    storefront = live["project"] / "app" / "storefronts" / "solares"
    assert f"storefront: 4 file(s) copied to {storefront}" in out
    assert f"media: 6 image(s) published to {live['content_root'] / 'solares'}" in out
    assert "missing media" not in out and "skipped" not in out
    assert clothing.returncode == 0 and "storefront:" not in clothing.stdout  # no storefront folder

    for rel in ("templates/store.html", "templates/product.html", "static/style.css", "static/logo-solar.jpg"):
        assert (storefront / rel).read_bytes() == (PACK_STOREFRONT / rel).read_bytes()
    assert {p.name: p.read_bytes() for p in (live["content_root"] / "solares").iterdir()} == \
        {p.name: p.read_bytes() for p in MEDIA.iterdir()}
    assert not (live["project"] / "app" / "storefronts" / "clothing-demo").exists()


# ------------------------------------------------------------------------------ the API


def test_api_over_http(live):
    api = live["stores"]
    assert api.get("/api/stores/solares").json()["theme"]["logo_url"] == "/content/solares/logo-solar.jpg"
    categories = {c["slug"]: c for c in api.get("/api/stores/solares/categories").json()}
    keys = {d["key"]: d["config"] for d in categories["calentadores-solares"]["property_definitions"]}
    assert keys["tubos"]["options"][0] == "8" and keys["tubos"]["options"][-1] == "20"
    assert keys["presion"]["options"] == ["alta", "baja"]
    products = {p["slug"] for p in api.get("/api/stores/solares/products").json()}
    assert len(products) == 5
    seen = set()
    for slug in sorted(products - {"boiler-gas-40-litros"}):
        values = {p["key"]: p["value"]["value"] for p in api.get(f"/api/stores/solares/products/{slug}").json()[
            "properties"] if p["key"] in ("tubos", "presion")}
        assert slug == f"solar-{values['tubos']}-tubos-{values['presion']}"
        seen.add((values["tubos"], values["presion"]))
    assert seen == {("8", "alta"), ("12", "baja"), ("16", "alta"), ("20", "alta")}


# ------------------------------------------------------------- storefront + photos


def _img_srcs(html: str) -> list[str]:
    return re.findall(r'<img [^>]*src="([^"]*)"', html)


def test_store_and_product_pages_use_the_copied_storefront(live):
    stores = live["stores"]
    home = stores.get("/stores/solares")
    product = stores.get("/stores/solares/p/solar-8-tubos-alta")
    assert home.status_code == product.status_code == 200
    assert 'data-storefront="solares"' in home.text and "¿Cuántos tubos necesito?" in home.text  # pack store.html
    assert '<article class="solar-product" data-storefront="solares">' in product.text  # pack product.html
    assert '<span class="badge badge-tubes">Número de tubos: 8</span>' in product.text
    assert '<span class="badge badge-pressure">Presión: alta</span>' in product.text
    assert stores.get("/stores/solares/assets/style.css").content == (PACK_STOREFRONT / "static/style.css").read_bytes()


def test_store_without_override_stays_on_default(live):
    stores = live["stores"]
    html = stores.get("/stores/clothing-demo").text
    assert '<section class="grid-two">' in html and "data-storefront" not in html
    default_css = (PROJECT / "app" / "storefronts" / "_default" / "static" / "style.css").read_bytes()
    assert stores.get("/stores/clothing-demo/assets/style.css").content == default_css


def test_photos_come_from_the_content_app_through_content_base_url(live):
    stores, base = live["stores"], live["content_base"]
    expected = {
        "/stores/solares/p/solar-8-tubos-alta": {"logo-solar.jpg", "tubo-8-alta.jpg"},
        "/stores/solares/p/solar-12-tubos-baja": {"logo-solar.jpg", "tubo-12-baja.jpg"},
        "/stores/solares/p/solar-16-tubos-alta": {"logo-solar.jpg", "tubo-16-alta.jpg"},
        "/stores/solares/p/solar-20-tubos-alta": {"logo-solar.jpg", "tubo-20-alta.jpg"},
        "/stores/solares/p/boiler-gas-40-litros": {"logo-solar.jpg", "gas-boiler.jpg"},
    }
    for page, files in expected.items():
        srcs = _img_srcs(stores.get(page).text)
        assert {s.rsplit("/", 1)[1] for s in srcs} == files, page
        for src in srcs:
            assert src.startswith(f"{base}/content/solares/"), src  # the content app, not Magic Stores
            image = httpx.get(src, timeout=10)
            assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg", src
            assert image.content == (MEDIA / src.rsplit("/", 1)[1]).read_bytes()
    # Magic Stores itself does not serve them: the files live only in magic_stores_content's folder.
    assert stores.get("/content/solares/tubo-8-alta.jpg").status_code == 404
