"""The Smartsheet snapshot: capture once, rebuild deterministically.

This is the answer to PSA's oldest architectural problem. `build_db.main()` deletes and recreates the
whole database, and every recommend/report/refresh used to do exactly that against the live sheet — so
two requests raced over one file, a global lock was needed to stop them, and two runs of "the same"
report could legitimately differ because the sheet moved underneath them.

Instead, following `da-backend/silos/mfg_atr/silo.py`'s stated reasoning — *"`fetch` stores the raw query
snapshot and `finalize` rebuilds the report from it. Both builders are pure functions of
`(identifier, raw)`, so the rebuild is deterministic, the expiry failure disappears, and the audit
snapshot and the rebuild read the same bytes"* — a run captures the sheet ONCE and every later stage
rebuilds from those bytes:

    fetch      capture_bytes()            one REST read  ->  ctx.storage.put_media(run, SNAPSHOT, raw)
    recommend  rebuild(raw)               no network     ->  ranked colours
    report     rebuild(raw)               no network     ->  the .docx

Two consequences worth stating. The recommendation and the report of one run are guaranteed to describe
the same catalogue, which they previously were not. And the snapshot is the audit record of what the
sheet said when the assessment was made — which is what a GxP reviewer would ask for.

**Product images are not in THIS file's snapshot, but they are captured.** They are separate
authenticated downloads, so `rebuild()` is image-free and a report rebuilt from the JSON alone has a blank
photo cell (`verify.py` accepts that). The `fetch` stage therefore captures the subject presentation's
photo alongside this snapshot, as its own run-media object (`silo.py`, `PHOTO_MEDIA_PREFIX`), and `report`
embeds those bytes directly — so the photo has the same capture-once guarantee as the sheet without
passing through the database. The credential that makes both reads possible arrives from
`da_platform.credentials.smartsheet_credentials()`; that question is settled, not open.
"""
from __future__ import annotations

import json

from . import build_db, ingest_smartsheet_api as ingest_api

SNAPSHOT_MEDIA = "smartsheet.json"
"""Name of the stored snapshot, under the run's media folder."""


def capture_bytes(sheet_id=None) -> bytes:
    """One live read, canonicalised to bytes.

    `sort_keys=True` so the same sheet content always produces the same bytes: it makes the snapshot
    comparable between runs, which is what turns "did the catalogue change?" into a byte comparison.
    """
    sheet, sid = ingest_api.capture(sheet_id)
    return json.dumps({"sheet_id": sid, "sheet": sheet},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def rebuild(raw: bytes, skip_images: bool = True) -> dict:
    """Recreate psa.db from a captured snapshot. No network when `skip_images` (the default).

    Returns the row counts, which are what a stage puts in its checkpoint so a later stage — or a person
    reading the run — can see what the snapshot actually contained.
    """
    doc = json.loads(raw.decode("utf-8"))
    build_db.main()
    return ingest_api.load(doc["sheet"], doc["sheet_id"], skip_images=skip_images)


def describe(raw: bytes) -> dict:
    """Cheap facts about a snapshot, without rebuilding anything.

    Used for the fetch stage's checkpoint and for the run's progress line, so a reviewer can see the
    shape of what was captured without opening the database.
    """
    doc = json.loads(raw.decode("utf-8"))
    sheet = doc.get("sheet") or {}
    return {
        "sheet_id": doc.get("sheet_id"),
        "sheet_name": sheet.get("name"),
        "columns": len(sheet.get("columns") or []),
        "rows": len(sheet.get("rows") or []),
        "bytes": len(raw),
    }
