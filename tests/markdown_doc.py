"""Reading markdown documentation in tests (the READMEs are checked against the code: see
tests/test_pack_docs.py and tests/test_feature_docs.py)."""

from __future__ import annotations

import re


def section(text: str, heading: str) -> str:
    """The body of a markdown section, until the next heading of the same or a higher level.
    Lines inside ``` code blocks are not headings (e.g. "# expected output" comments)."""
    lines = text.splitlines(keepends=True)
    start = level = None
    in_code = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
            continue
        heading_match = None if in_code else re.match(r"^(#+) (.*)$", line.rstrip("\n"))
        if heading_match is None:
            continue
        if start is None and heading_match.group(2) == heading:
            start, level = index + 1, len(heading_match.group(1))
        elif start is not None and len(heading_match.group(1)) <= level:
            return "".join(lines[start:index])
    assert start is not None, f"heading not found: {heading}"
    return "".join(lines[start:])


def code_blocks(text: str, language: str | None = None) -> list[str]:
    """The contents of the ``` blocks in ``text`` (only ```<language> ones if given)."""
    pattern = rf"```{re.escape(language)}\n(.*?)```" if language else r"```[a-z]*\n(.*?)```"
    return re.findall(pattern, text, re.S)


def table_rows(text: str) -> list[list[str]]:
    """The cells of every table row in ``text``, without header and separator rows."""
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|\s*-", line):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # without the header
