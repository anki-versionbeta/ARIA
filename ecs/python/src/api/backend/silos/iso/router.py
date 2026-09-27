"""ISO's own endpoints: the section-range picker.

Mounted by the platform under `/api/silos/iso`. This is the other half of the pause:
`await_range` parks the run, this endpoint records the chosen range as that stage's
checkpoint and re-queues, and the next worker to claim it starts at `build`.

`POST /generate` (backend.py:5529-5543) opened by validating two things before doing any
work — that the session existed, and that the range was sane. Both messages are kept
verbatim: output drift is a validation problem for these documents, and an error string
a user has learned to recognise is part of the product.

The session lookup is now a checkpoint lookup, which is the improvement worth having.
ISO's `sessions[session_id]` dict lived in one process's memory, so a restart — or a
second replica — produced "Session not found" for a perfectly good upload.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import Run, User
from api.backend.da_platform.engine import queue
from api.backend.da_platform.records.queries import get_document

logger = logging.getLogger(__name__)

router = APIRouter(tags=["iso"])

SILO_ID = "iso"
# Constants rather than an import from silo.py, so this module never pulls in the stage
# code. A test asserts both appear in silo.STAGES, so a rename cannot drift silently.
RANGE_STAGE = "await_range"
OUTLINE_STAGE = "outline"

# ISO's own strings (backend.py:5537, 5543). Do not reword.
SESSION_NOT_FOUND = "Session not found — please re-upload the PDF."
INVALID_RANGE = "Invalid section range selected."


class RangeIn(BaseModel):
    # Deliberately unconstrained: ISO checks the bounds itself and answers with its own
    # message, whereas a pydantic `ge=0` would turn a negative start into a 422 with a
    # different body.
    start_idx: int
    end_idx: int


class TocOut(BaseModel):
    doc_title: str
    total_pages: int
    # The entry shape is the outline stage's business, not this endpoint's.
    toc: list[Any]


def _load(session: DbSession, document_id: str) -> Run:
    run = get_document(session, document_id)
    if run is None or run.silo_id != SILO_ID:
        # A BOP run must not be steerable through ISO's endpoints.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return run


def _load_owned(session: DbSession, document_id: str, user: User) -> Run:
    """Mirrors `api.backend.da_platform.routers.documents._load_owned`: anyone may read a document
    (D13), only its owner may act on it (D14), and the 403 carries `can_fork` so the UI
    can offer "Create my own copy" instead of a dead end (D15)."""
    run = _load(session, document_id)
    if run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"can_fork": True}
        )
    return run


@router.get("/documents/{document_id}/toc", response_model=TocOut)
def get_toc(document_id: str, _user: CurrentUser, session: DbSession) -> TocOut:
    """The outline, so the picker can render.

    New surface: ISO returned the table of contents in the body of the blocking
    `/upload`, which is exactly what this platform refuses to do. Readable by any
    authenticated user, in line with every other document read.
    """
    run = _load(session, document_id)
    outline = queue.checkpoint_output(session, run.id, OUTLINE_STAGE)
    if not outline:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The outline is not ready yet",
        )
    return TocOut(
        doc_title=str(outline.get("doc_title") or ""),
        total_pages=int(outline.get("total_pages") or 0),
        toc=list(outline.get("toc") or []),
    )


@router.post("/documents/{document_id}/toc-range", status_code=status.HTTP_202_ACCEPTED)
def choose_range(
    document_id: str, payload: RangeIn, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    run = _load_owned(session, document_id, user)

    if run.status != "awaiting_user" or run.stage != RANGE_STAGE:
        # Not one of ISO's messages: ISO had no notion of a run being mid-flight,
        # because the work happened inside the request.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run is {run.status} at stage {run.stage}; "
                "it is not waiting for a section range"
            ),
        )

    outline = queue.checkpoint_output(session, run.id, OUTLINE_STAGE)
    if not outline:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SESSION_NOT_FOUND
        )

    toc = list(outline.get("toc") or [])
    # Verbatim from backend.py:5542, including the order of the three clauses.
    if (
        payload.start_idx > payload.end_idx
        or payload.start_idx < 0
        or payload.end_idx >= len(toc)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_RANGE
        )

    try:
        queue.complete_paused_stage(
            session, run, {"start_idx": payload.start_idx, "end_idx": payload.end_idx}
        )
    except ValueError as exc:
        # Lost a race with another writer; the platform's own resume maps this the same
        # way.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "%s chose ISO sections %d-%d for run %s",
        user.username,
        payload.start_idx,
        payload.end_idx,
        run.id,
    )
    return {"id": run.id, "status": run.status}
