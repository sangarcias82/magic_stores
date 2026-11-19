"""Copy a store pack's storefront folder (``storefront.copy_from_pack_dir``) into
``app/storefronts/<store-slug>/``.

Two steps, so a pack import stays all-or-nothing:

1. :func:`stage_storefront` (before the database commit) checks the source folder and copies it
   into a hidden staging folder next to the destination. Any problem raises
   :class:`StorefrontCopyError`, and nothing is installed.
2. :func:`publish_storefront` (after the commit) swaps the staged copy in place of
   ``app/storefronts/<store-slug>/``, or :func:`discard_staged` removes it if the import failed.

The pack's folder **replaces** the store's folder: files of an earlier copy that the pack no
longer has are removed (a stale template override would otherwise keep overriding).

Path safety (see :mod:`app.services.pack_files`) is checked on the real filesystem, not only by
the pack validator: the folder must stay inside the pack folder, symbolic links and special files
are refused, hidden files are not copied, and the destination is always a direct child of
``STOREFRONTS_ROOT`` named after a valid store slug.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services import storefront
from app.services.pack_files import PackFilesError, check_slug, copy_tree, pack_subfolder

StorefrontCopyError = PackFilesError  # the storefront folder can't be copied; nothing was installed


@dataclass
class StagedStorefront:
    store_slug: str
    path: Path  # hidden folder inside STOREFRONTS_ROOT holding the copy
    files: int  # number of files copied


def destination(store_slug: str) -> Path:
    """``STOREFRONTS_ROOT/<store_slug>``, refusing anything that isn't a plain store slug."""
    return Path(storefront.STOREFRONTS_ROOT) / check_slug(store_slug)


def stage_storefront(pack_dir: Path, folder: str, store_slug: str) -> StagedStorefront:
    """Check and copy the pack's folder into a hidden staging folder (not used by any store yet)."""
    source = pack_subfolder(pack_dir, folder)
    dest = destination(store_slug)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{store_slug}.staging-", dir=dest.parent))
    try:
        files = len(copy_tree(source, staged))
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return StagedStorefront(store_slug=store_slug, path=staged, files=files)


def discard_staged(staged: StagedStorefront) -> None:
    shutil.rmtree(staged.path, ignore_errors=True)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path, ignore_errors=True)


def publish_storefront(staged: StagedStorefront) -> Path:
    """Put the staged copy in place of ``STOREFRONTS_ROOT/<slug>`` (renames in the same folder:
    a request sees the old folder or the new one, never a half-copied one). Returns the folder."""
    dest = destination(staged.store_slug)
    old = None
    if dest.exists() or dest.is_symlink():
        old = dest.parent / f".{staged.store_slug}.old-{os.getpid()}-{staged.path.name.rsplit('-', 1)[-1]}"
        os.replace(dest, old)
    try:
        os.replace(staged.path, dest)
    except BaseException:
        if old is not None:
            os.replace(old, dest)  # put the previous folder back
        raise
    if old is not None:
        _remove(old)
    return dest
