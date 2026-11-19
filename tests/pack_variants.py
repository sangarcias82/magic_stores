"""Single-field variants of a store pack, for robustness tests: every field at every depth is
deleted, or replaced with a value of each wrong type. (A helper module, not a test module.)"""

from __future__ import annotations

import copy
from typing import Any, Iterator

WRONG_VALUES: list[Any] = [None, 0, -1, 1.5, True, "", "x", [], {}]
DELETE = object()


def _paths(data: Any, prefix: tuple = ()) -> Iterator[tuple]:
    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else ()
    for key, value in items:
        yield prefix + (key,)
        yield from _paths(value, prefix + (key,))


def variants(pack: dict) -> Iterator[tuple[str, dict]]:
    """(description, changed copy of the pack) for every path x (delete + each wrong value)."""
    for path in list(_paths(pack)):
        for change in (DELETE, *WRONG_VALUES):
            trial = copy.deepcopy(pack)
            parent = trial
            for key in path[:-1]:
                parent = parent[key]
            if change is DELETE:
                parent.pop(path[-1])  # works for lists (index) and dicts (key)
            else:
                parent[path[-1]] = change
            where = "".join(f"[{k}]" if isinstance(k, int) else f".{k}" for k in path).lstrip(".")
            yield f"{where} {'deleted' if change is DELETE else '= ' + repr(change)}", trial
