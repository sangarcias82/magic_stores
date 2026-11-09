from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.responses import FileResponse, Response

STOREFRONTS_ROOT = Path(__file__).resolve().parent.parent / "storefronts"
DEFAULT_KEY = "_default"
PLATFORM_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def storefront_dir(store_slug: str) -> Path:
    return STOREFRONTS_ROOT / store_slug


def default_dir() -> Path:
    return STOREFRONTS_ROOT / DEFAULT_KEY


def resolve_static_file(store_slug: str, relative_path: str) -> Optional[Path]:
    """Prefer store-specific static asset, else default."""
    relative_path = relative_path.lstrip("/")
    for root in (storefront_dir(store_slug), default_dir()):
        candidate = (root / "static" / relative_path).resolve()
        static_root = (root / "static").resolve()
        if not str(candidate).startswith(str(static_root)):
            continue
        if candidate.is_file():
            return candidate
    return None


def resolve_page_file(store_slug: str, page_slug: str) -> Optional[Path]:
    """Optional extra HTML pages under storefronts/<slug>/pages/."""
    safe = page_slug.replace("..", "").strip("/")
    for root in (storefront_dir(store_slug), default_dir()):
        candidate = root / "pages" / f"{safe}.html"
        if candidate.is_file():
            return candidate
    return None


def list_store_pages(store_slug: str) -> list[str]:
    pages: set[str] = set()
    for root in (default_dir(), storefront_dir(store_slug)):
        pages_dir = root / "pages"
        if not pages_dir.is_dir():
            continue
        for path in pages_dir.glob("*.html"):
            pages.add(path.stem)
    return sorted(pages)


@lru_cache(maxsize=32)
def templates_for(store_slug: str, root: str = "") -> Jinja2Templates:
    # ``root`` is part of the cache key so tests can re-point STOREFRONTS_ROOT.
    base = Path(root) if root else STOREFRONTS_ROOT
    loaders = []
    store_templates = base / store_slug / "templates"
    if store_templates.is_dir():
        loaders.append(FileSystemLoader(str(store_templates)))
    loaders.append(FileSystemLoader(str(base / DEFAULT_KEY / "templates")))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return Jinja2Templates(env=env)


def templates_for_store(store_slug: str) -> Jinja2Templates:
    return templates_for(store_slug, str(STOREFRONTS_ROOT))


@lru_cache(maxsize=1)
def platform_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(PLATFORM_TEMPLATES))


def theme_from_store(store) -> dict:
    defaults = {
        "primary": "#1a5f4a",
        "secondary": "#f7f4ef",
        "accent": "#c45c26",
        "logo_url": None,
    }
    return {**defaults, **(store.theme or {})}


def static_response(store_slug: str, relative_path: str) -> Response:
    path = resolve_static_file(store_slug, relative_path)
    if path is None:
        return Response(status_code=404, content="Not found")
    return FileResponse(path)


def storefront_has_override(store_slug: str) -> bool:
    path = storefront_dir(store_slug)
    return path.is_dir() and any(path.iterdir())
