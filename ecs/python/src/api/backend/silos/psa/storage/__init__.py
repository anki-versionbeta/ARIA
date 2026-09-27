"""PSA's object-store port, wired to the platform inside ARIA.

`get_object_store()` is the single symbol that differs from the standalone package, which builds a
`LocalObjectStore` rooted at a configured directory. That implementation does not travel: ARIA supplies
its own, local filesystem or S3, chosen by `STORAGE_BACKEND`.

`base.py` DID travel. It is PSA's declaration of the subset it uses, copied from the platform's storage
base signature-for-signature — including `put(key, data) -> int` returning the byte count, which PSA got
wrong once and which no local test could have caught.
"""
from __future__ import annotations

from api.backend.da_platform.storage import get_object_store as _platform_store

from .base import ObjectStore, StorageError, asset_key

__all__ = ["ObjectStore", "StorageError", "asset_key", "get_object_store"]


def get_object_store() -> ObjectStore:
    """The platform's object store — local filesystem or S3, chosen by STORAGE_BACKEND."""
    return _platform_store()
