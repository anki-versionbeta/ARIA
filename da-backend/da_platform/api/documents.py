from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from da_platform.api.schemas import (
    DocumentDetailOut,
    DocumentStatusOut,
    DocumentSummaryOut,
    Paginated,
    RunFileOut,
)
from da_platform.auth.access import accessible_module_ids, require_module
from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.db.models import (
    DocumentSection,
    DocumentSectionVersion,
    Run,
    RunFile,
    RunStageCheckpoint,
    User,
)
from da_platform.engine import queue
from da_platform.records.queries import PAGE_SIZE, get_document, list_documents
from da_platform.storage import get_object_store

router = APIRouter(tags=["documents"])


def _load_readable(session: DbSession, document_id: str, user: User) -> Run:
    """Fetch a document the caller is allowed to see at all.

    Module access is checked before ownership, and before the row is returned to anyone:
    D13 ("any authenticated user may read any document") now holds only within the modules
    the caller has been granted. Checking it here rather than at each call site means a new
    read endpoint cannot forget it.
    """
    run = get_document(session, document_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    require_module(session, user, run.silo_id)
    return run


def _load_owned(session: DbSession, document_id: str, user: User) -> Run:
    """Fetch a document the caller is allowed to act on.

    Within a granted module anyone may read any document (D13), but only its owner may
    change it (D14). The 403 carries `can_fork` so the UI can offer "Create my own copy"
    instead of a dead end (D15).
    """
    run = _load_readable(session, document_id, user)
    if run.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"can_fork": True}
        )
    return run


@router.get("/documents", response_model=Paginated[DocumentSummaryOut])
def list_documents_endpoint(
    user: CurrentUser,
    session: DbSession,
    owner: Annotated[str | None, Query(alias="user")] = None,
    silo: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: str | None = None,
    sort: Literal["date", "name", "user"] = "date",
    order: Literal["asc", "desc"] = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
) -> Paginated[DocumentSummaryOut]:
    # Within a granted module every authenticated user may read every document (D13).
    # Outside one they see nothing, including the count -- so the restriction is passed
    # into the query rather than filtering the page afterwards, which would leak both the
    # total and the pagination shape.
    rows, total = list_documents(
        session,
        user=owner,
        silo=silo,
        status=status_filter,
        q=q,
        sort=sort,
        order=order,
        page=page,
        silos=accessible_module_ids(session, user),
    )
    return Paginated[DocumentSummaryOut](
        items=[DocumentSummaryOut.model_validate(row) for row in rows],
        page=page,
        page_size=PAGE_SIZE,
        total=total,
    )


def _detail(run: Run, current_user_id: str) -> DocumentDetailOut:
    return DocumentDetailOut(
        **DocumentSummaryOut.model_validate(run).model_dump(),
        error_message=run.error_message,
        files=[RunFileOut.model_validate(item) for item in run.files],
        forked_from_run_id=run.forked_from_run_id,
        can_edit=run.user_id == current_user_id,
    )


@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
def get_document_endpoint(
    document_id: str, user: CurrentUser, session: DbSession
) -> DocumentDetailOut:
    run = _load_readable(session, document_id, user)
    return _detail(run, user.id)


@router.get("/documents/{document_id}/status", response_model=DocumentStatusOut)
def get_document_status(
    document_id: str, user: CurrentUser, session: DbSession
) -> Run:
    """Polled every 2 seconds by the browser, so it stays a single indexed read."""
    return _load_readable(session, document_id, user)


@router.post("/documents/{document_id}/build", status_code=status.HTTP_202_ACCEPTED)
def build_document(
    document_id: str,
    user: CurrentUser,
    session: DbSession,
    payload: dict | None = None,
) -> dict[str, str]:
    """Resume a run parked for user action.

    The optional body is stored as the paused stage's checkpoint, so this one endpoint
    covers both "just continue" (BOP's build) and "continue with my answer". A
    silo-specific endpoint that needs to validate a particular shape delegates here.
    """
    run = _load_owned(session, document_id, user)
    try:
        queue.complete_paused_stage(session, run, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return {"id": run.id, "status": run.status}


@router.post("/documents/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_document(
    document_id: str, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    """Re-enqueue a failed run.

    Checkpoints are kept on failure, so this resumes from the last completed stage
    rather than repeating work that already cost time and LLM spend.
    """
    run = _load_owned(session, document_id, user)
    if run.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is {run.status}; only a failed run can be retried",
        )
    queue.resume(session, run)
    return {"id": run.id, "status": run.status}


@router.get("/documents/{document_id}/files/{file_id}")
def download_file(
    document_id: str, file_id: str, user: CurrentUser, session: DbSession
) -> StreamingResponse:
    """Stream an input or output file. Readable by any user holding the module (D13)."""
    run = _load_readable(session, document_id, user)

    record = next((item for item in run.files if item.id == file_id), None)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        handle = get_object_store().open(record.storage_key)
    except FileNotFoundError as exc:
        # The row exists but the object does not — worth distinguishing from a bad id.
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="The stored file is missing"
        ) from exc

    return StreamingResponse(
        handle,
        media_type=record.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename}"',
        },
    )


@router.post("/documents/{document_id}/fork", status_code=status.HTTP_201_CREATED)
def fork_document_endpoint(
    document_id: str, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    """Produce an independent copy owned by the caller (spec section 8).

    Input files reference the *same* storage key rather than duplicating large
    PDFs, which is safe because nothing is ever deleted.
    """
    original = _load_readable(session, document_id, user)

    fork = Run(
        silo_id=original.silo_id,
        user_id=user.id,
        # A fork has no output until its owner builds one, so a finished original
        # yields a copy parked for user action rather than a complete document.
        status="awaiting_user" if original.status == "complete" else original.status,
        stage=original.stage,
        progress_pct=original.progress_pct,
        progress_message=original.progress_message,
        title=f"Copy of {original.title}",
        forked_from_run_id=original.id,
        meta=original.meta,
    )
    session.add(fork)
    session.flush()

    for source in original.files:
        if source.kind != "input":
            continue
        session.add(
            RunFile(
                run_id=fork.id,
                kind=source.kind,
                filename=source.filename,
                storage_key=source.storage_key,
                size_bytes=source.size_bytes,
                content_type=source.content_type,
            )
        )

    checkpoints = (
        session.query(RunStageCheckpoint)
        .filter(RunStageCheckpoint.run_id == original.id)
        .all()
    )
    for checkpoint in checkpoints:
        session.add(
            RunStageCheckpoint(
                run_id=fork.id, stage=checkpoint.stage, output=checkpoint.output
            )
        )

    sections = (
        session.query(DocumentSection)
        .filter(DocumentSection.run_id == original.id)
        .all()
    )
    for section in sections:
        session.add(
            DocumentSection(
                run_id=fork.id,
                section_key=section.section_key,
                content_html=section.content_html,
                revision=1,
                updated_by=user.id,
            )
        )
        # Version history deliberately does not carry over; the fork's history is
        # its own and the link back is the lineage pointer.
        session.add(
            DocumentSectionVersion(
                run_id=fork.id,
                section_key=section.section_key,
                version_num=1,
                content_html=section.content_html,
                label=f"Forked from {original.title}",
                created_by=user.id,
            )
        )

    session.commit()
    return {"id": fork.id}
