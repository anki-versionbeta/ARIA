"""The pipeline. One loop, every silo.

The silo supplies the steps; the platform owns the sequencing, checkpointing,
progress, pausing and failure handling. A silo that does not care about resume
points may declare a single stage.
"""

from __future__ import annotations

import logging
import traceback

from sqlalchemy.orm import Session

from da_platform.db.models import Run
from da_platform.engine import queue
from da_platform.engine.context import Pause, StageContext
from da_platform.silo_registry import Silo, get_silo

logger = logging.getLogger(__name__)


class StageFailed(RuntimeError):
    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"Stage {stage!r} failed: {cause}")
        self.stage = stage
        self.cause = cause


def run_one(session: Session, run: Run, silo: Silo | None = None) -> str:
    """Advance a claimed run as far as it will go.

    Returns the status it ended on: `complete`, `awaiting_user` or `failed`.
    """
    resolved = silo or get_silo(run.silo_id)
    if resolved is None:
        # A run whose silo was disabled or removed. Failing loudly beats leaving it
        # queued forever with no explanation.
        message = f"No silo registered for {run.silo_id!r}"
        logger.error("run %s: %s", run.id, message)
        queue.mark_failed(session, run, message)
        return "failed"

    done = queue.completed_stages(session, run.id)
    context = StageContext(
        session,
        run,
        storage_prefix=resolved.storage_prefix,
        storage_folders=resolved.storage_folders,
    )

    for stage in resolved.stages:
        if stage in done:
            # The heart of resume: a completed stage is never repeated, so a crash or
            # a deploy costs at most the stage that was in flight.
            logger.info("run %s: skipping completed stage %s", run.id, stage)
            continue

        run.stage = stage
        queue.heartbeat(session, run)
        logger.info("run %s: entering stage %s", run.id, stage)

        try:
            output = resolved.stage_callable(stage)(run, context)
        except Exception as exc:
            logger.exception("run %s: stage %s raised", run.id, stage)
            queue.mark_failed(
                session, run, f"{stage}: {exc}", traceback.format_exc()
            )
            return "failed"

        if isinstance(output, Pause):
            logger.info("run %s: parked at %s for user action", run.id, stage)
            queue.park_for_user(session, run, output.message)
            return "awaiting_user"

        queue.save_checkpoint(session, run.id, stage, output)
        queue.heartbeat(session, run)

    queue.finish(session, run)
    logger.info("run %s: complete", run.id)
    return "complete"
