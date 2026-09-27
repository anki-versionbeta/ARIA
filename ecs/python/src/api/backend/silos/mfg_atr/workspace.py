"""A scratch directory for one stage, because the builders write to filesystem paths.

`generate_atr_pdf`, `generate_atr_docx` and `finalize_pdf` all take an `out_path` and
return a `Path`. They were ported verbatim — rewriting them to return bytes would touch
rendering code, which is exactly what the porting rule forbids — so the stage gives them
somewhere real to write and reads the bytes back afterwards.

Nothing durable lives here. Outputs go to object storage via `ctx.storage.attach_output`;
this tree is deleted on the way out, including on failure, so a worker that renders
hundreds of reports does not fill its disk.

Unlike the ISO silo's workspace there is no input document to materialise — this silo's
input is an identifier, and its data comes from the warehouse.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

SCRATCH_PREFIX = "mfg_atr_"


class Workspace:
    """Somewhere to render. Valid only inside its `scratch()` block."""

    __slots__ = ("root",)

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, filename: str) -> str:
        """An absolute path inside the scratch tree, as a string — the builders join and
        `Path()` these directly."""
        leaf = filename.replace("\\", "/").split("/")[-1]
        return str(self.root / leaf)

    def read(self, filename: str) -> bytes:
        return (self.root / filename.replace("\\", "/").split("/")[-1]).read_bytes()


@contextlib.contextmanager
def scratch() -> Iterator[Workspace]:
    """Yield a scratch workspace and remove it afterwards.

        with workspace.scratch() as ws:
            generate_atr_pdf(report, ws.path("ATR_CMC-10352.pdf"))
            pdf_bytes = ws.read("ATR_CMC-10352.pdf")
    """
    root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    try:
        yield Workspace(root)
    finally:
        # ignore_errors because Windows can hold a handle briefly after a writer closes,
        # and a leftover temp file is not worth failing a finished report.
        shutil.rmtree(root, ignore_errors=True)
