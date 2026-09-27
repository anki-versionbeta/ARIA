"""What a silo stage receives.

The four capabilities spec section 5 names — `ctx.llm`, `ctx.storage`,
`ctx.progress()`, `ctx.pause_for_user()` — plus `ctx.assets` and `ctx.sections` for
a silo's static objects and generated content, `ctx.checkpoint()` to read an earlier
stage's output, and `ctx.textract` for per-page document analysis.

What matters is not the count but that every one of them is *provided*: a silo never
constructs a credential, names a bucket or picks a rate limit, so those exist in one
place. `tests/test_silo_isolation.py` fails the build if a silo tries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from api.backend.da_platform.aws import get_clients
from api.backend.da_platform.db.models import Run, RunFile
from api.backend.da_platform.engine import queue
from api.backend.da_platform.llm.client import IliadClient, get_client
from api.backend.da_platform.records import sections as sections_service
from api.backend.da_platform.storage import INPUT, MEDIA, OUTPUT, get_object_store, run_key
from api.backend.da_platform.storage.base import ObjectStore, asset_key
from api.backend.da_platform.warehouse import get_warehouse

logger = logging.getLogger(__name__)


class Pause:
    """Returned by `ctx.pause_for_user()`. A sentinel, not an exception, so a stage
    can do work and *then* decide to park."""

    __slots__ = ("message",)

    def __init__(self, message: str | None = None) -> None:
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Pause({self.message!r})"


class SiloAssets:
    """Read-only access to a silo's own static objects.

    BOP needs its PETRA prompt, its docx template and its 25 MB gold calibration
    document. Those live beside the silo's other objects in the shared bucket, under
    the Dataiku managed-folder names the app already used (`prompts/`, `templates/`,
    `gold/`), so the same objects serve the old app and this platform during migration.

    Cached per process: the gold document is large and is read on every run.
    """

    def __init__(self, store: ObjectStore, prefix: str) -> None:
        self._store = store
        self._prefix = prefix
        self._cache: dict[str, bytes] = {}

    def read(self, relative: str, cache: bool = True) -> bytes:
        if cache and relative in self._cache:
            return self._cache[relative]
        key = asset_key(self._prefix, relative)
        with self._store.open(key) as handle:
            data = handle.read()
        if cache:
            self._cache[relative] = data
        return data

    def read_text(self, relative: str, cache: bool = True) -> str:
        return self.read(relative, cache=cache).decode("utf-8", errors="replace")

    def exists(self, relative: str) -> bool:
        return self._store.exists(asset_key(self._prefix, relative))


class RunStorage:
    """Run-aware storage facade.

    Wraps the byte-level `ObjectStore` and also maintains `run_files` rows, so a silo
    makes one call instead of remembering to do both.
    """

    def __init__(
        self,
        session: Session,
        store: ObjectStore,
        prefix: str = "",
        folders: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._prefix = prefix
        self._folders = folders or {}

    def open_input(self, run: Run, filename: str | None = None) -> BinaryIO:
        record = self._input_record(run, filename)
        if record is None:
            raise FileNotFoundError(f"Run {run.id} has no input file")
        return self._store.open(record.storage_key)

    def read_input(self, run: Run, filename: str | None = None) -> bytes:
        with self.open_input(run, filename) as handle:
            return handle.read()

    def input_filename(self, run: Run) -> str | None:
        record = self._input_record(run, None)
        return record.filename if record else None

    def attach_output(
        self,
        run: Run,
        filename: str,
        data: bytes,
        content_type: str | None = None,
    ) -> RunFile:
        key = run_key(self._prefix, run.id, OUTPUT, filename, self._folders)
        size = self._store.put(key, data)
        record = RunFile(
            run_id=run.id,
            kind=OUTPUT,
            filename=filename,
            storage_key=key,
            size_bytes=size,
            content_type=content_type,
        )
        self._session.add(record)
        self._session.commit()
        return record

    def put_media(self, run: Run, name: str, data: bytes) -> str:
        """Store an intermediate artefact and return its key.

        No `run_files` row: media is working state, not a deliverable. This is where
        ISO's cropped figure PNGs go instead of a local `memry/` folder, which does
        not survive the container being replaced (spec section 10).
        """
        key = run_key(self._prefix, run.id, MEDIA, name, self._folders)
        self._store.put(key, data)
        return key

    def open_key(self, key: str) -> BinaryIO:
        return self._store.open(key)

    def _input_record(self, run: Run, filename: str | None) -> RunFile | None:
        candidates = [item for item in run.files if item.kind == INPUT]
        if filename is not None:
            candidates = [item for item in candidates if item.filename == filename]
        return candidates[0] if candidates else None


class RunSections:
    """How a silo hands generated content to the platform.

    The silo produces a `{section_key: html}` mapping and stops caring: editing,
    version history, restore and the ownership rules are all platform-owned (spec §7).
    """

    def __init__(self, session: Session, run: Run) -> None:
        self._session = session
        self._run = run

    def write(self, content: dict[str, str], label: str = "Generated") -> int:
        return sections_service.seed_sections(
            self._session, self._run.id, content, self._run.user_id, label
        )

    def read(self) -> dict[str, str]:
        return {
            row.section_key: row.content_html
            for row in sections_service.list_sections(self._session, self._run.id)
        }

    def rows(self) -> list[tuple[str, str, int]]:
        """(key, html, revision) per section.

        The revision matters at build time: a silo can tell an edited section from an
        untouched one, and so avoid overwriting generated structure with a rendering
        of itself.
        """
        return [
            (row.section_key, row.content_html, row.revision)
            for row in sections_service.list_sections(self._session, self._run.id)
        ]


class StageContext:
    def __init__(
        self,
        session: Session,
        run: Run,
        llm: IliadClient | None = None,
        store: ObjectStore | None = None,
        storage_prefix: str = "",
        storage_folders: dict[str, str] | None = None,
        textract: Callable[[bytes], Any] | None = None,
        warehouse: Any | None = None,
    ) -> None:
        self._session = session
        self._run = run
        resolved_store = store or get_object_store()
        self.llm = llm or get_client()
        self.storage = RunStorage(
            session, resolved_store, storage_prefix, storage_folders
        )
        self.assets = SiloAssets(resolved_store, storage_prefix)
        self.sections = RunSections(session, run)
        self._textract = textract
        self._warehouse = warehouse

    @property
    def textract(self) -> Callable[[bytes], Any]:
        """Analyse one rendered page: `ctx.textract(png_bytes)`.

        ISO calls this once per page of a garbled document, so a 200-page scan is 200
        billed page analyses — which is why the concurrency ceiling and the
        credential-expiry retry live in the platform rather than in the silo.

        Resolved on first use, not in the constructor: BOP never analyses a page, and
        every run builds a context, so construction must not require AWS credentials.
        """
        if self._textract is None:
            self._textract = get_clients().analyze_document
        return self._textract

    @property
    def warehouse(self) -> Any:
        """Read-only CMC data warehouse access (spec phase 5).

        The ATR/MFGR reports are built from Oracle rather than from an uploaded document.
        A silo may not open that connection itself — the isolation test forbids it and
        the credentials belong in one place — so it arrives here.

        Resolved on first use: most silos never query the warehouse, and every run builds
        a context, so construction must not need a database or its credentials.
        """
        if self._warehouse is None:
            self._warehouse = get_warehouse()
        return self._warehouse

    def progress(self, message: str, pct: int | None = None) -> None:
        """Report progress, which the browser sees on its next 2s poll.

        Doubles as a heartbeat: a long stage that reports progress keeps its claim
        alive for free, so only stages that are silent for a long time risk being
        re-queued by the reaper.
        """
        self._run.progress_message = message
        if pct is not None:
            self._run.progress_pct = max(0, min(100, int(pct)))
        queue.heartbeat(self._session, self._run)
        logger.info("run %s: %s (%s%%)", self._run.id, message, self._run.progress_pct)

    def pause_for_user(self, message: str | None = None) -> Pause:
        """Park the run until a person acts. Replaces holding a thread.

        The runner writes no checkpoint for this stage; whatever endpoint accepts the
        user's answer records it and re-queues the run.
        """
        return Pause(message)

    def checkpoint(self, stage: str) -> dict | None:
        """Read a previous stage's output, including one written by a user action."""
        return queue.checkpoint_output(self._session, self._run.id, stage)

    @property
    def session(self) -> Session:
        """Escape hatch for silos that must persist their own rows. Using it is a
        signal that something belongs in the platform instead."""
        return self._session
