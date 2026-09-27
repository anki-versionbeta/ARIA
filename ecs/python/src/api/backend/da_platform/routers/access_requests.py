"""Asking for module access.

The requester half. Approving lives in `api/admin.py`, next to the code that actually
changes access, so the grant and the decision that authorised it are written together.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.backend.da_platform.routers.schemas import AccessRequestIn, AccessRequestOut, ModuleOut
from api.backend.da_platform.auth import audit
from api.backend.da_platform.auth.access import (
    accessible_module_ids,
    known_module_ids,
    sees_every_module,
)
from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import AccessRequest, User
from api.backend.da_platform import notify
from api.backend.da_platform.silo_registry import discover_silos

router = APIRouter(tags=["access-requests"])


def as_out(request: AccessRequest, requester: User) -> AccessRequestOut:
    return AccessRequestOut(
        id=request.id,
        user_id=requester.id,
        username=requester.username,
        display_name=requester.display_name,
        email=requester.email,
        modules=list(request.modules or []),
        note=request.note,
        status=request.status,
        created_at=request.created_at,
        decided_at=request.decided_at,
        decided_by=request.decided_by,
        decision_note=request.decision_note,
    )


def _pending_for(session: DbSession, user_id: str) -> AccessRequest | None:
    return session.scalars(
        select(AccessRequest).where(
            AccessRequest.user_id == user_id, AccessRequest.status == "pending"
        )
    ).first()


@router.get("/modules", response_model=list[ModuleOut])
def list_modules(_user: CurrentUser) -> list[ModuleOut]:
    """The modules that exist, for any signed-in user.

    Not admin-only, and it cannot be: the request form is shown to somebody with no access
    at all, and a form that cannot list its options is not a form. /api/silos stays
    filtered -- that answers "what may I use", which is a different question from "what
    could I ask for". The catalogue is three product names, not a secret.
    """
    return [ModuleOut(id=silo.id, label=silo.label) for silo in discover_silos()]


@router.get("/access-requests/mine", response_model=AccessRequestOut | None)
def my_request(user: CurrentUser, session: DbSession) -> AccessRequestOut | None:
    """The caller's open request, or null.

    Null rather than 404: "you have not asked for anything" is an ordinary state for this
    screen, not an error, and a 404 would make the frontend treat it as one.
    """
    pending = _pending_for(session, user.id)
    return as_out(pending, user) if pending else None


@router.post(
    "/access-requests", response_model=AccessRequestOut, status_code=status.HTTP_201_CREATED
)
def request_access(
    payload: AccessRequestIn, user: CurrentUser, session: DbSession
) -> AccessRequestOut:
    """Ask for modules. Re-asking updates the open request rather than adding another."""
    wanted = sorted({module.strip() for module in payload.modules if module.strip()})
    if not wanted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose at least one module to request.",
        )

    unknown = sorted(set(wanted) - known_module_ids())
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown module(s): {', '.join(unknown)}",
        )

    # Asking for what you already hold is a sign the screen is out of date rather than a
    # thing to queue for an admin, so say so instead of filing it.
    if sees_every_module(user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "already_granted",
                "message": "Your role already gives you every module.",
            },
        )
    already = accessible_module_ids(session, user) & set(wanted)
    if already == set(wanted):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "already_granted",
                "message": "You already have access to everything you asked for. Reload the page.",
            },
        )

    note = (payload.note or "").strip() or None
    pending = _pending_for(session, user.id)
    if pending is not None:
        pending.modules = wanted
        pending.note = note
        request = pending
    else:
        request = AccessRequest(user_id=user.id, modules=wanted, note=note)
        session.add(request)

    try:
        session.commit()
    except IntegrityError:
        # The partial unique index caught a second tab submitting at the same time. The
        # other one won and the outcome is what the user wanted either way.
        session.rollback()
        existing = _pending_for(session, user.id)
        if existing is None:
            raise
        return as_out(existing, user)

    session.refresh(request)
    audit.record("access_requested", user.username, detail=", ".join(wanted))
    # After the commit, so a mail problem cannot lose a saved request.
    notify.access_request_raised(session, user, wanted, note)
    return as_out(request, user)


@router.delete("/access-requests/mine", status_code=status.HTTP_204_NO_CONTENT)
def withdraw_request(user: CurrentUser, session: DbSession) -> None:
    """Withdraw an open request. Idempotent: nothing open is already the desired state."""
    pending = _pending_for(session, user.id)
    if pending is None:
        return
    pending.status = "withdrawn"
    pending.decided_at = None
    session.commit()
    audit.record("access_request_withdrawn", user.username)
