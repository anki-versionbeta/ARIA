"""User management: roles and module access. Admin only.

Deliberately not a generic CRUD surface. Users are created by logging in (the LDAP
provider upserts them), so there is no create or delete here -- only the two things an
admin actually decides: what role someone has, and which modules they may reach.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, func, select

from da_platform.api.schemas import (
    AdminUserOut,
    ModuleOut,
    ModulesIn,
    RoleIn,
)
from da_platform.auth import audit
from da_platform.auth.access import (
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLES,
    accessible_module_ids,
    is_protected_admin,
    known_module_ids,
    require_admin,
    sees_every_module,
)
from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.db.models import User, UserModuleAccess
from da_platform.silo_registry import discover_silos

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


@router.get("/modules", response_model=list[ModuleOut])
def list_modules(user: CurrentUser, session: DbSession) -> list[ModuleOut]:
    """The grantable modules, straight from the silo registry.

    Read from the registry rather than a hardcoded list so a new silo appears here the
    moment it is mounted, instead of being invisible until someone remembers this file.
    """
    require_admin(user)
    return [ModuleOut(id=silo.id, label=silo.label) for silo in discover_silos()]


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
