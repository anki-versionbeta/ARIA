"""Section editing endpoints (spec §7).

Platform-owned and silo-agnostic: any silo that produces sections gets editing,
version history and restore without writing any of it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from api.backend.da_platform.routers.schemas import (
    SectionOut,
    SectionSaveIn,
    SectionSavedOut,
    SectionVersionDetailOut,
    SectionVersionOut,
)
from api.backend.da_platform.auth.access import require_module
from api.backend.da_platform.auth.deps import CurrentUser, DbSession
from api.backend.da_platform.db.models import Run, User
from api.backend.da_platform.records import sections as service
from api.backend.da_platform.records.queries import get_document
from api.backend.da_platform.silo_registry import get_silo

router = APIRouter(tags=["sections"])


def _readable(session: DbSession, document_id: str, user: User) -> Run:
    """Readable means "exists, and the caller holds its module".

    Takes the user for that second half. Section content is the document, so leaving this
    ungated would have let anyone read the body of a module they were never granted even
    though the document itself is hidden from them.
    """
    run = get_document(session, document_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    require_module(session, user, run.silo_id)
    return run


def _editable(session: DbSession, document_id: str, user: User) -> Run:
    run = _readable(session, document_id, user)
    if run.user_id != user.id:
        # D14/D15: the answer to "may I change this?" is no, but with a route
        # forward, so the UI offers a fork instead of a dead end.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"can_fork": True}
        )
    return run


def _declared_section_order(silo_id: str) -> list[str]:
    """The silo's own section sequence, or empty if it declares none.

    Optional part of the contract, like `router.py`: a silo that says nothing keeps the
    stored order rather than being forced to describe itself.
    """
    silo = get_silo(silo_id)
    if silo is None:
        return []
    return list(getattr(silo.module, "SECTION_ORDER", None) or [])


@router.get("/documents/{document_id}/sections", response_model=list[SectionOut])
def list_sections(document_id: str, user: CurrentUser, session: DbSession):
    # Readable by any authenticated user (D13).
    run = _readable(session, document_id, user)
    rows = service.list_sections(session, document_id)
    # Ordered the way the built document reads, not the way the rows happen to sort.
    return service.in_declared_order(rows, _declared_section_order(run.silo_id))


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
    document_id: str, section_key: str, user: CurrentUser, session: DbSession
):
    _readable(session, document_id, user)
    return service.list_versions(session, document_id, section_key)


@router.get(
    "/documents/{document_id}/sections/{section_key}/versions/{version_num}",
    response_model=SectionVersionDetailOut,
)
def get_version(
    document_id: str,
    section_key: str,
    version_num: int,
    user: CurrentUser,
    session: DbSession,
):
    _readable(session, document_id, user)
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
