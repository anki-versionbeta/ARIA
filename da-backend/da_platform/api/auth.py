from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException, Response, status

from da_platform.auth import audit
from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.auth.providers import AuthProvider, UserInfo, build_provider
from da_platform.auth.tokens import (
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
)
from da_platform.db.models import User, utcnow
from da_platform.api.schemas import CurrentUserOut, LoginIn

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache(maxsize=1)
def get_provider() -> AuthProvider:
    return build_provider()


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    user = _upsert_user(session, info)
    set_session_cookie(response, create_session_token(user.id, user.username))
    audit.record("login", user.username)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    # Idempotent on purpose: clearing an already-absent session is not an error.
    clear_session_cookie(response)


@router.get("/me", response_model=CurrentUserOut)
def me(user: CurrentUser) -> User:
    return user
