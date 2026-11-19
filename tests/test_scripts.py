"""The shell scripts run as ``./script``: executable on disk and recorded as executable (100755) in git,
so a fresh checkout or an archive of it keeps the bit (``./for_testing`` failed with 'Permission denied')."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = [PROJECT.parent / "for_testing", PROJECT / "scripts" / "pack_archive.sh"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable(script):
    if not script.exists():
        pytest.skip(f"{script} is not part of this checkout")
    assert script.read_bytes().startswith(b"#!/usr/bin/env bash\n"), "shebang missing (or CRLF line ending)"
    assert os.access(script, os.X_OK), f"not executable: run  chmod 755 {script}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable_in_git(script):
    if not script.exists() or shutil.which("git") is None:
        pytest.skip("no git or no script")
    result = subprocess.run(
        ["git", "ls-files", "-s", script.name], cwd=script.parent, capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or not result.stdout:
        pytest.skip("not in a git checkout")
    mode = result.stdout.split()[0]
    assert mode == "100755", f"git records {mode}: run  git update-index --chmod=+x {script}"
