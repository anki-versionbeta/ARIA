"""Worker entrypoint: the process that actually does the slow work.

    python -m worker

The API never runs a stage. It writes a queued row and returns in milliseconds;
this process picks the row up. That split is what makes 15 concurrent generations
possible and what stops a deploy from destroying in-flight work.

Scale by running more processes (`docker compose up --scale worker=N`) rather than
by threading inside one: the heavy work is PyMuPDF rendering and docx assembly,
which contend on the GIL.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from types import FrameType

from api.backend.da_platform.db.session import SessionLocal, create_all
from api.backend.da_platform.engine import runner
from api.backend.da_platform.engine import queue
from api.backend.da_platform.engine.reaper import reap_once
from api.backend.da_platform.settings import settings

logger = logging.getLogger("api.backend.worker")

IDLE_SLEEP_SECONDS = 2.0
REAP_EVERY_SECONDS = 60.0

_shutdown_requested = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    """Finish the current stage, then stop.

    Killing mid-stage would waste the LLM spend already incurred and force the
    reaper to re-queue the run. The deployment's grace period must therefore exceed
    the longest stage (spec section 11.1, item 1) — otherwise every deploy hard-kills
    in-flight work.
    """
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Signal %s received; finishing the current stage then exiting", signum)


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    if settings.is_local:
        create_all()

    identity = worker_id()
    logger.info(
        "Worker %s started (storage=%s, silos=%s)",
        identity,
        settings.storage_backend,
        settings.silos_dir,
    )

    last_reap = 0.0
    while not _shutdown_requested:
        now = time.monotonic()
        if now - last_reap > REAP_EVERY_SECONDS:
            try:
                requeued = reap_once()
                if requeued:
                    logger.warning("Reaper re-queued %d run(s)", len(requeued))
            except Exception:
                logger.exception("Reaper failed")
            last_reap = now

        session = SessionLocal()
        try:
            run = queue.claim(session, identity)
            if run is None:
                session.close()
                time.sleep(IDLE_SLEEP_SECONDS)
                continue

            logger.info("Claimed run %s (silo=%s)", run.id, run.silo_id)
            status = runner.run_one(session, run)
            logger.info("Run %s ended as %s", run.id, status)
        except Exception:
            logger.exception("Unhandled error in the worker loop")
            time.sleep(IDLE_SLEEP_SECONDS)
        finally:
            session.close()

    logger.info("Worker %s exiting cleanly", identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
