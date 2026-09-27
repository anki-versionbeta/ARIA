"""Object storage interface.

Deals in keys and bytes only. The run-aware facade that also writes `run_files`
rows lives on the stage context, so a silo never has to know both.

Keys follow `runs/<run_id>/{input,output,media}/<filename>`. Nothing durable is
written to local disk in a deployed environment (spec section 10) — ISO currently
writes cropped figure PNGs to `folders/memry/` and re-reads them later in the same
run, which breaks the moment the container is replaced.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, Protocol, runtime_checkable

INPUT = "input"
OUTPUT = "output"
MEDIA = "media"


def _leaf(filename: str) -> str:
    return filename.replace("\\", "/").split("/")[-1]


def run_key(
    prefix: str,
    run_id: str,
    kind: str,
    filename: str,
    folders: dict[str, str] | None = None,
) -> str:
    """Key for one of a run's files. `kind` is input, output or media.

    Prefixed per silo rather than globally, because the apps being absorbed already
    own separate locations in the same bucket — BOP under `BOP/` and ISO under
    `ISO_Doc_Generation/`. A single global prefix could not serve both.

    A silo may map each kind onto its own folder name via `STORAGE_FOLDERS`, so the
    Dataiku managed-folder layout it already uses is preserved:

        default          BOP/runs/<run_id>/input/manual.pdf
        with folders     BOP/uploads/<run_id>_manual.pdf

    The run id stays in the flat form because those folders are shared: the app being
    replaced writes the bare basename, so two people uploading the same filename
    overwrite each other. ISO already prefixes the id for this reason.
    """
    head = f"{prefix.strip('/')}/" if prefix else ""
    folder = (folders or {}).get(kind)
    if folder:
        return f"{head}{folder.strip('/')}/{run_id}_{_leaf(filename)}"
    return f"{head}runs/{run_id}/{kind}/{_leaf(filename)}"


def asset_key(prefix: str, relative: str) -> str:
    """Key for a silo's own static asset, e.g. `prompts/petra.txt`.

    Mirrors the Dataiku managed-folder names the silos already use, so the same S3
    objects serve both the old app and this platform during migration.
    """
    head = f"{prefix.strip('/')}/" if prefix else ""
    return f"{head}{relative.strip('/')}"


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> int:
        """Store bytes, returning the number written."""
        ...

    def put_stream(self, key: str, chunks: Iterator[bytes], max_bytes: int | None = None) -> int:
        """Store a stream without buffering it whole.

        Raises `PayloadTooLarge` once `max_bytes` is exceeded, so an oversized
        upload cannot exhaust memory before it is rejected.
        """
        ...

    def open(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class StorageError(RuntimeError):
    pass


class PayloadTooLarge(StorageError):
    pass
