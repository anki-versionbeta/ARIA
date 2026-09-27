from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from da_platform.auth.tokens import COOKIE_NAME, read_session_token
from da_platform.db.models import User
from da_platform.db.session import get_session

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
)


def current_user(
    session: Annotated[Session, Depends(get_session)],
    da_session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> User:
    if not da_session:
        raise UNAUTHORIZED

    claims = read_session_token(da_session)
    if not claims:
        raise UNAUTHORIZED

    user = session.get(User, claims.get("sub"))
    if user is None:
        # Token signed for a user that no longer exists.
        raise UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(current_user)]
DbSession = Annotated[Session, Depends(get_session)]
