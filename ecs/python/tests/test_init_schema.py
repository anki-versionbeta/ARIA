"""The schema bootstrap that ROLE=migrate runs.

Covers the two properties a deploy depends on: it creates the whole schema on an empty
database, and it is safe to run again. Also covers password redaction, because the
assembled RDS URL carries the password in userinfo and this script prints its target.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from api.backend.da_platform.db.models import Base
from scripts.init_schema import create_schema, describe, ensure_sqlite_parent


def test_creates_every_table_on_an_empty_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")

    before, after = create_schema(engine)

    assert before == []
    assert set(after) == set(Base.metadata.tables)


def test_running_it_again_changes_nothing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'twice.db'}")
    create_schema(engine)

    before, after = create_schema(engine)

    # Idempotent, so a deploy can run it unconditionally.
    assert before == after
    assert set(after) == set(Base.metadata.tables)


def test_an_existing_table_is_left_alone(tmp_path):
    """A partially migrated database must not lose what is already there."""
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    Base.metadata.tables["users"].create(engine)

    before, after = create_schema(engine)

    assert before == ["users"]
    assert "users" in after
    assert set(after) == set(Base.metadata.tables)


def test_the_reported_target_hides_the_password():
    shown = describe(
        "postgresql+psycopg2://aria:s3cr3t-p4ssw0rd@db.example.rds.amazonaws.com:5432/aria"
    )

    assert "s3cr3t-p4ssw0rd" not in shown
    # Still identifiable, or the log line is useless.
    assert "db.example.rds.amazonaws.com" in shown
    assert "aria" in shown


def test_a_sqlite_target_is_reported_unchanged():
    # No userinfo to redact; the path is what identifies it.
    assert describe("sqlite:///./dev.db") == "sqlite:///./dev.db"


def test_a_sqlite_directory_is_created_if_missing(tmp_path):
    """sqlite cannot create its own parent directory.

    Without this the script fails with "unable to open database file", which reads like
    a permissions problem rather than a missing folder.
    """
    target = tmp_path / "does" / "not" / "exist" / "aria.db"

    created = ensure_sqlite_parent(f"sqlite:///{target}")

    assert created == target.parent
    assert target.parent.is_dir()

    # And the schema can then actually be written there.
    engine = create_engine(f"sqlite:///{target}")
    _before, after = create_schema(engine)
    assert set(after) == set(Base.metadata.tables)


def test_a_postgres_url_creates_no_directory(tmp_path):
    # Nothing to mkdir for a network database, and guessing a path would be wrong.
    assert ensure_sqlite_parent("postgresql+psycopg2://u:p@host:5432/aria") is None


def test_an_in_memory_sqlite_url_creates_no_directory():
    assert ensure_sqlite_parent("sqlite://") is None
    assert ensure_sqlite_parent("sqlite:///:memory:") is None


def test_the_created_schema_matches_the_orm(tmp_path):
    """Column-level check, not just table names."""
    engine = create_engine(f"sqlite:///{tmp_path / 'columns.db'}")
    create_schema(engine)

    inspector = inspect(engine)
    for name, table in Base.metadata.tables.items():
        actual = {column["name"] for column in inspector.get_columns(name)}
        expected = {column.name for column in table.columns}
        assert actual == expected, name
