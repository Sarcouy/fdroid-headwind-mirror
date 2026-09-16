from __future__ import annotations

from typing import Any


def merge_diff(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_diff(base[key], value)
        else:
            base[key] = value
    return base
