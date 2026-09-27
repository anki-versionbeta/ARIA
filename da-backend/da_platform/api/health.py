from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from da_platform.auth.deps import DbSession

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(session: DbSession) -> dict[str, str]:
    """Unauthenticated, and checks the database — a process that cannot reach its
    database is not healthy even though it can serve a response."""
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc
    return {"status": "ok", "database": "ok"}
