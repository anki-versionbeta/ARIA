"""This silo's own endpoints.

Mounted by the platform under `/api/silos/mfg_atr`. Three jobs:

1. **Create a run from an identifier** (`POST /runs`). This silo has no uploaded document,
   so the platform's upload endpoint does not apply and creation lives here. It still writes
   the same single queued row that `api/uploads.py` writes, so ownership, history and the
   worker queue behave identically — only the input differs. The path is `/runs` rather than
   `/documents` because the platform's multipart upload route is
   `POST /api/silos/{silo_id}/documents`, and core routers are registered before silo
   routers, so a `/documents` here would be permanently shadowed.
2. **Serve the pickers** the UI needs before a run exists: the live request-id and
   batch-id lists, an existence check, and a warehouse reachability probe. Ported from
   `api/routers/{atr,mfgr}.py`, including their short-lived caching so opening the page
   does not re-query the warehouse every time.
3. **Accept the author's field values**, completing the `await_edits` pause.

Credentials are never read here — the warehouse arrives through the platform
(`api.backend.da_platform.warehouse`), which is what keeps `tests/test_silo_isolation.py` green.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import Run, User
from api.backend.da_platform.engine import queue
from api.backend.da_platform.records.queries import get_document
from api.backend.da_platform.warehouse import get_warehouse

from . import config, mfgr_config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mfg_atr"])

SILO_ID = "mfg_atr"
# Constants rather than importing silo.py, so this module never pulls in the pipeline. A
# test asserts both appear in silo.STAGES, so a rename cannot drift silently.
EDIT_STAGE = "await_edits"
REPORT_TYPES = ("atr", "mfgr")

# Cache windows from `api/settings.py`, ported so a page load does not re-query the
# warehouse. These are UI conveniences over reference data, not run state — run state
# lives in checkpoints.
CACHE_TTL_REQUEST_IDS = 3600
CACHE_TTL_BATCH_IDS = 3600
CACHE_TTL_EXISTS = 300

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int, producer) -> Any:
    """`api/cache.py:get_or_set`. The producer runs outside the lock so a slow warehouse
    query does not serialise every request; a brief double-compute on a cold race is fine.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


class CreateIn(BaseModel):
    report_type: str = Field(description="atr or mfgr")
    identifier: str = Field(description="a CMC request id, or a batch id for MFGR")
    title: str | None = None


class FieldValuesIn(BaseModel):
    # Every editable field is optional in the source — an author may sign off on the
    # generated values — so an empty map is a valid submission.
    field_values: dict[str, str] = {}


def _normalise(report_type: str, identifier: str) -> dict[str, str]:
    """Validate the identifier, keeping the source's own error text."""
    if report_type not in REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown report type {report_type!r}; expected one of {', '.join(REPORT_TYPES)}",
        )
    try:
        if report_type == "atr":
            return config.normalize_request_id(identifier)
        return mfgr_config.normalize_batch_id(identifier)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _load(session: DbSession, document_id: str) -> Run:
    run = get_document(session, document_id)
    if run is None or run.silo_id != SILO_ID:
        # Another silo's run must not be steerable through these endpoints.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return run


def _load_owned(session: DbSession, document_id: str, user: User) -> Run:
    """Anyone may read a document (D13); only its owner may act on it (D14). The 403
    carries `can_fork` so the UI can offer "Create my own copy" (D15)."""
    run = _load(session, document_id)
    if run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"can_fork": True}
        )
    return run


# ── creating a run from an identifier ─────────────────────────────────────────


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_document(
    payload: CreateIn, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    """Queue a report run. Writes one row and returns — it never queries the warehouse.

    The app being replaced did the whole pipeline inside this request, which is what made
    concurrent use impossible.

    **`/runs`, not `/documents`, and deliberately so.** The platform's own multipart upload
    endpoint is `POST /api/silos/{silo_id}/documents`, and FastAPI matches routes in
    registration order — core routers first, silo routers after. A `/documents` here is
    therefore shadowed by that wildcard, which answers a JSON body with 422 for a missing
    `file` field and never reaches this function.
    """
    ids = _normalise(payload.report_type, payload.identifier)
    display = ids["display"]

    run = Run(
        silo_id=SILO_ID,
        user_id=user.id,
        status="queued",
        stage=None,
        progress_pct=0,
        progress_message="Waiting for a worker",
        title=payload.title or f"{payload.report_type.upper()} {display}",
        # The stages read these. `meta` exists for exactly this — there is no input file
        # to hang them off.
        meta={
            "report_type": payload.report_type,
            "identifier": payload.identifier,
            "ids": ids,
            # The warehouse is the only data source; the app being replaced also had an
            # offline fixture mode, removed here along with its UI selector.
            "source": "live",
        },
    )
    session.add(run)
    session.commit()

    logger.info(
        "Queued %s run %s for %s", payload.report_type.upper(), run.id, display
    )
    return {"id": run.id}


# ── pickers, before a run exists ──────────────────────────────────────────────


@router.get("/status")
def warehouse_status(_user: CurrentUser) -> dict[str, Any]:
    """Can we reach the warehouse? Answers rather than failing, because the picker screen
    has to render either way — "unreachable" is information the user can act on."""
    warehouse = get_warehouse()
    reachable = warehouse.reachable()
    return {
        "warehouse_reachable": reachable,
        "results_view": warehouse.results_view if reachable else None,
    }


@router.get("/request-ids")
def request_ids(_user: CurrentUser) -> dict[str, Any]:
    """Every CMC request id that actually has results, for the ATR picker.

    Deliberately not part of the audited per-request query pack — it is a convenience
    lookup, exactly as the source notes.
    """
    ids = _cached("atr:request_ids", CACHE_TTL_REQUEST_IDS, _live_request_ids)
    return {"request_ids": ids, "reachable": bool(ids)}


def _live_request_ids() -> list[str]:
    try:
        # Imported inside the guard, not above it: an unreachable warehouse and an
        # unavailable pipeline should both give an empty picker rather than a 500.
        from . import atr_pipeline

        warehouse = get_warehouse()
        with warehouse.connect() as conn:
            raw_ids = atr_pipeline.list_request_ids(
                conn, warehouse.schema, warehouse.results_object
            )
    except Exception as exc:  # noqa: BLE001 - an empty picker beats a broken page
        logger.info("Could not list request ids: %s", exc)
        return []
    displays: dict[str, bool] = {}
    for rid in raw_ids:
        try:
            displays[config.normalize_request_id(rid)["display"]] = True
        except ValueError:
            continue
    return sorted(displays)


@router.get("/batch-ids")
def batch_ids(_user: CurrentUser, limit: int = 200) -> dict[str, Any]:
    ids = _cached(
        f"mfgr:batch_ids:{limit}", CACHE_TTL_BATCH_IDS, lambda: _live_batch_ids(limit)
    )
    return {"batch_ids": ids, "reachable": bool(ids)}


def _live_batch_ids(limit: int) -> list[str]:
    try:
        # Imported inside the guard, not above it: an unreachable warehouse and an
        # unavailable pipeline should both give an empty picker rather than a 500.
        from . import mfgr_pipeline

        warehouse = get_warehouse()
        with warehouse.connect() as conn:
            return mfgr_pipeline.list_batch_ids(conn, warehouse.schema, limit)
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not list batch ids: %s", exc)
        return []


@router.get("/exists")
def exists(id: str = Query(...), _user: CurrentUser = None) -> dict[str, Any]:  # noqa: B008
    """Does this batch have data? The source checks separately so the user finds out
    before waiting for a full generation."""
    try:
        norm = mfgr_config.normalize_batch_id(id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    def probe() -> bool:
        try:
            from . import mfgr_pipeline

            warehouse = get_warehouse()
            with warehouse.connect() as conn:
                return mfgr_pipeline.batch_exists(conn, norm, warehouse.schema)
        except Exception as exc:  # noqa: BLE001
            logger.info("Existence check failed for %s: %s", norm["short"], exc)
            return False

    return {
        "id": norm["display"],
        "exists": _cached(f"mfgr:exists:{norm['short']}", CACHE_TTL_EXISTS, probe),
    }


# ── the edit pause ────────────────────────────────────────────────────────────


@router.get("/documents/{document_id}/review")
def review(document_id: str, _user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """What the edit screen needs while the run is parked.

    The review flags live in the `generate` checkpoint rather than on the run, because they
    describe that stage's output. Readable by any authenticated user (D13) — acting on the
    document is what ownership gates, and that is `field-values` below.

    `preview_file_id` names the *clean* PDF: it carries the AcroForm the editor binds to.
    The `_edited` copy is excluded because finalize locks it, so its fields no longer
    round-trip.
    """
    run = _load(session, document_id)
    generated = queue.checkpoint_output(session, run.id, "generate") or {}
    meta = run.meta or {}

    preview = next(
        (
            item
            for item in run.files
            if item.kind == "output"
            and item.filename.endswith(".pdf")
            and "_edited" not in item.filename
        ),
        None,
    )
    return {
        "report_type": meta.get("report_type"),
        "display": (meta.get("ids") or {}).get("display"),
        "generated": bool(generated.get("generated")),
        "flags": generated.get("flags") or [],
        "preview_file_id": preview.id if preview else None,
    }


# ── completing the edit pause ─────────────────────────────────────────────────


@router.post("/documents/{document_id}/field-values", status_code=status.HTTP_202_ACCEPTED)
def submit_field_values(
    document_id: str, payload: FieldValuesIn, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    """Record the author's values and resume the run at `finalize`.

    The other half of the pause. This replaces the process-id lookup into an in-memory
    cache — and with it the "this report run has expired" dead end.
    """
    run = _load_owned(session, document_id, user)

    if run.status != "awaiting_user" or run.stage != EDIT_STAGE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run is {run.status} at stage {run.stage}; "
                "it is not waiting for your values"
            ),
        )

    try:
        queue.complete_paused_stage(
            session, run, {"field_values": dict(payload.field_values)}
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "%s submitted %d field value(s) for run %s",
        user.username,
        len(payload.field_values),
        run.id,
    )
    return {"id": run.id, "status": run.status}
