"""Who may reach which module.

One module == one silo id. ATR and MFGR are report types inside `mfg_atr`, so they are
granted together; splitting them would be a schema change, not a config tweak.

`admin` and `super_user` reach every module by definition, so no grant rows are read or
written for them. Only `user` is gated by `user_module_access`. Keeping that rule in one
place is the point of this module: the alternative is every silo router growing its own
half-remembered version of it.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.backend.da_platform.db.models import User, UserModuleAccess
from api.backend.da_platform.silo_registry import discover_silos

ROLE_ADMIN = "admin"
ROLE_SUPER_USER = "super_user"
ROLE_USER = "user"

# Ordered most privileged first, which is the order the admin UI shows them in.
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_SUPER_USER, ROLE_USER)

ROLE_LABELS = {
    ROLE_ADMIN: "Admin",
    ROLE_SUPER_USER: "Super user",
    ROLE_USER: "User",
}

# Fixed administrators: their role cannot be changed through the API by anyone, including
# another admin and themselves. The last-admin check alone is not enough -- it only
# guarantees *someone* keeps the role, not that these two do, so a third admin could
# demote both of them and take over user management.
#
# Deliberately a constant and not a database flag: a flag is editable by whoever owns the
# database, which is the same surface being protected. Changing this list is a code change
# with review, which is the point.
PROTECTED_ADMIN_USERNAMES: frozenset[str] = frozenset({"bapatar", "mohanax25"})


def is_protected_admin(user: User) -> bool:
    return (user.username or "").strip().lower() in PROTECTED_ADMIN_USERNAMES


def known_module_ids() -> set[str]:
    return {silo.id for silo in discover_silos()}


def sees_every_module(user: User) -> bool:
    return user.role in (ROLE_ADMIN, ROLE_SUPER_USER)


def accessible_module_ids(session: Session, user: User) -> set[str]:
    """The modules this user may reach, always intersected with the silos that exist.

    The intersection matters: a grant for a silo that was later removed would otherwise
    linger and let a stale row widen access to something unexpected.
    """
    if sees_every_module(user):
        return known_module_ids()
    granted = set(
        session.scalars(
            select(UserModuleAccess.module_id).where(UserModuleAccess.user_id == user.id)
        ).all()
    )
    return granted & known_module_ids()


def require_module(session: Session, user: User, module_id: str) -> None:
    """403 unless the caller holds `module_id`.

    The body names the module so the UI can say which access is missing instead of a bare
    "Forbidden", and so it reads clearly in the API logs.
    """
    if module_id not in accessible_module_ids(session, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "module_not_granted",
                "module": module_id,
                "message": (
                    "You do not have access to this module. Ask an administrator to "
                    "grant it under User management."
                ),
            },
        )


def require_admin(user: User) -> None:
    if user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "admin_only",
                "message": "Only an administrator can manage users and access.",
            },
        )
