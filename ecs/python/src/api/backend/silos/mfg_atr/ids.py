"""Process-id minting for a report run. Ported verbatim from `api/ids.py`.

A process id ties one ATR/MFGR run together across preview → edit → download, and names
its archival folder. Format `<yyyymmddHHMMSS>-<6hex>` — sortable and unique.

Kept even though the platform already gives every run a uuid, because the process id is
what appears in the archival folder name that compliance reviewers navigate. Changing it
would orphan the existing tree.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from .location import STORAGE_PREFIX


def new_process_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def safe_segment(value: str) -> str:
    """Sanitize a value for use inside an S3 key segment (no spaces/slashes)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    return re.sub(r"-+", "-", s).strip("-") or "unknown"


def run_prefix(kind: str, process_id: str, identifier: str) -> str:
    """e.g. 'ATR_MFG/Downloads/ATR_<pid>_CMC-10352'.

    `api/s3.py:run_prefix` produced `<BASE_PREFIX>/<folder>`; the platform supplies the
    bucket and the storage prefix, so only the tail is built here.
    """
    folder = f"{kind.upper()}_{safe_segment(process_id)}_{safe_segment(identifier)}"
    return f"{STORAGE_PREFIX}/Downloads/{folder}"
