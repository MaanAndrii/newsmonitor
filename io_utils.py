"""Shared JSON IO helpers with atomic writes."""

from __future__ import annotations

import json
import os
from typing import Any, Callable


def load_json(path: str, default: Any, on_error: Callable[[Exception], None] | None = None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(default, dict) and isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                return merged
            return data
        except (json.JSONDecodeError, OSError) as exc:
            if on_error:
                on_error(exc)
    write_json(path, default)
    return dict(default) if isinstance(default, dict) else default


def write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
