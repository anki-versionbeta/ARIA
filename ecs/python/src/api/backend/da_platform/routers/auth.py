from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from api.backend.da_platform.auth import audit
from api.backend.da_platform.auth.access import (
    PROTECTED_ADMIN_USERNAMES,
    ROLE_ADMIN,
    accessible_module_ids,
)
from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.auth.providers import AuthProvider, UserInfo, build_provider
from api.backend.da_platform.auth.tokens import (
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
)
from api.backend.da_platform.db.models import AccessRequest, User, utcnow
from api.backend.da_platform.routers.schemas import AdministratorOut, CurrentUserOut, LoginIn

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache(maxsize=1)
def get_provider() -> AuthProvider:
    return build_provider()


def _pending_access_requests(session: DbSession, user: User) -> int:
    """Only meaningful to an admin, so it is not counted for anybody else -- this runs on
    every /auth/me, which the shell calls on every page."""
    if user.role != ROLE_ADMIN:
        return 0
    return (
        session.scalar(
            select(func.count(AccessRequest.id)).where(
                AccessRequest.status == "pending"
            )
        )
        or 0
    )


def _current_user_out(session: DbSession, user: User) -> CurrentUserOut:
    return CurrentUserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        modules=sorted(accessible_module_ids(session, user)),
        pending_access_requests=_pending_access_requests(session, user),
    )


def _upsert_user(session: DbSession, info: UserInfo) -> User:
    """Create the users row on first login, refresh it on later ones.

    No password is ever stored (spec section 9).
    """
    user = session.query(User).filter(User.username == info.username).one_or_none()
    if user is None:
        user = User(
            username=info.username,
            display_name=info.display_name,
            email=info.email,
        )
        session.add(user)
    else:
        user.display_name = info.display_name
        user.email = info.email or user.email
        user.last_seen = utcnow()
    session.commit()
    session.refresh(user)
    return user


@router.post("/login", response_model=CurrentUserOut)
def login(payload: LoginIn, response: Response, session: DbSession) -> User:
    info = get_provider().authenticate(payload.username, payload.password)
    if info is None:
        audit.record("login_failed", payload.username)
        # Active Directory binds on the short login name, with or without a domain suffix
        # (`haufwx`, `haufwx@abbvienet.com`, `haufwx@abbvie.com` all work). It does not
        # bind on the mail attribute, so `waldemar.hauf@abbvie.com` fails as
        # invalidCredentials -- indistinguishable, from here, from a wrong password. Two
        # people in the first week read that as "my password is broken" and went to reset
        # it. Resolving an address to a username would need a directory search, and that
        # needs a service account this provider deliberately does without, so the fix is
        # to say so rather than to guess.
        looks_like_an_email = "@" in payload.username and "." in (
            payload.username.split("@", 1)[0]
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Incorrect username or password. If you entered your email address, "
                "sign in with your AbbVie username instead."
                if looks_like_an_email
                else "Incorrect username or password"
            ),
        )

    user = _upsert_user(session, info)
    set_session_cookie(response, create_session_token(user.id, user.username))
    audit.record("login", user.username)
    return _current_user_out(session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    # Idempotent on purpose: clearing an already-absent session is not an error.
    clear_session_cookie(response)


@router.get("/administrators", response_model=list[AdministratorOut])
def administrators(_user: CurrentUser, session: DbSession) -> list[User]:
    """Who can grant module access, for any signed-in user to see.

    Deliberately not under /admin and deliberately not admin-only: its whole purpose is to
    tell somebody with no access who to ask. Read live rather than hardcoded in the
    frontend, so promoting or demoting an admin does not leave a stale name on the one
    screen a new joiner reads.

    Restricted to the fixed administrators rather than everyone holding the role. Any admin
    *can* grant access, but these two own it, and a new joiner should not have to guess
    which of a growing list to approach -- nor should someone promoted for one afternoon
    start receiving access requests. Same constant that makes them undemotable, so the two
    facts cannot drift apart.

    Includes the email address, so the request can be composed in one click. That is a
    list of addresses handed to every authenticated caller, which gave me pause -- but
    these are colleagues' work addresses, already visible to every employee in the
    corporate directory, and withholding them only means the person with no access has to
    go and find them by hand.
    """
    return list(
        session.scalars(
            select(User)
            .where(
                User.role == ROLE_ADMIN,
                func.lower(User.username).in_(PROTECTED_ADMIN_USERNAMES),
            )
            .order_by(User.display_name)
        ).all()
    )


@router.get("/me", response_model=CurrentUserOut)
def me(user: CurrentUser, session: DbSession) -> CurrentUserOut:
    """Re-read from the database on every call, so a role or module change an admin makes
    takes effect on the user's next request rather than at their next login."""
    return _current_user_out(session, user)
