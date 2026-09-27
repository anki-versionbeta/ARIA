"""Recovers runs whose worker died.

`SKIP LOCKED` hands a queued row to exactly one worker. If that worker is killed
mid-stage, nothing else will ever look at the row again — so without a reaper the
run stays `running` forever. Checkpoints survive, so the re-queued run resumes at
the stage it was in rather than starting over.
"""

from __future__ import annotations

import logging

from api.backend.da_platform.db.session import SessionLocal
from api.backend.da_platform.engine import queue

logger = logging.getLogger(__name__)


def reap_once() -> list[str]:
    session = SessionLocal()
    try:
        return queue.requeue_stale(session)
    finally:
        session.close()
