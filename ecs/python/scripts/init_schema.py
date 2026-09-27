"""Create the base schema on whatever DATABASE_URL points at.

`create_all()` runs from the app itself only when DA_ENV=local, and the SQL files in
deploy/ are incremental — 002 does ALTER TABLE users, 003 references users(id) — so
none of them creates the base tables. A deployed database therefore comes up empty,
and nothing notices: /api/healthz only runs SELECT 1, which succeeds against an empty
schema, so the task reports healthy, joins the load balancer, and every real query
fails.

This closes that gap without adding Alembic. It is idempotent (create_all skips what
exists), so it is safe to run on every deploy, and it reports what it actually created
rather than claiming success.

    cd ecs/python && python -m scripts.init_schema

In a container it is ROLE=migrate, run once before the api service takes traffic.

Deliberately does NOT apply deploy/sql/001_worker_allowlist_guard.sql. That installs a
trigger rejecting run claims from hosts outside an allowlist holding one dev EC2's
hostname prefix; container hostnames are random task IDs and ECS does not support the
hostname field in awsvpc mode, so on ECS every run would sit in `queued` forever while
the worker logged and retried. The credential expiry it guards against cannot happen on
Fargate, where the task role renews itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402

from api.backend.da_platform.db.models import Base  # noqa: E402
from api.backend.da_platform.db.session import engine  # noqa: E402


def describe(url: str) -> str:
    """The target, with the password removed.

    Deployed runs put this in CloudWatch, and an assembled RDS URL carries the
    password in userinfo.
    """
    return make_url(url).render_as_string(hide_password=True)


def ensure_sqlite_parent(url: str) -> Path | None:
    """Create the directory a SQLite file will live in, if it does not exist.

    Postgres has no equivalent, so this is a no-op there. It matters because
    `db/session.py:create_all()` mkdirs the storage directory on the app's behalf and
    this script does not go through it, so a URL like sqlite:///./data/aria.db would
    otherwise fail with "unable to open database file" — which reads like a
    permissions problem rather than a missing directory.
    """
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if not parsed.database or parsed.database == ":memory:":
        return None
    parent = Path(parsed.database).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def create_schema(target: Engine) -> tuple[list[str], list[str]]:
    """Create any missing tables. Returns (before, after) table names."""
    before = sorted(inspect(target).get_table_names())
    Base.metadata.create_all(target)
    after = sorted(inspect(target).get_table_names())
    return before, after


def main() -> int:
    print(f"target   : {describe(str(engine.url))}")
    print(f"dialect  : {engine.dialect.name}")

    created_dir = ensure_sqlite_parent(str(engine.url))
    if created_dir is not None:
        print(f"data dir : {created_dir}")

    before, after = create_schema(engine)
    created = [name for name in after if name not in before]

    print(f"existing : {len(before)} table(s)")
    print(f"created  : {len(created)} table(s)")
    for name in created:
        print(f"    + {name}")

    expected = set(Base.metadata.tables)
    missing = sorted(expected - set(after))
    if missing:
        print(f"\nFAILED: {len(missing)} table(s) still missing", file=sys.stderr)
        for name in missing:
            print(f"    ! {name}", file=sys.stderr)
        return 1

    print(f"\nschema complete: all {len(expected)} tables present")
    if not created:
        print("(nothing to do - already up to date)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
