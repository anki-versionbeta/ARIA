from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class LoginIn(BaseModel):
    username: str
    password: str


class UserRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    display_name: str


class CurrentUserOut(UserRefOut):
    email: str | None = None


class SiloOut(BaseModel):
    id: str
    label: str
    # Bare extensions, e.g. [".pdf", ".docx"] — the frontend Dropzone uses these
    # directly, so there is no per-silo upload code.
    accepts: list[str]
    stages: list[str]


class RunFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    filename: str
    size_bytes: int
    content_type: str | None
    created_at: datetime


class DocumentSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    silo_id: str
    title: str
    status: str
    stage: str | None
    progress_pct: int
    progress_message: str | None
    owner: UserRefOut
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class DocumentDetailOut(DocumentSummaryOut):
    error_message: str | None
    files: list[RunFileOut]
    forked_from_run_id: str | None
    # Resolved server-side so the ownership rule (D14) has one source of truth.
    can_edit: bool


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    section_key: str
    content_html: str
    revision: int
    updated_at: datetime


class SectionSaveIn(BaseModel):
    html: str
    # Sent back with the edit so a stale copy is rejected rather than silently
    # overwriting a newer one (same user, two tabs).
    revision: int


class SectionSavedOut(BaseModel):
    section_key: str
    revision: int
    version_num: int
    label: str | None


class SectionVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_num: int
    label: str | None
    created_at: datetime


class SectionVersionDetailOut(SectionVersionOut):
    content_html: str


class DocumentStatusOut(BaseModel):
    """Light payload for the 2s poll — deliberately smaller than the full detail."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage: str | None
    progress_pct: int
    progress_message: str | None
    error_message: str | None
    finished_at: datetime | None


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
