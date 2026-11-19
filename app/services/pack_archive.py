"""A store pack uploaded as a .zip of its folder: unpack it safely into a temporary folder, so the
normal import can copy ``storefront/`` and publish ``media/`` from it (like the command line).

Accepted layouts (what "compress folder" / ``zip -r pack.zip storefront-demo/`` produce)::

    storefront-demo/storefront-demo.store-pack.v1.json     or     storefront-demo.store-pack.v1.json
    storefront-demo/media/…                                        media/…
    storefront-demo/storefront/…                                   storefront/…

i.e. exactly one ``*.store-pack.v1.json`` at the top of the zip, or at the top of its single
top-level folder. ``__MACOSX/`` and hidden entries (``.DS_Store``) are skipped.

Refused (``StorePackError`` with an issue at path ``zip``; nothing is imported): not a zip, an
unsafe entry name (absolute, ``..``, drive letters, backslashes), symbolic links or other special
entries, more than :data:`MAX_ENTRIES` entries, more than :data:`MAX_EXTRACTED_BYTES` once
unpacked (counted while unpacking, so a lying header can't get past it), or a pack file larger
than ``max_pack_bytes``.
"""

from __future__ import annotations

import io
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.services.store_pack_validation import PACK_FILE_SUFFIX, PackIssue, StorePackError

ZIP_ISSUE_PATH = "zip"
MAX_ENTRIES = 2000
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
_CHUNK = 64 * 1024


def _refuse(message: str) -> StorePackError:
    return StorePackError([PackIssue(ZIP_ISSUE_PATH, message)])


def _parts(name: str) -> list[str]:
    """The path parts of a zip entry name, refusing anything that could leave the folder."""
    if "\\" in name or ":" in name or name.startswith("/") or "\0" in name:
        raise _refuse(f"unsafe path in the zip: {name!r}")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _refuse(f"unsafe path in the zip: {name!r}")
    return parts


def _skipped(parts: list[str]) -> bool:
    return parts[0] == "__MACOSX" or any(part.startswith(".") for part in parts)


def _unpack(raw: bytes, root: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError) as exc:
        raise _refuse(f"not a valid zip file ({exc})") from exc
    with archive:
        entries = []
        for info in archive.infolist():
            parts = _parts(info.filename)  # checked even for skipped entries
            if not _skipped(parts):
                entries.append((info, parts))
        if len(entries) > MAX_ENTRIES:
            raise _refuse(f"more than {MAX_ENTRIES} files and folders")
        total = 0
        for info, parts in entries:
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind == stat.S_IFLNK:
                raise _refuse(f"{info.filename}: symbolic links are not allowed in the zip")
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise _refuse(f"{info.filename}: not a regular file")
            target = root.joinpath(*parts)
            clash = _refuse(f"{info.filename}: a file and a folder have the same name")
            try:
                (target if info.is_dir() else target.parent).mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError) as exc:  # a file is where a folder goes
                raise clash from exc
            if info.is_dir():
                continue
            if target.is_dir():
                raise clash
            try:
                with archive.open(info) as source, open(target, "xb") as out:
                    while chunk := source.read(_CHUNK):
                        total += len(chunk)
                        if total > MAX_EXTRACTED_BYTES:
                            raise _refuse(f"more than {MAX_EXTRACTED_BYTES} bytes once unpacked")
                        out.write(chunk)
            except FileExistsError as exc:
                raise _refuse(f"{info.filename}: appears twice in the zip") from exc
            except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError) as exc:
                raise _refuse(f"{info.filename}: can't be read ({exc}); not a valid zip file") from exc


def _pack_file(root: Path) -> Path:
    def candidates(folder: Path) -> list[Path]:
        return sorted(p for p in folder.iterdir() if p.is_file() and p.name.endswith(PACK_FILE_SUFFIX))

    found = candidates(root)
    top = list(root.iterdir())
    if not found and len(top) == 1 and top[0].is_dir():  # zip of a folder: look inside it
        found = candidates(top[0])
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise _refuse(f"more than one *{PACK_FILE_SUFFIX} file at the top of the zip ({names}); a zip holds one pack")
    if not found:
        raise _refuse(f"no store-pack file (*{PACK_FILE_SUFFIX}) at the top of the zip or of its single top-level "
                      "folder")
    return found[0]


@contextmanager
def extract_pack_archive(raw: bytes, max_pack_bytes: Optional[int] = None) -> Iterator[tuple[bytes, Path]]:
    """Unpack the zip ``raw`` into a temporary folder (removed afterwards). Yields the pack file's
    bytes and its folder (the ``pack_dir`` of the import)."""
    with tempfile.TemporaryDirectory(prefix="magic-stores-pack-") as tmp:
        root = Path(tmp)
        _unpack(raw, root)
        pack_file = _pack_file(root)
        if max_pack_bytes is not None and pack_file.stat().st_size > max_pack_bytes:
            raise _refuse(f"{pack_file.name}: larger than {max_pack_bytes} bytes")
        yield pack_file.read_bytes(), pack_file.parent
