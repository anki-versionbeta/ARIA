"""Read-only access to the assets that ship with the silo.

Modelled on ARIA's `SiloAssets` (`da_platform/engine/context.py`), which exposes exactly
`read` / `read_text` / `exists` and **no `put`** — a silo reads its own assets and never rewrites them.
That constraint is the reason the cap-palette override is written by the router through an object store
rather than by the palette module itself.

Backed here by the package's `assets/` directory. At Stage C the three functions delegate to
`ctx.assets`, and every caller stays as it is.

Assets shipped today:
  cap_palette.csv     the DEFAULT supplier cap palette (65 rows) — see `palette.py`
  schema.sql          the canonical SQLite schema
  PSA_template.docx   the blank QPP11-04-001-G004-F01 assessment form
"""
from __future__ import annotations

import os
import threading

from .config import ASSETS_DIR

_LOCK = threading.Lock()
_CACHE: dict[str, bytes] = {}


def _resolve(relative: str) -> str:
    """Absolute path of a shipped asset, refusing anything that escapes the assets directory."""
    full = os.path.abspath(os.path.join(ASSETS_DIR, relative))
    if not full.startswith(os.path.abspath(ASSETS_DIR) + os.sep):
        raise ValueError(f"asset path escapes the assets directory: {relative!r}")
    return full


def exists(relative: str) -> bool:
    try:
        return os.path.isfile(_resolve(relative))
    except ValueError:
        return False


def read(relative: str, cache: bool = True) -> bytes:
    """The asset's bytes.

    Cached by default: these files are immutable for the life of a deployment. `cache=False` is for the
    rare caller that wants to see an edit made while the process is running.
    """
    if cache:
        with _LOCK:
            hit = _CACHE.get(relative)
        if hit is not None:
            return hit
    with open(_resolve(relative), "rb") as fh:
        data = fh.read()
    if cache:
        with _LOCK:
            _CACHE[relative] = data
    return data


def read_text(relative: str, cache: bool = True, encoding: str = "utf-8") -> str:
    return read(relative, cache=cache).decode(encoding)
