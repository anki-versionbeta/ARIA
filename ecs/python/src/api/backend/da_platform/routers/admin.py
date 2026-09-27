"""User management: roles and module access. Admin only.

Deliberately not a generic CRUD surface. Users are created by logging in (the LDAP
provider upserts them), so there is no create or delete here -- only the two things an
admin actually decides: what role someone has, and which modules they may reach.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, func, select

from api.backend.da_platform.routers.access_requests import as_out
from api.backend.da_platform.routers.schemas import (
    AccessRequestOut,
    AdminUserOut,
    DecisionIn,
    ModuleOut,
    ModulesIn,
    RoleIn,
)
from api.backend.da_platform.auth import audit
from api.backend.da_platform.auth.access import (
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLES,
    accessible_module_ids,
    is_protected_admin,
    known_module_ids,
    require_admin,
    sees_every_module,
)
from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import AccessRequest, User, UserModuleAccess, utcnow
from api.backend.da_platform.silo_registry import discover_silos

router = APIRouter(prefix="/admin", tags=["admin"])


def _load_user(session: DbSession, user_id: str) -> User:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user")
    return target


def _admin_count(session: DbSession) -> int:
    return (
        session.scalar(select(func.count(User.id)).where(User.role == ROLE_ADMIN)) or 0
    )


PROTECTED_MESSAGE = (
    "This account is a fixed administrator and cannot be changed here. Editing the list "
    "of fixed administrators is a code change."
)


def _reject_if_protected(target: User) -> None:
    if is_protected_admin(target):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "protected_admin", "message": PROTECTED_MESSAGE},
        )


def _as_out(session: DbSession, target: User) -> AdminUserOut:
    return AdminUserOut(
        protected=is_protected_admin(target),
        id=target.id,
        username=target.username,
        display_name=target.display_name,
        email=target.email,
        role=target.role,
        # For admin/super_user this is every module, which is what the UI should show:
        # their access is real, it just comes from the role rather than from grants.
        modules=sorted(accessible_module_ids(session, target)),
        modules_from_role=sees_every_module(target),
        last_seen=target.last_seen,
    )


@router.get("/roles", response_model=list[ModuleOut])
def list_roles(user: CurrentUser, session: DbSession) -> list[ModuleOut]:
    require_admin(user)
    return [ModuleOut(id=role, label=ROLE_LABELS[role]) for role in ROLES]


@router.get("/users", response_model=list[AdminUserOut])
def list_users(user: CurrentUser, session: DbSession) -> list[AdminUserOut]:
    require_admin(user)
    rows = session.scalars(select(User).order_by(User.display_name)).all()
    return [_as_out(session, row) for row in rows]


@router.put("/users/{user_id}/role", response_model=AdminUserOut)
def set_role(
    user_id: str, payload: RoleIn, user: CurrentUser, session: DbSession
) -> AdminUserOut:
    require_admin(user)
    if payload.role not in ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown role {payload.role!r}; expected one of {', '.join(ROLES)}",
        )

    target = _load_user(session, user_id)
    _reject_if_protected(target)

    # Refuse to remove the last admin. Without this, one careless change locks everybody
    # out of user management permanently and the only way back is hand-editing the
    # database.
    if (
        target.role == ROLE_ADMIN
        and payload.role != ROLE_ADMIN
        and _admin_count(session) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "last_admin",
                "message": (
                    "This is the only administrator. Promote someone else before "
                    "changing this role."
                ),
            },
        )

    previous = target.role
    target.role = payload.role
    session.commit()
    session.refresh(target)
    audit.record(
        "role_changed",
        user.username,
        detail=f"{target.username}: {previous} -> {payload.role}",
    )
    return _as_out(session, target)


@router.get("/access-requests", response_model=list[AccessRequestOut])
def list_access_requests(
    user: CurrentUser, session: DbSession, include_decided: bool = False
) -> list[AccessRequestOut]:
    """The queue every admin sees. Pending first, oldest first -- the order to work in."""
    require_admin(user)
    if include_decided:
        # History: newest is the useful end of it, and capped because nobody scrolls an
        # audit trail in a side panel -- the database has it all if anyone needs more.
        stmt = select(AccessRequest).order_by(AccessRequest.created_at.desc()).limit(50)
    else:
        stmt = (
            select(AccessRequest)
            .where(AccessRequest.status == "pending")
            .order_by(AccessRequest.created_at)
        )
    return [as_out(row, row.user) for row in session.scalars(stmt).unique().all()]


def _load_pending(session: DbSession, request_id: str) -> AccessRequest:
    request = session.get(AccessRequest, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown request"
        )
    if request.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "already_decided",
                "message": (
                    f"This request was already {request.status}"
                    + (f" by {request.decided_by}." if request.decided_by else ".")
                ),
            },
        )
    return request


@router.post("/access-requests/{request_id}/approve", response_model=AdminUserOut)
def approve_access_request(
    request_id: str, payload: DecisionIn, user: CurrentUser, session: DbSession
) -> AdminUserOut:
    """Grant what was asked for, on top of what the person already has.

    Adds rather than replaces: a request is for *more* access, so approving one should not
    quietly remove a module granted separately. `payload.modules` lets an admin approve a
    subset -- the common case of "you can have BOP but not ISO" -- and defaults to the
    whole request.
    """
    require_admin(user)
    request = _load_pending(session, request_id)
    target = request.user

    granting = sorted(set(payload.modules or list(request.modules or [])))
    unknown = sorted(set(granting) - known_module_ids())
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown module(s): {', '.join(unknown)}",
        )
    not_asked_for = sorted(set(granting) - set(request.modules or []))
    if not_asked_for:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Cannot grant modules that were not requested: "
                f"{', '.join(not_asked_for)}. Change their access directly instead."
            ),
        )

    existing = set(
        session.scalars(
            select(UserModuleAccess.module_id).where(
                UserModuleAccess.user_id == target.id
            )
        ).all()
    )
    for module_id in granting:
        if module_id not in existing:
            session.add(
                UserModuleAccess(
                    user_id=target.id, module_id=module_id, granted_by=user.username
                )
            )

    request.status = "approved"
    request.decided_at = utcnow()
    request.decided_by = user.username
    request.decision_note = (payload.note or "").strip() or None
    session.commit()
    session.refresh(target)
    audit.record(
        "access_request_approved",
        user.username,
        detail=f"{target.username}: {', '.join(granting) or 'nothing'}",
    )
    return _as_out(session, target)


@router.post("/access-requests/{request_id}/reject", response_model=AccessRequestOut)
def reject_access_request(
    request_id: str, payload: DecisionIn, user: CurrentUser, session: DbSession
) -> AccessRequestOut:
    require_admin(user)
    request = _load_pending(session, request_id)
    request.status = "rejected"
    request.decided_at = utcnow()
    request.decided_by = user.username
    request.decision_note = (payload.note or "").strip() or None
    session.commit()
    session.refresh(request)
    audit.record("access_request_rejected", user.username, detail=request.user.username)
    return as_out(request, request.user)


@router.put("/users/{user_id}/modules", response_model=AdminUserOut)
def set_modules(
    user_id: str, payload: ModulesIn, user: CurrentUser, session: DbSession
) -> AdminUserOut:
    """Replace a user's module grants with exactly `payload.modules`.

    Replace rather than add/remove endpoints: the admin screen edits a set of checkboxes,
    so one idempotent write matches what the user did and cannot half-apply.
    """
    require_admin(user)
    target = _load_user(session, user_id)
    # A fixed admin holds every module by role, so a grant here would be a silent no-op.
    # Rejecting is clearer than pretending the write meant something.
    _reject_if_protected(target)

    unknown = sorted(set(payload.modules) - known_module_ids())
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown module(s): {', '.join(unknown)}",
        )

    session.execute(
        delete(UserModuleAccess).where(UserModuleAccess.user_id == target.id)
    )
    for module_id in sorted(set(payload.modules)):
        session.add(
            UserModuleAccess(
                user_id=target.id, module_id=module_id, granted_by=user.username
            )
        )
    session.commit()
    session.refresh(target)
    audit.record(
        "modules_changed",
        user.username,
        detail=f"{target.username}: {', '.join(sorted(set(payload.modules))) or 'none'}",
    )
    return _as_out(session, target)
