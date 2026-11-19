"""The cart's JavaScript logic (app/static/cart/cart-core.js: rage-click guard, quantity batching,
error messages) with Node's built-in test runner: tests/js/cart-core.test.js. Skipped without Node."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
JS_TESTS = sorted((PROJECT / "tests" / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_cart_javascript_logic():
    """Pass / fail is Node's exit status. The reporter is chosen explicitly (TAP): Node's default
    one changed between versions (TAP before Node 23 when not on a terminal, "spec" since), so the
    summary lines are only parsed in a known format, to check that tests did run."""
    assert JS_TESTS, "no tests/js/*.test.js"
    done = subprocess.run(["node", "--test", "--test-reporter=tap", *map(str, JS_TESTS)], cwd=PROJECT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    summary = dict(re.findall(r"^# (tests|pass|fail) (\d+)$", done.stdout, re.M))
    assert int(summary["fail"]) == 0 and int(summary["pass"]) == int(summary["tests"]) > 0, done.stdout


def test_cart_scripts_are_plain_browser_files():
    """No build step: the browser loads these files as they are (no import/export, no JSX)."""
    for name in ("cart.js", "cart-core.js"):
        source = (PROJECT / "app" / "static" / "cart" / name).read_text(encoding="utf-8")
        assert not any(line.startswith(("import ", "export ")) for line in source.splitlines()), name
        assert "<" + "div" not in source.replace('"div"', ""), name  # React.createElement, no JSX


def test_for_testing_runs_the_node_tests():
    """../for_testing runs tests/js with Node (and enforces 90 % on cart-core.js with --cov)."""
    script = (PROJECT.parent / "for_testing")
    if not script.exists():
        pytest.skip("for_testing is not part of this checkout")
    text = script.read_text(encoding="utf-8")
    assert "node --test" in text and "tests/js/*.test.js" in text
    for threshold in ("--test-coverage-lines=90", "--test-coverage-branches=90", "--test-coverage-functions=90"):
        assert threshold in text
    assert "--test-coverage-include='app/static/cart/cart-core.js'" in text
