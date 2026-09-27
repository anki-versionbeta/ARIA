"""The compliance tree in object storage.

Reviewers navigate

    ATR_MFG/Downloads/{ATR|MFGR}_<processId>_<identifier>/
        pdf/   <id>_preview.pdf    <id>_edited.pdf
        docx/  <id>_preview.docx   <id>_edited.docx

and that layout is the artefact, not an implementation detail: the `_preview` copy is the
clean report and `_edited` is what the author signed off, both retained. The same objects
serve the app being replaced and this platform during migration, so the tree is reproduced
exactly — see `location.py` and `ids.run_prefix`.

**Why this writes through the object store rather than `ctx.storage`.** Both of that
facade's write methods route through `run_key`, which calls `_leaf()` and reduces the name
to its basename, and which maps a kind onto a single flat folder. Neither can express a
nested key, so `put_media` silently turned

    ATR_MFG/Downloads/ATR_<pid>_CMC-10352/pdf/ATR_CMC-10352_preview.pdf

into

    ATR_MFG/extracts/<run_id>_ATR_CMC-10352_preview.pdf

— wrong folder, and the tree gone. That flattening is right for BOP and ISO, whose media
names are flat; it is simply not a capability `ctx` offers. The bypass is deliberate and
confined to this one function, and the key is checked to stay inside this silo's prefix so
a caller's mistake cannot write into another silo's area.

The run's own downloadable files are unaffected and still go through
`ctx.storage.attach_output`; this tree is in addition to them, exactly as in the source.
"""

from __future__ import annotations

import logging

from .location import STORAGE_PREFIX

logger = logging.getLogger(__name__)


def put(key: str, data: bytes) -> str:
    """Write `data` at exactly `key`, which must already sit under this silo's prefix.

    `ids.run_prefix` returns a fully-qualified prefix, so callers pass a complete key.
    """
    from api.backend.da_platform.storage import get_object_store

    key = key.strip("/")
    root = STORAGE_PREFIX.strip("/")
    if not key.startswith(f"{root}/"):
        raise ValueError(
            f"archive key {key!r} is outside this silo's prefix {root!r}; "
            "pass a key built from ids.run_prefix"
        )

    get_object_store().put(key, data)
    logger.debug("archived %s (%d bytes)", key, len(data))
    return key
