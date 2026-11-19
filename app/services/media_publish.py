"""Publish a store pack's images to the content app (``../magic_stores_content``).

Pack layout: a ``media/`` folder next to the pack file. Its image files (subfolders kept) are
published to ``CONTENT_ROOT/<store-slug>/``, the folder the content app serves at
``/content/<store-slug>/…``. The pack refers to them with that path, e.g. an image value or
``store.theme.logo_url`` of ``"/content/<store-slug>/logo.png"``, which storefront templates turn
into ``CONTENT_BASE_URL/content/<store-slug>/logo.png`` (:func:`app.services.media.media_url`).

Like the storefront folder, publishing has two steps so an import stays all-or-nothing:
:func:`stage_media` (before the database commit) checks everything and copies the files into a
hidden staging folder in ``CONTENT_ROOT``; :func:`publish_media` (after the commit) moves them in
place, or :func:`discard_media` removes them if the import failed.

Re-import **merges**: files of the pack overwrite files with the same path; other files in
``CONTENT_ROOT/<store-slug>/`` are kept (images added by hand, or used by products edited in admin).

Safety, on top of :mod:`app.services.pack_files` (``media/`` inside the pack folder, no symbolic
links or special files, hidden files skipped, valid slug):

- only image files are published (:data:`IMAGE_SUFFIXES`); the content app serves them publicly;
- ``CONTENT_ROOT`` must be an existing folder, and nothing is written outside
  ``CONTENT_ROOT/<store-slug>/``: that folder, and every folder below it that a file goes into,
  must be a real folder, not a symbolic link (which could lead anywhere).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from app.config import get_settings
from app.services.pack_files import PackFilesError, check_slug, copy_tree, pack_subfolder

MEDIA_DIR = "media"
CONTENT_PREFIX = "/content/"  # the content app's URL path, as used in pack values
IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})


@dataclass
class StagedMedia:
    store_slug: str
    dest: Path  # CONTENT_ROOT/<store-slug> (resolved root)
    path: Path  # hidden staging folder inside CONTENT_ROOT
    files: list[str] = field(default_factory=list)  # relative paths, POSIX style


def content_root() -> Optional[Path]:
    """The ``CONTENT_ROOT`` setting, or None when it is empty (publishing disabled)."""
    value = get_settings().content_root
    return Path(value) if value else None


def pack_has_media(pack_dir: Path) -> bool:
    return os.path.lexists(Path(pack_dir) / MEDIA_DIR)


def _only_images(relative: str) -> Optional[str]:
    if PurePosixPath(relative).suffix.lower() not in IMAGE_SUFFIXES:
        return f"only image files are published ({', '.join(sorted(IMAGE_SUFFIXES))})"
    return None


def _store_folder(root: Path, store_slug: str) -> Path:
    if not root.is_dir():
        raise PackFilesError(f"content root not found: {root} (setting CONTENT_ROOT)")
    dest = root.resolve() / check_slug(store_slug)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        raise PackFilesError(f"{dest} must be a real folder (not a symbolic link or a file)")
    return dest


def _check_target(dest: Path, relative: str) -> Path:
    """``dest / relative``, refusing a path through a symbolic link (it could leave ``dest``)."""
    current = dest
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise PackFilesError(
                f"{relative}: {current} must be a real folder (not a symbolic link or a file); nothing was published"
            )
    target = dest / relative
    if target.is_dir() and not target.is_symlink():
        raise PackFilesError(f"{relative}: a folder with that name exists in {dest}")
    return target


def stage_media(pack_dir: Path, store_slug: str, root: Path) -> StagedMedia:
    """Check the pack's ``media/`` folder and copy its images into a staging folder in ``root``."""
    source = pack_subfolder(pack_dir, f"{MEDIA_DIR}/")
    dest = _store_folder(Path(root), store_slug)
    staged = Path(tempfile.mkdtemp(prefix=f".{store_slug}.media-", dir=dest.parent))
    try:
        files = copy_tree(source, staged, check_file=_only_images)
        for relative in files:
            _check_target(dest, relative)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return StagedMedia(store_slug=store_slug, dest=dest, path=staged, files=files)


def discard_media(staged: StagedMedia) -> None:
    shutil.rmtree(staged.path, ignore_errors=True)


def publish_media(staged: StagedMedia) -> Path:
    """Move the staged files to ``CONTENT_ROOT/<store-slug>/`` (one rename per file: a request sees
    the old file or the new one). Returns that folder."""
    try:
        dest = _store_folder(staged.dest.parent, staged.store_slug)
        for relative in staged.files:  # checked again: the folder may have changed since staging
            _check_target(dest, relative)
        for relative in staged.files:
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged.path / relative, target)
    finally:
        discard_media(staged)
    return dest


def referenced_content_paths(pack: dict[str, Any]) -> list[str]:
    """The content-app paths a pack uses: ``store.theme.logo_url`` and image property values that
    start with ``/content/`` (sorted, without duplicates)."""
    found = set()
    logo = ((pack.get("store") or {}).get("theme") or {}).get("logo_url")
    if isinstance(logo, str) and logo.startswith(CONTENT_PREFIX):
        found.add(logo)
    image_keys = {
        (category.get("slug"), definition.get("key"))
        for category in pack.get("categories") or []
        for definition in category.get("property_definitions") or []
        if definition.get("prop_type") == "image"
    }
    for product in pack.get("products") or []:
        for key, value in (product.get("values") or {}).items():
            if (product.get("category_slug"), key) in image_keys and isinstance(value, str) \
                    and value.startswith(CONTENT_PREFIX):
                found.add(value)
    return sorted(found)


def _is_file_exact_case(root: Path, parts: list[str]) -> bool:
    """``root/<parts>`` is a file whose every name matches **exactly**, case included.

    ``Path.is_file`` alone is not enough: macOS (APFS) and Windows file systems ignore case by
    default, so ``Tiny-Gadget.png`` would find ``tiny-gadget.png`` there, while the content app on a
    Linux server (and URLs in general) would answer 404. Each name is looked up in its folder's
    listing instead, so every OS reports the same images as missing."""
    folder = root
    for name in parts:
        try:
            if name not in os.listdir(folder):
                return False
        except OSError:  # not a folder, or not readable
            return False
        folder = folder / name
    return folder.is_file()


def missing_content_files(pack: dict[str, Any], root: Path) -> list[str]:
    """The pack's ``/content/<slug>/<file>`` paths with no file at ``root/<slug>/<file>`` (names
    compared case-sensitively, on every OS): they would show as broken images. A path with ``..``
    counts as missing (never looked up)."""
    missing = []
    for path in referenced_content_paths(pack):
        parts = path[len(CONTENT_PREFIX):].split("/")
        if any(part in ("", ".", "..") for part in parts) or not _is_file_exact_case(root, parts):
            missing.append(path)
    return missing
