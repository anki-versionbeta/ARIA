"""Section content and version history — platform-owned and silo-agnostic (spec §7).

Every save is a **synchronous write before the API returns**, not a background
flush. That is what makes an afternoon of editing survive a restart; the app being
replaced keeps its primary copy in a process-local dict and spills older versions to
disk, so a deploy loses work.

Version history is a table, which removes that app's 20-version in-memory cap
entirely.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from da_platform.db.models import DocumentSection, DocumentSectionVersion, utcnow
from da_platform.records.html_content import sanitize_section_html


class StaleRevision(RuntimeError):
    """The caller edited from an out-of-date copy.

    Easy to hit with the same user in two browser tabs, which matters when runs take
    minutes and people leave tabs open.
    """

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"Revision {expected} is stale; current is {actual}")
        self.expected = expected
        self.actual = actual


def list_sections(session: Session, run_id: str) -> list[DocumentSection]:
    return list(
        session.scalars(
            select(DocumentSection)
            .where(DocumentSection.run_id == run_id)
            .order_by(DocumentSection.section_key)
        ).all()
    )


def get_section(session: Session, run_id: str, key: str) -> DocumentSection | None:
    return session.get(DocumentSection, {"run_id": run_id, "section_key": key})


def _next_version_num(session: Session, run_id: str, key: str) -> int:
    highest = session.scalar(
        select(func.max(DocumentSectionVersion.version_num)).where(
            DocumentSectionVersion.run_id == run_id,
            DocumentSectionVersion.section_key == key,
        )
    )
    return (highest or 0) + 1


def _add_version(
    session: Session,
    run_id: str,
    key: str,
    html: str,
    label: str,
    user_id: str | None,
) -> DocumentSectionVersion:
    version = DocumentSectionVersion(
        run_id=run_id,
        section_key=key,
        version_num=_next_version_num(session, run_id, key),
        content_html=html,
        label=label,
        created_by=user_id,
    )
    session.add(version)
    return version


def seed_sections(
    session: Session,
    run_id: str,
    content: dict[str, str],
    user_id: str | None,
    label: str = "Generated",
) -> int:
    """Write the sections a silo just generated, each with its first version.

    Called from a stage, so the generated document is durable the moment it exists
    rather than when the user first saves.
    """
    for key, html in content.items():
        existing = get_section(session, run_id, key)
        if existing is None:
            session.add(
                DocumentSection(
                    run_id=run_id,
                    section_key=key,
                    content_html=html,
                    revision=1,
                    updated_by=user_id,
                )
            )
        else:
            existing.content_html = html
            existing.revision += 1
            existing.updated_at = utcnow()
            existing.updated_by = user_id
        _add_version(session, run_id, key, html, label, user_id)
    session.commit()
    return len(content)


def save_section(
    session: Session,
    run_id: str,
    key: str,
    html: str,
    revision: int,
    user_id: str,
) -> tuple[DocumentSection, DocumentSectionVersion]:
    """Persist an edit and append a version. Raises `StaleRevision` on conflict."""
    # Sanitised here rather than in the router, so anything reaching the database is
    # already safe no matter which caller wrote it.
    html = sanitize_section_html(html)
    section = get_section(session, run_id, key)
    if section is None:
        section = DocumentSection(
            run_id=run_id, section_key=key, content_html=html, revision=1
        )
        session.add(section)
        version = _add_version(session, run_id, key, html, "Edited (v1)", user_id)
        session.commit()
        return section, version

    if section.revision != revision:
        raise StaleRevision(revision, section.revision)

    section.content_html = html
    section.revision += 1
    section.updated_at = utcnow()
    section.updated_by = user_id
    version = _add_version(
        session, run_id, key, html, f"Edited (v{_next_version_num(session, run_id, key)})", user_id
    )
    session.commit()
    return section, version


def list_versions(session: Session, run_id: str, key: str) -> list[DocumentSectionVersion]:
    return list(
        session.scalars(
            select(DocumentSectionVersion)
            .where(
                DocumentSectionVersion.run_id == run_id,
                DocumentSectionVersion.section_key == key,
            )
            .order_by(DocumentSectionVersion.version_num.desc())
        ).all()
    )


def get_version(
    session: Session, run_id: str, key: str, version_num: int
) -> DocumentSectionVersion | None:
    return session.scalar(
        select(DocumentSectionVersion).where(
            DocumentSectionVersion.run_id == run_id,
            DocumentSectionVersion.section_key == key,
            DocumentSectionVersion.version_num == version_num,
        )
    )


def restore_version(
    session: Session, run_id: str, key: str, version_num: int, user_id: str
) -> tuple[DocumentSection, DocumentSectionVersion]:
    """Restore an earlier version by appending it as a new one.

    History is append-only, so restoring never erases what came after it.
    """
    source = get_version(session, run_id, key, version_num)
    if source is None:
        raise LookupError(f"Version {version_num} of {key!r} does not exist")

    section = get_section(session, run_id, key)
    if section is None:
        section = DocumentSection(
            run_id=run_id, section_key=key, content_html=source.content_html, revision=1
        )
        session.add(section)
    else:
        section.content_html = source.content_html
        section.revision += 1
        section.updated_at = utcnow()
        section.updated_by = user_id

    version = _add_version(
        session,
        run_id,
        key,
        source.content_html,
        f"Restored v{version_num}",
        user_id,
    )
    session.commit()
    return section, version
