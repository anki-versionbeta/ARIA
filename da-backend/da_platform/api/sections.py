"""Section editing endpoints (spec §7).

Platform-owned and silo-agnostic: any silo that produces sections gets editing,
version history and restore without writing any of it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from da_platform.api.schemas import (
    SectionOut,
    SectionSaveIn,
    SectionSavedOut,
    SectionVersionDetailOut,
    SectionVersionOut,
)
from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.db.models import Run, User
from da_platform.records import sections as service
from da_platform.records.queries import get_document

router = APIRouter(tags=["sections"])


def _readable(session: DbSession, document_id: str) -> Run:
    run = get_document(session, document_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return run


def _editable(session: DbSession, document_id: str, user: User) -> Run:
    run = _readable(session, document_id)
    if run.user_id != user.id:
        # D14/D15: the answer to "may I change this?" is no, but with a route
        # forward, so the UI offers a fork instead of a dead end.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"can_fork": True}
        )
    return run


@router.get("/documents/{document_id}/sections", response_model=list[SectionOut])
def list_sections(document_id: str, _user: CurrentUser, session: DbSession):
    # Readable by any authenticated user (D13).
    _readable(session, document_id)
    return service.list_sections(session, document_id)


@router.put(
    "/documents/{document_id}/sections/{section_key}", response_model=SectionSavedOut
)
def save_section(
    document_id: str,
    section_key: str,
    payload: SectionSaveIn,
    user: CurrentUser,
    session: DbSession,
) -> SectionSavedOut:
    run = _editable(session, document_id, user)
    try:
        section, version = service.save_section(
            session, run.id, section_key, payload.html, payload.revision, user.id
        )
    except service.StaleRevision as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This section changed since you loaded it",
                "current_revision": exc.actual,
            },
        ) from exc
    return SectionSavedOut(
        section_key=section.section_key,
        revision=section.revision,
        version_num=version.version_num,
        label=version.label,
    )


@router.get(
    "/documents/{document_id}/sections/{section_key}/versions",
    response_model=list[SectionVersionOut],
)
def list_versions(
    document_id: str, section_key: str, _user: CurrentUser, session: DbSession
):
    _readable(session, document_id)
    return service.list_versions(session, document_id, section_key)


@router.get(
    "/documents/{document_id}/sections/{section_key}/versions/{version_num}",
    response_model=SectionVersionDetailOut,
)
def get_version(
    document_id: str,
    section_key: str,
    version_num: int,
    _user: CurrentUser,
    session: DbSession,
):
    _readable(session, document_id)
    version = service.get_version(session, document_id, section_key, version_num)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return version


@router.post(
    "/documents/{document_id}/sections/{section_key}/restore/{version_num}",
    response_model=SectionSavedOut,
)
def restore_version(
    document_id: str,
    section_key: str,
    version_num: int,
    user: CurrentUser,
    session: DbSession,
) -> SectionSavedOut:
    run = _editable(session, document_id, user)
    try:
        section, version = service.restore_version(
            session, run.id, section_key, version_num, user.id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return SectionSavedOut(
        section_key=section.section_key,
        revision=section.revision,
        version_num=version.version_num,
        label=version.label,
    )
