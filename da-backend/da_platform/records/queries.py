"""History queries.

Filtering, sorting and pagination happen in the database rather than the client
because the history table is the one screen guaranteed to grow without bound.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Select, asc, desc, func, select
from sqlalchemy.orm import Session

from da_platform.db.models import Run, User

PAGE_SIZE = 25

SORT_COLUMNS = {
    "date": Run.started_at,
    "name": Run.title,
    "user": User.display_name,
}


def _apply_filters(
    stmt: Select,
    *,
    user: str | None,
    silo: str | None,
    status: str | None,
    q: str | None,
    silos: Collection[str] | None = None,
) -> Select:
    if user:
        stmt = stmt.where(User.username == user.strip().lower())
    if silo:
        stmt = stmt.where(Run.silo_id == silo)
    if silos is not None:
        # Module access, not a user-chosen filter. Tested with `is not None` rather than
        # truthiness on purpose: a user granted nothing must see nothing, and `if silos:`
        # would fall through on the empty set and show them everything.
        stmt = stmt.where(Run.silo_id.in_(silos))
    if status:
        stmt = stmt.where(Run.status == status)
    if q:
        stmt = stmt.where(Run.title.ilike(f"%{q.strip()}%"))
    return stmt


def list_documents(
    session: Session,
    *,
    user: str | None = None,
    silo: str | None = None,
    status: str | None = None,
    q: str | None = None,
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    silos: Collection[str] | None = None,
) -> tuple[list[Run], int]:
    """`silos` restricts the result to the caller's granted modules.

    Distinct from `silo`, which is the filter the user picked in the UI: `silo` narrows
    what they asked for, `silos` bounds what they are allowed to see at all.
    """
    sort_column = SORT_COLUMNS.get(sort, Run.started_at)
    direction = asc if order == "asc" else desc

    rows_stmt = _apply_filters(
        select(Run).join(User, Run.user_id == User.id),
        user=user,
        silo=silo,
        status=status,
        q=q,
        silos=silos,
    )
    count_stmt = _apply_filters(
        select(func.count(Run.id)).join(User, Run.user_id == User.id),
        user=user,
        silo=silo,
        status=status,
        q=q,
        silos=silos,
    )

    total = session.scalar(count_stmt) or 0
    page = max(page, 1)
    rows = session.scalars(
        rows_stmt.order_by(direction(sort_column), desc(Run.started_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def get_document(session: Session, run_id: str) -> Run | None:
    return session.get(Run, run_id)
