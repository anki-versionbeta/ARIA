"""The single place the pipeline decides WHERE to read and write.

Every accessor is a function, not a constant. That is the whole difference from the previous version:
a module-level `DB = ...` read from the process environment at import time, so the writable locations were
frozen before any caller could influence them, and a host that needed different paths had to configure it
before the first import. Now a path is resolved when it is used, from `config.config()`.

Shipped read-only assets (the PSA template, the schema, the default cap palette) are NOT here — they go
through `asset_store`, which is the port ARIA replaces with `ctx.assets`.
"""
from __future__ import annotations

import os

from .config import config


def root() -> str:
    """Base for repo input data (`Input_Data_Sources/...`). Read-only in a deployed environment."""
    return config().root


def db_path() -> str:
    """psa.db — a DERIVED artifact, dropped and recreated by `build_db.main()` on every run."""
    return config().db_path


def output_dir() -> str:
    """Generated reports (.docx) and exports."""
    return config().output_dir


def uploads_dir() -> str:
    return config().uploads_dir


def image_dir() -> str:
    """Product images pulled from Smartsheet (`<output>/assets`)."""
    return config().image_dir


def ensure_dirs() -> None:
    """Create the writable directories if missing. Safe to call on every run."""
    cfg = config()
    for d in (os.path.dirname(cfg.db_path), cfg.output_dir, cfg.uploads_dir, cfg.image_dir):
        os.makedirs(d, exist_ok=True)
