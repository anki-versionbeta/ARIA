"""Run creation — the one thing the platform does not already do for PSA.

**This module only works inside ARIA.** It imports `Run` and `DbSession`, so `router.py` includes it
behind `try/except ImportError` and the standalone harness simply has no run route. That makes it the one
file in the silo that cannot be byte-identical in both copies — everything else is, and
`tests/test_port_sync.py` enforces it.

**Why there is so little here.** A PSA job is `fetch -> report`, straight through, so the platform covers
everything except starting it (verified in `docs/PSA_As_An_ARIA_Run.md`, with file:line citations):

    GET  /api/documents/{id}/status          progress, polled every 2 s by the browser
    GET  /api/documents/{id}                 the run and its files
    GET  /api/documents/{id}/files/{file_id} download a produced file, readable by anyone
    POST /api/documents/{id}/retry           re-queue a failed run from its last checkpoint

There is no upload endpoint to piggyback on, because PSA starts from an identifier rather than a file —
hence this one route.

**What used to be here, and why it is gone.** Until 2026-08-22 this module also served
`GET /runs/{id}/recommendation` and `POST /runs/{id}/cap-choice`, which completed a `confirm` stage that
parked the run so an assessor could confirm a cap colour. That whole flow was removed at the user's
request: a job is the REPORT, and the cap-colour recommendation is the screen's instant, unrecorded
preview. With nothing pausing, both endpoints had nothing to read or resume. `git log` has them.

That also retired the one ownership exception PSA had. Nothing here acts on an existing run, so there is
no owner-only action left and the standing "anyone acts" rule needs no carve-out.

`/runs` and NOT `/documents`: the platform registers `POST /api/silos/{silo_id}/documents` first and
FastAPI matches in registration order, so a `/documents` here would be permanently shadowed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import Run
from api.backend.da_platform.engine import queue
from api.backend.da_platform.records.queries import get_document

logger = logging.getLogger(__name__)

router = APIRouter(tags=["psa"])

SILO_ID = "psa"
REPORT_STAGE = "report"
# Named here rather than imported from silo.py, so this module never drags in the pipeline —
# `mfg_atr/router.py` does the same. `tests/test_silo_isolation.py` asserts every `*_STAGE` constant in
# this file names a real stage, so a rename cannot drift away from it silently.


class CreateRunIn(BaseModel):
    program: str = Field(description="Program code, e.g. 'AGN-151586'.")
    source_row: int | str = Field(
        description="The Smartsheet row. Stable across database rebuilds, unlike product_id.")
    vendor: str | None = Field(
        default=None,
        description=("Optional restriction to one supplier. Omit it — the recommendation then spans every "
                     "supplier, which is the default because at assessment time the cap is often not yet "
                     "tooled. Recorded on the run for the audit trail even though the report itself does "
                     "not rank colours."))
    title: str | None = Field(
        default=None,
        description=("What the document list shows. The SCREEN supplies this, because only it knows the "
                     "readable labels — this endpoint never reads Smartsheet, so it cannot turn row 20 "
                     "into '2R (2.00 mL) vial'."))


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(payload: CreateRunIn, user: CurrentUser, session: DbSession) -> dict[str, str]:
    """Queue a PSA report. Writes ONE row and returns; it never reads Smartsheet.

    Reading the sheet is the `fetch` stage's job, in the worker. Doing it here is what made the old
    synchronous design impossible to use concurrently, and it would also make starting a job fail for
    reasons that have nothing to do with starting a job.

    The stage inputs go on `run.meta` because there is no input file to hang them off — `mfg_atr` does the
    same, for the same reason.
    """
    program = payload.program.strip()
    if not program:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A program code is required.")

    run = Run(
        silo_id=SILO_ID,
        user_id=user.id,
        status="queued",
        stage=None,
        progress_pct=0,
        progress_message="Waiting for a worker",
        # The row number is deliberately absent from the fallback: it is our internal key for a Smartsheet
        # row and means nothing to whoever reads the document list.
        title=payload.title or f"PSA {program}",
        meta={
            "program": program,
            "source_row": payload.source_row,
            "vendor": payload.vendor,
        },
    )
    session.add(run)
    session.commit()

    logger.info("Queued PSA run %s for %s row %s", run.id, program, payload.source_row)
    return {"id": run.id}


def _load(session: DbSession, run_id: str) -> Run:
    run = get_document(session, run_id)
    if run is None or run.silo_id != SILO_ID:
        # Another silo's run must not be readable through PSA's endpoints.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return run


@router.get("/runs/{run_id}/result")
def run_result(run_id: str, _user: CurrentUser, session: DbSession) -> dict:
    """What the `report` stage produced, so the screen can show the same detail it always has.

    Readable by ANY authenticated user: this is a read, and PSA gates no reads. Nothing here acts on the
    run, so there is no owner-only action to enforce.

    The screen used to get this from the synchronous `POST /report`, which returned the engine's whole
    result dict — verify status, the provisional mix-up risk, the comparator tables, the run log. Now that
    a worker produces the report, that dict lives in the stage's checkpoint, and without this endpoint the
    screen would have lost all of it and shown only "done". Same shape as before, so one rendering path
    serves both the run and the standalone fallback.

    `output_key` is stripped: it is a storage key, and the client has no business addressing storage
    directly. `files` carries the platform's own file ids instead, which is what the download endpoint
    takes — the same reasoning that replaced `output_path` with an opaque id on the synchronous route.
    """
    run = _load(session, run_id)
    checkpoint = dict(queue.checkpoint_output(session, run.id, REPORT_STAGE) or {})
    checkpoint.pop("output_key", None)

    return {
        "run_id": run.id,
        "run_status": run.status,
        "stage": run.stage,
        **checkpoint,
        "files": [
            {"id": item.id, "filename": item.filename, "size_bytes": item.size_bytes}
            for item in run.files
            if item.kind == "output"
        ],
    }
