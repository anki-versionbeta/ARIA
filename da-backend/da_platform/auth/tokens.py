"""Stateless sessions: a signed JWT in an httpOnly cookie (spec section 9).

No session table, so nothing to reconcile across API replicas, and no password
is ever stored or cached.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Response
from jose import JWTError, jwt

from da_platform.settings import settings

COOKIE_NAME = "da_session"
ALGORITHM = "HS256"


def create_session_token(user_id: str, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def read_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_ttl_hours * 3600,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")
