"""Document creation.

The API's whole job here is: store the file, write one queued row, return. It never
runs a stage. ISO currently holds an HTTP request open for minutes doing Textract and
vision work, which is what makes 15 concurrent runs impossible.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, UploadFile, status

from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.db.models import Run, RunFile
from da_platform.settings import settings
from da_platform.silo_registry import get_silo
from da_platform.storage import INPUT, get_object_store, run_key
from da_platform.storage.base import PayloadTooLarge

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

CHUNK_SIZE = 1024 * 1024


def _chunks(upload: UploadFile) -> Iterator[bytes]:
    while True:
        chunk = upload.file.read(CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


@router.post(
    "/silos/{silo_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
)
def create_document(
    silo_id: str,
    file: UploadFile,
    user: CurrentUser,
    session: DbSession,
    title: str | None = None,
) -> dict[str, str]:
    silo = get_silo(silo_id)
    if silo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown silo {silo_id!r}"
        )

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A filename is required"
        )
    if not silo.accepts_filename(filename):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{silo.label} accepts {', '.join(silo.accepts)}",
        )

    run = Run(
        silo_id=silo.id,
        user_id=user.id,
        status="queued",
        stage=None,
        progress_pct=0,
        progress_message="Waiting for a worker",
        title=title or filename,
    )
    session.add(run)
    session.flush()

    key = run_key(
        silo.storage_prefix, run.id, INPUT, filename, silo.storage_folders
    )
    try:
        # Streamed against a ceiling so a large upload cannot exhaust the container
        # before it is rejected.
        size = get_object_store().put_stream(
            key, _chunks(file), max_bytes=settings.max_upload_bytes
        )
    except PayloadTooLarge:
        # Roll back so a rejected upload leaves no orphan row in the history.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Maximum upload size is {settings.max_upload_mb} MB",
        ) from None

    session.add(
        RunFile(
            run_id=run.id,
            kind=INPUT,
            filename=filename,
            storage_key=key,
            size_bytes=size,
            content_type=file.content_type,
        )
    )
    session.commit()

    logger.info(
        "Queued run %s for silo %s (%s, %d bytes)", run.id, silo.id, filename, size
    )
    return {"id": run.id}
