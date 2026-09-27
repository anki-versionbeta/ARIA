"""Run state transitions.

Every status change lives here so the state machine is readable in one place:

    queued -> running -> awaiting_user -> queued -> running -> complete
                     \\-> failed (from any running state)

Claiming is the one dialect-specific operation and is delegated to
`da_platform.db.dialect.claim_next_run`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from da_platform.db.dialect import claim_next_run
from da_platform.db.models import Run, RunStageCheckpoint, utcnow
from da_platform.settings import settings

logger = logging.getLogger(__name__)


def claim(session: Session, worker_id: str) -> Run | None:
    run_id = claim_next_run(session, worker_id, utcnow())
    session.commit()
    if run_id is None:
        return None
    run = session.get(Run, run_id)
    if run is not None:
        # The claim is raw SQL and the session is configured with
        # expire_on_commit=False, so an instance already in the identity map would
        # otherwise still report status="queued" after being claimed.
        session.refresh(run)
    return run


def heartbeat(session: Session, run: Run) -> None:
    """Prove the worker is still alive so the reaper leaves this run alone."""
    run.heartbeat_at = utcnow()
    session.commit()


def park_for_user(session: Session, run: Run, message: str | None = None) -> None:
    """Persist a pause and release the worker.

    Deliberately writes **no checkpoint** for the paused stage: the user's answer is
    what completes it. Whatever endpoint accepts that answer records the checkpoint
    and re-queues, so the runner then skips the stage and continues at the next one.
    """
    run.status = "awaiting_user"
    run.progress_message = message or run.progress_message
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    session.commit()


def resume(session: Session, run: Run) -> None:
    """Move an awaiting_user or failed run back into the queue."""
    run.status = "queued"
    run.error_message = None
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    session.commit()


def finish(session: Session, run: Run) -> None:
    run.status = "complete"
    run.stage = None
    run.progress_pct = 100
    run.finished_at = utcnow()
    run.duration_ms = _elapsed_ms(run)
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    session.commit()


def mark_failed(session: Session, run: Run, message: str, traceback_text: str = "") -> None:
    """Fail the run but keep its checkpoints, so a retry resumes from the last good
    stage rather than re-spending on work already done."""
    run.status = "failed"
    run.error_message = message
    run.finished_at = utcnow()
    run.duration_ms = _elapsed_ms(run)
    run.claimed_by = None
    run.claimed_at = None
    run.heartbeat_at = None
    if traceback_text:
        # Stored, not returned to non-admins (spec section 10).
        metadata = dict(run.meta or {})
        metadata["traceback"] = traceback_text
        run.meta = metadata
    session.commit()


def save_checkpoint(session: Session, run_id: str, stage: str, output: object) -> None:
    existing = session.get(RunStageCheckpoint, {"run_id": run_id, "stage": stage})
    payload = output if isinstance(output, dict) or output is None else {"value": output}
    if existing is None:
        session.add(
            RunStageCheckpoint(run_id=run_id, stage=stage, output=payload)
        )
    else:
        existing.output = payload
        existing.completed_at = utcnow()
    session.commit()


def complete_paused_stage(
    session: Session, run: Run, payload: dict | None = None
) -> None:
    """Record a user's answer as the paused stage's checkpoint and re-queue.

    This is the other half of `park_for_user`. Writing the checkpoint here — rather
    than when the stage returned — is what makes the runner skip the stage on resume
    and start at the next one, with the user's input readable via
    `ctx.checkpoint(<stage>)`.

    Silo-specific endpoints (ISO's /toc-range) validate their own payload shape and
    then delegate here, so the state transition exists once.
    """
    if run.status != "awaiting_user":
        raise ValueError(f"Run {run.id} is {run.status}, not awaiting_user")
    if not run.stage:
        raise ValueError(f"Run {run.id} is parked with no stage recorded")
    save_checkpoint(session, run.id, run.stage, payload or {})
    resume(session, run)


def completed_stages(session: Session, run_id: str) -> set[str]:
    rows = session.scalars(
        select(RunStageCheckpoint.stage).where(RunStageCheckpoint.run_id == run_id)
    ).all()
    return set(rows)


def checkpoint_output(session: Session, run_id: str, stage: str) -> dict | None:
    row = session.get(RunStageCheckpoint, {"run_id": run_id, "stage": stage})
    return row.output if row else None


def requeue_stale(session: Session) -> list[str]:
    """Re-queue runs whose worker stopped heartbeating.

    Without this, a killed worker's runs stay `running` forever: SKIP LOCKED hands
    the row to one worker and nothing else will ever look at it again.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.stale_claim_timeout_s
    )
    stale = session.scalars(
        select(Run).where(Run.status == "running", Run.heartbeat_at < cutoff)
    ).all()
    for run in stale:
        logger.warning(
            "Re-queueing run %s: worker %s stopped heartbeating",
            run.id,
            run.claimed_by,
        )
        run.status = "queued"
        run.claimed_by = None
        run.claimed_at = None
        run.heartbeat_at = None
    session.commit()
    return [run.id for run in stale]


def _elapsed_ms(run: Run) -> int | None:
    if run.started_at is None:
        return None
    started = run.started_at
    if started.tzinfo is None:
        # SQLite returns naive datetimes even for timezone-aware columns.
        started = started.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
