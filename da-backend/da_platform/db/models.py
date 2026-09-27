"""The data model from design spec section 6.

The full schema is created now, even though phase 1 only reads `users` and
`runs`, so that porting a silo later does not require reshaping tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# One model definition serves both dialects (spec section 6).
JsonType = JSON().with_variant(JSONB(), "postgresql")

RUN_STATUSES = ("queued", "running", "awaiting_user", "complete", "failed")
TERMINAL_STATUSES = ("complete", "failed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256))
    # "admin" | "super_user" | "user" -- see da_platform.auth.access, which owns the
    # meaning of each. New users land on "user", i.e. no modules until an admin grants
    # them, so access is never widened by simply logging in.
    role: Mapped[str] = mapped_column(
        String(32), default="user", server_default="user", nullable=False
    )
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserModuleAccess(Base):
    """Which modules a `user` may reach. One module == one silo id.

    Only consulted for role="user": admin and super_user reach everything by role, so no
    rows are written or read for them. Composite primary key, so granting the same module
    twice is a no-op rather than a duplicate.
    """

    __tablename__ = "user_module_access"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    module_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # server_default as well as default: create_all() builds this table in local
    # environments, and without it the generated DDL is NOT NULL with no default, so any
    # plain SQL INSERT that omits the column fails.
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    granted_by: Mapped[str | None] = mapped_column(String(128))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    silo_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str | None] = mapped_column(String(64))
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    forked_from_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # `metadata` is reserved on the declarative base, so the attribute is renamed
    # while the column keeps the name the spec gives it.
    meta: Mapped[dict | None] = mapped_column("metadata", JsonType)

    owner: Mapped[User] = relationship(lazy="joined")
    files: Mapped[list[RunFile]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_runs_user_started", "user_id", "started_at"),
        Index("ix_runs_silo_started", "silo_id", "started_at"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_title", "title"),
    )


class RunFile(Base):
    __tablename__ = "run_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(512))
    storage_key: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="files")


class RunStageCheckpoint(Base):
    __tablename__ = "run_stage_checkpoints"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(64), primary_key=True)
    output: Mapped[dict | None] = mapped_column(JsonType)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentSection(Base):
    __tablename__ = "document_sections"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    section_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    content_html: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class DocumentSectionVersion(Base):
    """Append-only. Replaces BOP's 20-version in-memory cap and JSON spill."""

    __tablename__ = "document_section_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    section_key: Mapped[str] = mapped_column(String(256))
    version_num: Mapped[int] = mapped_column(Integer)
    content_html: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_section_versions_lookup", "run_id", "section_key", "version_num"),
    )


class SiloPromptOverride(Base):
    """Per-user overrides layered over the silo default (D17)."""

    __tablename__ = "silo_prompt_overrides"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    silo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
