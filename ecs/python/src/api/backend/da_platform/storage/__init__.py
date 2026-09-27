from __future__ import annotations

from api.backend.da_platform.settings import settings
from api.backend.da_platform.storage.base import (
    INPUT,
    MEDIA,
    OUTPUT,
    ObjectStore,
    PayloadTooLarge,
    StorageError,
    run_key,
)
from api.backend.da_platform.storage.local import LocalObjectStore

__all__ = [
    "INPUT",
    "MEDIA",
    "OUTPUT",
    "LocalObjectStore",
    "ObjectStore",
    "PayloadTooLarge",
    "StorageError",
    "get_object_store",
    "run_key",
]

_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    """The configured backend. Local for development, S3 elsewhere (D24)."""
    global _store
    if _store is None:
        if settings.storage_backend == "s3":
            # Imported lazily so local development never needs botocore configured.
            from api.backend.da_platform.storage.s3 import S3ObjectStore

            _store = S3ObjectStore()
        else:
            settings.storage_dir.mkdir(parents=True, exist_ok=True)
            _store = LocalObjectStore(settings.storage_dir)
    return _store


def reset_object_store() -> None:
    """Drop the cached backend. Used by tests that change configuration."""
    global _store
    _store = None
