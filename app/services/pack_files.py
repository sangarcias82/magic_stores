"""Safe copying of folders that ship next to a store pack file (``storefront/``, ``media/``).

Shared by :mod:`app.services.storefront_install` (storefront folder -> ``app/storefronts/<slug>/``)
and :mod:`app.services.media_publish` (``media/`` -> the content app's ``content/<slug>/``).
Path safety is checked on the real filesystem, independently of the pack validator:

- a pack folder must be a relative path *inside* the pack folder (no absolute paths, ``~``, ``\\``,
  ``:`` or ``..``; the pack folder itself is not allowed either), also after resolving symlinks;
- symbolic links inside it are refused (they could point at any file on the server), and so is
  anything that is not a plain file or folder;
- hidden files and folders (``.DS_Store``, ``.git``, …) are not copied;
- destinations are named after a valid store slug.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from app.services.store_admin import SLUG_MAX, SLUG_RE


class PackFilesError(ValueError):
    """A pack folder can't be copied (the message says why); nothing was installed."""


def check_slug(store_slug: object) -> str:
    """The store slug, if it is safe as a single folder name (a valid store slug)."""
    if not isinstance(store_slug, str) or len(store_slug) > SLUG_MAX or not SLUG_RE.fullmatch(store_slug):
        raise PackFilesError(f"invalid store slug for a storefront folder: {store_slug!r}")
    return store_slug


def pack_subfolder(pack_dir: Path, folder: str) -> Path:
    """``pack_dir / folder``, which must be an existing folder inside ``pack_dir``."""
    if not isinstance(folder, str) or not folder.strip():
        raise PackFilesError("must be a folder path inside the pack")
    relative = PurePosixPath(folder)
    if relative.is_absolute() or folder.startswith("~") or "\\" in folder or ":" in folder:
        raise PackFilesError('must be a relative folder inside the pack, like "storefront/"')
    if ".." in relative.parts:
        raise PackFilesError('must not leave the pack folder ("..")')
    base = Path(pack_dir).resolve()
    source = (base / relative).resolve()  # follows symlinks: a linked folder must stay inside too
    if source == base:
        raise PackFilesError('must be a folder inside the pack, not the pack folder itself, like "storefront/"')
    if base not in source.parents:
        raise PackFilesError("must not leave the pack folder (it resolves outside it, e.g. through a symlink)")
    if not source.is_dir():
        raise PackFilesError(f'folder not found: "{folder}" in {base}')
    return source


def copy_tree(source: Path, target: Path, check_file: Optional[Callable[[str], Optional[str]]] = None) -> list[str]:
    """Copy plain files and folders from ``source`` into the existing folder ``target``; refuse
    symlinks and special files; skip hidden entries. ``check_file(relative_path)`` may return a
    reason to refuse a file. Returns the copied files' relative paths (POSIX style)."""
    copied = []
    for current, dirnames, filenames in os.walk(source):  # does not follow symlinked folders
        here = Path(current)
        rel_dir = here.relative_to(source)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in dirnames + sorted(filenames):
            if name.startswith("."):
                continue
            entry = here / name
            shown = (rel_dir / name).as_posix()
            if entry.is_symlink():
                raise PackFilesError(f"{shown}: symbolic links are not copied (copy the real file instead)")
            if entry.is_dir():
                (target / rel_dir / name).mkdir()
            elif entry.is_file():
                problem = check_file(shown) if check_file else None
                if problem:
                    raise PackFilesError(f"{shown}: {problem}")
                shutil.copyfile(entry, target / rel_dir / name)  # content only: fresh mtime, default mode
                copied.append(shown)
            else:
                raise PackFilesError(f"{shown}: not a regular file")
    return copied
