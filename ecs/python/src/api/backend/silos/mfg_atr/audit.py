"""Audit trail for report generation (GxP). Ported from `src/audit.py` and `src/mfgr_audit.py`.

Every generation is recorded with enough detail to reproduce it and to support ALCOA+
data-integrity expectations: who / when, the identifier, the exact SQL and bind values, row
counts, a hash of the source-data snapshot, review flags, and the output path.

**The record's field names are part of the compliance artefact.** Do not rename, reorder or
"tidy" them. `build_record` is deliberately separate from writing, so a test can pin the
shape without touching storage.

Two differences from the source, both forced and both worth knowing:

* **One immutable object per run instead of an appended JSONL file.** The source appends a
  line to `output/audit/atr_audit.jsonl`. Object stores cannot append, and several workers
  can finish at once, so an append-in-place emulation would be a race with a compliance
  record as the prize. Each run writes its own record and its own full snapshot instead;
  the union of those objects is the same trail.
* **`os_user` is the worker's service account**, not the author's machine. The authenticated
  author is already carried separately in `app_user`, which is the field a reviewer wants —
  but the meaning of `os_user` has changed and that is recorded here rather than discovered.

ATR and MFGR name things differently and the caller supplies both names, because the
difference is visible in the artefact:

| | record key | bind name |
|---|---|---|
| ATR | `request_id` | `request_id` |
| MFGR | `batch_id` | `batch_root` |

The bind name is *not* derivable from the record key — MFGR's differ — and getting it wrong
would mean the record names a bind the query never used.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import platform
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Media names for the two objects written per run.
AUDIT_RECORD_MEDIA = "audit.json"
AUDIT_SNAPSHOT_MEDIA = "audit.snapshot.json"


def now_iso() -> str:
    """UTC timestamp for the generation (also stamped onto the PDF audit page)."""
    return datetime.now(timezone.utc).isoformat()


def _data_hash(raw: dict[str, Any]) -> str:
    payload = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return str(obj)


def build_record(
    *,
    ids: dict[str, str],
    id_field: str,
    bind_field: str,
    queries: dict[str, str],
    raw: dict[str, list[dict[str, Any]]],
    flags: list[Any],
    output_path: str,
    generated_at: str,
    finalized: bool = False,
    app_user: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The audit record for one generation.

    `app_user` is the authenticated user who ran the generation; `os_user` remains the
    account the worker process runs as.
    """
    return {
        "timestamp_utc": generated_at,
        "os_user": getpass.getuser(),
        "app_user": app_user,
        "host": platform.node(),
        id_field: ids,
        "queries": queries,
        "bind_values": {bind_field: ids.get("query")},
        "row_counts": {k: len(v or []) for k, v in raw.items()},
        "source_data_sha256": _data_hash(raw),
        "review_flags": [_jsonable(f) for f in flags],
        "output_path": output_path,
        "finalized": finalized,
    }


def record_generation(
    run,
    ctx,
    *,
    ids: dict[str, str],
    id_field: str,
    bind_field: str,
    queries: dict[str, str],
    raw: dict[str, list[dict[str, Any]]],
    flags: list[Any],
    output_path: str,
    generated_at: str,
    finalized: bool = False,
    app_user: dict[str, str] | None = None,
) -> str:
    """Write the record and the full source-data snapshot. Returns the record's key.

    Both are stored as run media, so they live beside the report they describe and survive
    the worker being replaced.
    """
    record = build_record(
        ids=ids,
        id_field=id_field,
        bind_field=bind_field,
        queries=queries,
        raw=raw,
        flags=flags,
        output_path=output_path,
        generated_at=generated_at,
        finalized=finalized,
        app_user=app_user,
    )

    key = ctx.storage.put_media(
        run, AUDIT_RECORD_MEDIA, json.dumps(record, indent=2, default=str).encode("utf-8")
    )
    # The snapshot carries the source data itself, which is what lets a reviewer prove the
    # report came from it — `source_data_sha256` above is the hash of exactly this.
    snapshot = dict(record, source_data=raw)
    ctx.storage.put_media(
        run,
        AUDIT_SNAPSHOT_MEDIA,
        json.dumps(snapshot, indent=2, default=str).encode("utf-8"),
    )

    logger.info(
        "run %s: audit recorded for %s (sha256=%s)",
        run.id,
        ids.get("display") or ids.get("short"),
        record["source_data_sha256"][:12],
    )
    return key
