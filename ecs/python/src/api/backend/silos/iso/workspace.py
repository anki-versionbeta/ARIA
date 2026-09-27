"""A local scratch copy of the run's PDF, because PyMuPDF needs a real path.

The ported ISO code calls `fitz.open(pdf_path)` in eleven places (backend.py:425, 533,
693, 1575, 1642, 1803, 1955, 3232, 3354, 4207, 5166) and `fitz` cannot open an
object-store key. Each stage therefore materialises the PDF into a temporary directory,
does its work, and throws the directory away.

Re-materialised per stage on purpose. A paused run resumes on whatever worker claims it
next, so scratch written by `ingest` is simply not there for `build`; assuming otherwise
is the bug this class exists to make impossible. Anything worth keeping goes to
`ctx.storage` (spec section 10).

This also replaces ISO's `session_id` plumbing. `_get_work_dir()` (backend.py:120-131)
resolved the memry folder's local path and `_session_media_dir(session_id)` (2191-2197)
appended the uuid; `session_id` was then threaded through `prescan_media(...)` purely to
derive that directory. The ported media code takes this workspace instead — the one
signature change permitted in it.

`put_media` is `_write_media` (2200-2213): it writes the local file **and** the durable
copy. The local write is kept because downstream ISO code passes file paths around and
`_media_as_bytes` (2216-2221) reads them back; removing it would change the data flowing
through the figures/tables/formulas structures. As in the source, a failure of the
durable copy is a warning rather than an error.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCRATCH_PREFIX = "iso_run_"
# Mirrors the managed-folder name, so a path in a log line still reads like ISO's.
MEDIA_DIRNAME = "memry"
FALLBACK_PDF_NAME = "source.pdf"

# The progress callback the long, silent ported loops take. One argument on purpose:
# the stage owns the percentage band, an inner loop only refreshes the message — which
# is also the heartbeat that keeps the reaper away (stale_claim_timeout_s is 900s).
ProgressFn = Callable[[str], None]


def no_progress(message: str) -> None:
    """The default `progress=` for ported functions, so they stay importable alone."""


def _leaf(filename: str) -> str:
    """Basename only, mirroring the platform's own key builder so the local name and
    the object name always agree."""
    return filename.replace("\\", "/").split("/")[-1]


class Workspace:
    """A run's scratch tree. Valid only inside its `open_run(...)` block."""

    __slots__ = (
        "root",
        "pdf_path",
        "source_name",
        "run_id",
        "progress",
        "_run",
        "_ctx",
        "_media_dir",
    )

    def __init__(
        self,
        *,
        root: Path,
        pdf_path: Path,
        source_name: str,
        run: Any,
        ctx: Any,
        progress: ProgressFn,
    ) -> None:
        # Strings rather than Paths: the ported code passes these straight into
        # fitz.open() and os.path.join().
        self.root = str(root)
        self.pdf_path = str(pdf_path)
        self.source_name = source_name
        self.run_id = run.id
        self.progress = progress
        self._run = run
        self._ctx = ctx
        self._media_dir: str | None = None

    @property
    def media_dir(self) -> str:
        """`_session_media_dir(session_id)` (backend.py:2191-2197), created on first use
        so a text-only run never makes the directory at all."""
        if self._media_dir is None:
            path = Path(self.root) / MEDIA_DIRNAME
            path.mkdir(parents=True, exist_ok=True)
            self._media_dir = str(path)
        return self._media_dir

    def media_path(self, filename: str) -> str:
        return str(Path(self.media_dir) / _leaf(filename))

    def put_media(self, filename: str, data: bytes, remote: bool = True) -> str:
        """`_write_media` (backend.py:2200-2213). Returns the local path, which is what
        downstream code passes around and `_media_as_bytes` reads back."""
        path = Path(self.media_path(filename))
        path.write_bytes(data)
        if remote:
            try:
                self._ctx.storage.put_media(self._run, path.name, data)
            except Exception as exc:  # noqa: BLE001 - the source swallows this too
                # Losing the durable copy must not fail the run: the local copy is what
                # this run reads back. Same tolerance as the source, which printed a
                # warning and carried on.
                logger.warning("Could not store media %s durably: %s", path.name, exc)
        return str(path)

    def read_media(self, filename: str) -> bytes:
        return Path(self.media_path(filename)).read_bytes()


@contextlib.contextmanager
def open_run(run: Any, ctx: Any, filename: str | None = None) -> Iterator[Workspace]:
    """Materialise the run's input PDF and yield a workspace over it.

        with workspace.open_run(run, ctx) as ws:
            document = fitz.open(ws.pdf_path)

    The tree is removed on the way out, including on failure, so a worker that
    processes hundreds of large PDFs does not fill its disk.
    """
    root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    try:
        name = _leaf(filename or ctx.storage.input_filename(run) or FALLBACK_PDF_NAME)
        pdf_path = root / (name or FALLBACK_PDF_NAME)
        # Streamed rather than read whole: a 200-page standard should not be held in
        # memory twice just to get it onto disk.
        with ctx.storage.open_input(run) as source, pdf_path.open("wb") as target:
            shutil.copyfileobj(source, target)
        logger.info(
            "run %s: materialised %s (%d bytes)",
            run.id,
            name,
            pdf_path.stat().st_size,
        )

        def report(message: str) -> None:
            # No pct: the stage owns the band, this is a message plus a heartbeat.
            ctx.progress(message)

        yield Workspace(
            root=root,
            pdf_path=pdf_path,
            source_name=name,
            run=run,
            ctx=ctx,
            progress=report,
        )
    finally:
        # ignore_errors because Windows holds a handle until fitz closes the document,
        # and a leftover temp file is not worth failing a finished run.
        shutil.rmtree(root, ignore_errors=True)
