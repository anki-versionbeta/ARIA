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
    # Role and modules ship with the session so the shell can decide what to render
    # without a second round trip. They are advisory for the UI only -- every endpoint
    # re-checks server-side, because anything the browser is told it can also be told
    # to lie about.
    role: str = "user"
    modules: list[str] = []
    # Admins only, and 0 for everyone else. Rides along on the session so the shell can
    # badge the menu without polling a second endpoint on every page.
    pending_access_requests: int = 0


class AdministratorOut(UserRefOut):
    """Who to ask for access. Shown to any signed-in user, so it carries only what a
    request needs: a name to address and an address to send to."""

    email: str | None = None


class AccessRequestIn(BaseModel):
    modules: list[str]
    note: str | None = None


class DecisionIn(BaseModel):
    """An approval or rejection. `modules` lets an admin approve part of a request; empty
    means "all of it", which is the usual answer."""

    modules: list[str] | None = None
    note: str | None = None


class AccessRequestOut(BaseModel):
    id: str
    user_id: str
    username: str
    display_name: str
    email: str | None = None
    modules: list[str]
    note: str | None = None
    status: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


class ModuleOut(BaseModel):
    """A grantable thing with a human label. Doubles as the role list, which has the
    same id/label shape, rather than inventing a second identical schema."""

    id: str
    label: str


class AdminUserOut(UserRefOut):
    email: str | None = None
    role: str
    modules: list[str]
    # True when the modules above come from the role (admin, super user) rather than from
    # grants, so the UI can show the checkboxes as implied-and-locked instead of empty.
    modules_from_role: bool
    # A fixed administrator: the UI locks the controls and explains why, instead of
    # offering an edit that the API will reject.
    protected: bool = False
    last_seen: datetime


class RoleIn(BaseModel):
    role: str


class ModulesIn(BaseModel):
    """The complete set of modules the user should end up with, not a delta."""

    modules: list[str]


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
