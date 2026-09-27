"""The only dialect-specific module (D26).

Postgres uses `FOR UPDATE SKIP LOCKED`; SQLite has no such construct and instead
relies on `BEGIN IMMEDIATE` plus a single `UPDATE ... RETURNING`. Confining the
divergence here keeps the eventual Postgres switch a config change.

Phase 1 has no silos, so nothing calls this yet. It exists so the run engine has
a defined seam to land against in phase 3.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


def claim_next_run(session: Session, worker_id: str, now: datetime) -> str | None:
    """Atomically claim one queued run, returning its id."""
    if session.bind is None:
        raise RuntimeError("Session is not bound to an engine")

    if session.bind.dialect.name == "postgresql":
        selected = session.execute(
            text(
                """
                SELECT id FROM runs
                WHERE status = 'queued'
                ORDER BY started_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
        ).scalar()
        if selected is None:
            return None
        session.execute(
            text(
                """
                UPDATE runs
                SET status = 'running', claimed_by = :worker, claimed_at = :now,
                    heartbeat_at = :now
                WHERE id = :id
                """
            ),
            {"worker": worker_id, "now": now, "id": selected},
        )
        return str(selected)

    claimed = session.execute(
        text(
            """
            UPDATE runs
            SET status = 'running', claimed_by = :worker, claimed_at = :now,
                heartbeat_at = :now
            WHERE id = (
                SELECT id FROM runs WHERE status = 'queued'
                ORDER BY started_at LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id, "now": now},
    ).scalar()
    return str(claimed) if claimed is not None else None
