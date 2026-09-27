"""The atomic queue claim — the only dialect-specific code in the platform (D26).

`runs` IS the job queue, so this one statement decides whether two workers can ever pick
up the same document. The SQLite path is exercised for real against the test database; the
Postgres path cannot be, without a server, so it is covered by checking the branch is
selected and the statement carries FOR UPDATE SKIP LOCKED — the clause that makes it safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.backend.da_platform.db.dialect import claim_next_run
from api.backend.da_platform.db.models import Run, User, utcnow


def make_user(session) -> User:
    user = User(username="queue.owner", display_name="Queue Owner",
                email="q@abbvie.com", role="admin")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def queue_run(session, owner_id: str, title: str, *, status: str = "queued",
              age_minutes: int = 0) -> Run:
    run = Run(
        silo_id="probe",
        user_id=owner_id,
        title=title,
        status=status,
        started_at=utcnow() - timedelta(minutes=age_minutes),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


# ── the SQLite path, for real ─────────────────────────────────────────────────


def test_an_empty_queue_yields_nothing(session):
    assert claim_next_run(session, "worker-1", NOW) is None


def test_a_queued_run_is_claimed(session):
    owner = make_user(session)
    run = queue_run(session, owner.id, "Only one")

    claimed = claim_next_run(session, "worker-1", NOW)

    assert claimed == run.id


def test_claiming_marks_the_run_running_and_records_the_worker(session):
    owner = make_user(session)
    run = queue_run(session, owner.id, "To claim")

    claim_next_run(session, "worker-7", NOW)
    session.commit()
    session.refresh(run)

    assert run.status == "running"
    assert run.claimed_by == "worker-7"
    assert run.claimed_at is not None
    # The reaper compares against heartbeat_at, so it must be set at claim time or a
    # fresh claim looks immediately stale.
    assert run.heartbeat_at is not None


def test_the_oldest_queued_run_is_claimed_first(session):
    owner = make_user(session)
    newest = queue_run(session, owner.id, "Newest", age_minutes=1)
    oldest = queue_run(session, owner.id, "Oldest", age_minutes=90)
    middle = queue_run(session, owner.id, "Middle", age_minutes=30)

    first = claim_next_run(session, "worker-1", NOW)
    session.commit()

    assert first == oldest.id
    assert first not in (newest.id, middle.id)


def test_successive_claims_never_return_the_same_run(session):
    """The property that matters: no document is processed twice."""
    owner = make_user(session)
    ids = {queue_run(session, owner.id, f"Run {n}", age_minutes=n).id for n in range(1, 4)}

    claimed = []
    for n in range(3):
        got = claim_next_run(session, f"worker-{n}", NOW)
        session.commit()
        claimed.append(got)

    assert len(set(claimed)) == 3
    assert set(claimed) == ids
    # And the queue is then genuinely empty.
    assert claim_next_run(session, "worker-x", NOW) is None


@pytest.mark.parametrize("status", ["running", "complete", "failed", "awaiting_user"])
def test_only_queued_runs_are_claimable(session, status):
    """A run parked for review must not be dragged back into a worker."""
    owner = make_user(session)
    queue_run(session, owner.id, f"In {status}", status=status)

    assert claim_next_run(session, "worker-1", NOW) is None


def test_a_claim_returns_a_string_id(session):
    owner = make_user(session)
    queue_run(session, owner.id, "Typed")

    claimed = claim_next_run(session, "worker-1", NOW)

    # The caller uses it as a Run id; a Row or int would break the lookup.
    assert isinstance(claimed, str)


def test_claiming_leaves_other_queued_runs_alone(session):
    owner = make_user(session)
    oldest = queue_run(session, owner.id, "Oldest", age_minutes=60)
    later = queue_run(session, owner.id, "Later", age_minutes=5)

    claim_next_run(session, "worker-1", NOW)
    session.commit()
    session.refresh(later)

    assert later.status == "queued"
    assert later.claimed_by is None
    assert oldest.id != later.id


def test_an_unbound_session_is_rejected():
    """Better a clear error than a confusing failure deeper in SQLAlchemy."""

    class Unbound:
        bind = None

    with pytest.raises(RuntimeError, match="not bound"):
        claim_next_run(Unbound(), "worker-1", NOW)


# ── the Postgres path, without a server ──────────────────────────────────────


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _Recorded:
    """Captures the statements a session is asked to run."""

    def __init__(self, first_scalar):
        self.bind = _FakeBind()
        self.statements: list[str] = []
        self.params: list[dict | None] = []
        self._scalars = [first_scalar]

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params)
        outcome = self._scalars.pop(0) if self._scalars else None

        class Result:
            def scalar(self_inner):
                return outcome

        return Result()


def test_a_postgres_session_takes_the_skip_locked_path():
    session = _Recorded(first_scalar="run-abc")

    claimed = claim_next_run(session, "worker-9", NOW)

    assert claimed == "run-abc"
    select_sql = session.statements[0]
    # This clause is what stops two workers claiming the same row under Postgres.
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "status = 'queued'" in select_sql
    assert "ORDER BY started_at" in select_sql


def test_the_postgres_path_updates_the_row_it_selected():
    session = _Recorded(first_scalar="run-abc")

    claim_next_run(session, "worker-9", NOW)

    update_sql, update_params = session.statements[1], session.params[1]
    assert "UPDATE runs" in update_sql
    assert "status = 'running'" in update_sql
    assert update_params == {"worker": "worker-9", "now": NOW, "id": "run-abc"}


def test_the_postgres_path_does_not_update_when_the_queue_is_empty():
    session = _Recorded(first_scalar=None)

    assert claim_next_run(session, "worker-9", NOW) is None
    # Only the SELECT ran: no row was touched.
    assert len(session.statements) == 1
