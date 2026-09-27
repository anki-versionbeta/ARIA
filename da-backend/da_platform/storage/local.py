"""Filesystem object store, for local development (D24/D25).

Installs nothing and needs no container. Keys map straight onto paths under
`STORAGE_DIR`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from da_platform.storage.base import PayloadTooLarge


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        # Keys are built internally, but a silo-supplied filename reaches this
        # point, so refuse anything that would escape the root.
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError(f"Key escapes the storage root: {key!r}")
        return candidate

    def put(self, key: str, data: bytes) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def put_stream(
        self, key: str, chunks: Iterator[bytes], max_bytes: int | None = None
    ) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with path.open("wb") as handle:
                for chunk in chunks:
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise PayloadTooLarge(
                            f"Upload exceeded {max_bytes} bytes"
                        )
                    handle.write(chunk)
        except PayloadTooLarge:
            # Leave nothing half-written behind for a rejected upload.
            path.unlink(missing_ok=True)
            raise
        return written

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
