"""The object-store port: what PSA needs in order to persist something durably.

Deliberately a **subset** of ARIA's `da_platform/storage/base.py` — the same method names with the same
signatures and semantics, minus the operations PSA does not perform (no `put_stream`; the palette
override is a small JSON blob, not an upload). Because it is a strict subset, ARIA's store satisfies
this Protocol, so the Stage C port repoints `get_object_store()` at `da_platform.storage` and changes no
call site.

"Strict" is load-bearing and was got wrong once: this file first declared
`put(key, data, content_type=None) -> str`, and `palette.py` duly passed `content_type="application/json"`.
ARIA's is `put(key, data) -> int` (`da_platform/storage/base.py:68-70`, returning the number of bytes
written), so the port would have raised `TypeError: put() got an unexpected keyword argument` on the
first palette edit inside ARIA — a failure that no local test could see. Any method added here must be
copied from ARIA's Protocol signature-for-signature, not merely named after it.

Keys are '/'-separated and prefixed by the silo, exactly as in ARIA: `asset_key(STORAGE_PREFIX, rel)`.

The reason this exists at all, in ARIA's words (`da_platform/storage/base.py`): "Nothing durable is
written to local disk in a deployed environment." The previous palette editor wrote to
`~/.psa/cap_palette.csv`, which is precisely that anti-pattern — a file that survives on a developer's
laptop and vanishes when a container is replaced.
"""
from __future__ import annotations

from typing import IO, Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Any failure to read or write an object."""


@runtime_checkable
class ObjectStore(Protocol):
    """Durable key/value blob storage."""

    def put(self, key: str, data: bytes) -> int:
        """Store bytes, returning the number written. Replaces anything already at `key`."""
        ...

    def open(self, key: str) -> IO[bytes]:
        """Open `key` for reading. Raises `StorageError` when it does not exist."""
        ...

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> None:
        """Remove `key`. Absent is not an error — deletes are idempotent."""
        ...


def asset_key(prefix: str, relative: str) -> str:
    """Namespace a silo-relative path into a store key. Mirrors ARIA's helper of the same name."""
    return f"{prefix.strip('/')}/{relative.strip('/')}"
