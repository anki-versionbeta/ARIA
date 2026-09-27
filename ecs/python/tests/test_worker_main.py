"""The worker process loop.

Nothing in the suite ran `worker/main.py` before this file, so the process that does
all the real work was the only unexercised half of the engine: `tests/test_engine.py`
drives `queue.claim` and `runner.run_one` directly, which proves the pipeline but never
the loop that is supposed to keep calling them, close its session, survive a failure,
reap dead workers and stop cleanly on a deploy.

**Which seam bounds the loop.** `main()` takes no arguments: there is no
max-iterations parameter and no injectable clock, and the loop condition is the module
global `_shutdown_requested` (worker/main.py:75). So the exit seam used here is that
global, driven exactly the way production drives it — a fake `queue.claim` (or the fake
clock's `sleep`) calls `worker_main._request_shutdown(SIGTERM, None)` once its scripted
work is done, which is what `docker stop` / an ECS task drain does. `worker_main.time`
is replaced with a fake clock so the 2s idle sleep and the 60s reap cadence are free and
deterministic; the clock starts at a large value because `time.monotonic()` in a real
process reflects host uptime, and starting at 0.0 would hide the fact that the reaper
fires on the very first iteration.

Two hazards are handled by autouse fixtures rather than by each test:

* `main()` installs process-wide SIGTERM/SIGINT handlers. Left in place, pytest's own
  Ctrl-C handling would be gone for the rest of the session, so they are saved and
  restored.
* `_shutdown_requested` is module state that leaks between tests; it is reset around
  every test, otherwise the first test to request shutdown makes every later one exit
  before doing any work.

Every scripted fake carries a hard call cap that raises `LoopRunaway`, which derives
from `BaseException` on purpose: `main()`'s `except Exception` would swallow anything
else and a broken test would hang the suite instead of failing it.
"""

from __future__ import annotations

import logging
import signal
from types import SimpleNamespace

import pytest

from api.backend.worker import main as worker_main
from tests.conftest import login, make_run

MAX_ITERATIONS = 25


class LoopRunaway(BaseException):
    """Escape hatch for a loop that will not stop.

    Deliberately not an `Exception`: the worker's own `except Exception` handler is
    what a runaway test would otherwise be caught by, turning a failure into a hang.
    """


class FakeClock:
    """Stands in for the `time` module inside `worker/main.py`.

    Starts high because `time.monotonic()` is host uptime in a real process; with a
    0.0 origin the startup reap would silently not happen (0.0 - 0.0 is not > 60).
    """

    def __init__(self, *, start: float = 41_000.0, shutdown_after_sleeps: int | None = None):
        self.now = start
        self.sleeps: list[float] = []
        self._shutdown_after = shutdown_after_sleeps

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Sleeping must move the clock, or the reap cadence could never elapse.
        self.now += seconds
        if len(self.sleeps) > MAX_ITERATIONS:
            raise LoopRunaway(f"{len(self.sleeps)} sleeps without stopping")
        if self._shutdown_after is not None and len(self.sleeps) >= self._shutdown_after:
            worker_main._request_shutdown(signal.SIGTERM, None)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeQueue:
    """Scripted `queue.claim`, and the loop's exit seam.

    Each script entry is either a run to hand back, `None` for an empty queue, or an
    exception instance to raise (a database outage). Once the script is exhausted it
    requests shutdown, so the loop ends the same way a deploy ends it.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[tuple[object, str]] = []

    def claim(self, session: object, worker_id: str) -> object:
        self.calls.append((session, worker_id))
        if len(self.calls) > MAX_ITERATIONS:
            raise LoopRunaway(f"claim called {len(self.calls)} times without stopping")
        if not self.script:
            worker_main._request_shutdown(signal.SIGTERM, None)
            return None
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeRunner:
    def __init__(self, *, outcomes: list[object] | None = None, on_run=None) -> None:
        self.calls: list[object] = []
        self._outcomes = list(outcomes or [])
        self._on_run = on_run

    def run_one(self, session: object, run: object) -> str:
        self.calls.append(run)
        if self._on_run is not None:
            self._on_run(run)
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return str(outcome)
        return "complete"


class FakeSessionFactory:
    """Counts opens and closes, because a session leaked per iteration exhausts the
    pool in a process that is meant to run for weeks."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    def __call__(self) -> object:
        self.opened += 1
        factory = self

        class _Session:
            def close(self) -> None:
                factory.closed += 1

        return _Session()


def fake_run(run_id: str = "run-1", silo: str = "probe") -> SimpleNamespace:
    return SimpleNamespace(id=run_id, silo_id=silo)


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """`main()` installs handlers process-wide; pytest needs its own back."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Module state, so it leaks into the next test and makes it exit immediately."""
    worker_main._shutdown_requested = False
    yield
    worker_main._shutdown_requested = False


@pytest.fixture
def harness(monkeypatch):
    """Wire the fakes into `worker/main.py` and hand back the recorders."""

    def install(
        *script: object,
        outcomes: list[object] | None = None,
        on_run=None,
        reap=None,
        clock: FakeClock | None = None,
        sessions: bool = True,
    ) -> SimpleNamespace:
        clock = clock or FakeClock()
        queue = FakeQueue(*script)
        runner = FakeRunner(outcomes=outcomes, on_run=on_run)
        reaps: list[float] = []

        def reap_once() -> list[str]:
            reaps.append(clock.now)
            if reap is None:
                return []
            if isinstance(reap, BaseException):
                raise reap
            return list(reap)

        monkeypatch.setattr(worker_main, "time", clock)
        monkeypatch.setattr(worker_main, "queue", queue)
        monkeypatch.setattr(worker_main, "runner", runner)
        monkeypatch.setattr(worker_main, "reap_once", reap_once)
        factory = FakeSessionFactory()
        if sessions:
            monkeypatch.setattr(worker_main, "SessionLocal", factory)
        return SimpleNamespace(
            clock=clock, queue=queue, runner=runner, reaps=reaps, sessions=factory
        )

    return install


# ── identity and shutdown, in isolation ───────────────────────────────────────


class TestWorkerIdentity:
    def test_the_worker_id_names_the_host_and_the_process(self):
        """The id is written to `runs.claimed_by`, which is how an operator finds
        which container is holding a stuck run."""
        import os
        import socket

        identity = worker_main.worker_id()
        assert identity.startswith(f"{socket.gethostname()}-{os.getpid()}-")

    def test_two_workers_in_one_container_get_different_ids(self):
        """`docker compose up --scale worker=N` is the documented way to scale, and a
        shared id would make the reaper unable to tell two workers apart."""
        assert worker_main.worker_id() != worker_main.worker_id()


class TestShutdownRequest:
    def test_a_signal_sets_the_flag_rather_than_exiting_immediately(self):
        """Killing mid-stage wastes LLM spend already incurred, so the handler only
        raises a flag that the loop notices between runs."""
        assert worker_main._shutdown_requested is False
        worker_main._request_shutdown(signal.SIGTERM, None)
        assert worker_main._shutdown_requested is True

    def test_the_handler_is_logged_so_a_deploy_is_visible_in_the_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger="api.backend.worker"):
            worker_main._request_shutdown(signal.SIGINT, None)
        assert "finishing the current stage" in caplog.text


# ── the loop ──────────────────────────────────────────────────────────────────


class TestLoop:
    def test_a_claimed_run_is_executed_and_the_worker_exits_cleanly(self, harness):
        run = fake_run()
        h = harness(run)
        assert worker_main.main() == 0
        assert h.runner.calls == [run]

    def test_every_queued_run_is_drained_before_the_worker_stops(self, harness):
        runs = [fake_run(f"run-{i}") for i in range(3)]
        h = harness(*runs)
        assert worker_main.main() == 0
        assert h.runner.calls == runs

    def test_the_same_identity_claims_every_run_so_the_reaper_can_attribute_them(
        self, harness
    ):
        h = harness(fake_run("a"), fake_run("b"))
        worker_main.main()
        identities = {identity for _, identity in h.queue.calls}
        assert len(identities) == 1
        assert next(iter(identities)).count("-") >= 2

    def test_an_empty_queue_sleeps_instead_of_spinning_on_the_database(self, harness):
        """Without the idle sleep an idle worker would hammer `claim` in a tight loop,
        which on Postgres means a `SELECT ... FOR UPDATE SKIP LOCKED` per microsecond."""
        h = harness(None, None, None)
        assert worker_main.main() == 0
        assert h.runner.calls == []
        # Three idle passes plus the pass that observes the shutdown request.
        assert h.clock.sleeps == [worker_main.IDLE_SLEEP_SECONDS] * 4

    def test_a_busy_worker_does_not_sleep_between_runs(self, harness):
        """Sleeping while work is queued would cap throughput far below the 15
        concurrent generations the split exists to support."""
        h = harness(fake_run("a"), fake_run("b"))
        worker_main.main()
        # Only the final idle pass that notices the shutdown sleeps.
        assert h.clock.sleeps == [worker_main.IDLE_SLEEP_SECONDS]

    def test_no_new_run_is_claimed_once_shutdown_has_been_requested(self, harness):
        """The flag is checked before claiming, so a draining task cannot pick up work
        it has no time left to finish."""
        h = harness(fake_run())
        worker_main._request_shutdown(signal.SIGTERM, None)
        assert worker_main.main() == 0
        assert h.queue.calls == []
        assert h.runner.calls == []

    def test_the_exit_is_logged_with_the_identity(self, harness, caplog):
        harness(None)
        with caplog.at_level(logging.INFO, logger="api.backend.worker"):
            worker_main.main()
        assert "exiting cleanly" in caplog.text

    def test_the_run_and_its_outcome_are_logged(self, harness, caplog):
        harness(fake_run("run-77"), outcomes=["awaiting_user"])
        with caplog.at_level(logging.INFO, logger="api.backend.worker"):
            worker_main.main()
        assert "Claimed run run-77" in caplog.text
        assert "ended as awaiting_user" in caplog.text


class TestFailureHandling:
    def test_a_run_that_raises_does_not_kill_the_worker(self, harness, caplog):
        """One bad run must not take the container down: the next queued run would
        otherwise wait for the orchestrator to notice and restart the task."""
        good = fake_run("good")
        h = harness(
            fake_run("bad"), good, outcomes=[RuntimeError("stage exploded"), "complete"]
        )
        with caplog.at_level(logging.ERROR, logger="api.backend.worker"):
            assert worker_main.main() == 0
        assert [r.id for r in h.runner.calls] == ["bad", "good"]
        assert "Unhandled error in the worker loop" in caplog.text

    def test_a_failure_backs_off_before_the_next_claim(self, harness):
        """If the database is the thing that is broken, retrying instantly turns one
        outage into a log flood."""
        h = harness(fake_run("bad"), outcomes=[RuntimeError("boom")])
        worker_main.main()
        assert h.clock.sleeps[0] == worker_main.IDLE_SLEEP_SECONDS

    def test_a_database_outage_while_claiming_is_survived(self, harness, caplog):
        """`claim` itself is the most likely thing to fail, and it fails outside the
        per-run try in every other engine test."""
        h = harness(RuntimeError("connection refused"), fake_run("after"))
        with caplog.at_level(logging.ERROR, logger="api.backend.worker"):
            assert worker_main.main() == 0
        assert [r.id for r in h.runner.calls] == ["after"]
        assert "Unhandled error" in caplog.text

    def test_the_session_is_closed_even_when_the_run_raises(self, harness):
        """A session leaked per failure is how a worker that has been up for a week
        stops being able to claim anything at all."""
        h = harness(fake_run("bad"), outcomes=[RuntimeError("boom")])
        worker_main.main()
        assert h.sessions.opened >= 1
        assert h.sessions.closed >= h.sessions.opened

    def test_a_session_is_opened_and_closed_for_each_busy_iteration(self, harness):
        h = harness(fake_run("a"), fake_run("b"))
        worker_main.main()
        # Two runs plus the pass that observes the shutdown request.
        assert h.sessions.opened == 3
        assert h.sessions.closed >= 3

    def test_the_idle_path_closes_its_session_twice(self, harness):
        """CHARACTERIZATION, not endorsed: worker/main.py:90 closes the session before
        the idle `continue`, and the `finally` at :101 closes it again. Harmless with
        SQLAlchemy (close is idempotent) but it is a redundant call on the hot idle
        path, and it is the kind of thing that stops being harmless if the session
        factory is ever swapped for something that counts.
        """
        # No script at all, so the very first claim requests shutdown and returns None:
        # exactly one idle iteration to count closes over.
        h = harness()
        worker_main.main()
        assert h.sessions.opened == 1
        assert h.sessions.closed == 2


class TestReaper:
    def test_the_reaper_runs_on_the_first_iteration(self, harness):
        """A worker starting up is often the replacement for one that just died, so
        recovering its runs must not wait a minute."""
        h = harness(None)
        worker_main.main()
        assert h.reaps == [41_000.0]

    def test_the_reaper_is_not_run_on_every_iteration(self, harness):
        """`requeue_stale` is a full scan of running runs; once a minute is the
        documented cadence, and per-iteration would be a scan per claim."""
        # Each run takes 30 simulated seconds, so the 60s window elapses every few
        # iterations rather than never or always.
        clock = FakeClock()
        h = harness(
            *[fake_run(f"run-{i}") for i in range(6)],
            clock=clock,
            on_run=lambda _run: clock.advance(30.0),
        )
        worker_main.main()

        iterations = len(h.queue.calls)
        assert iterations == 7
        assert 1 < len(h.reaps) < iterations, h.reaps
        gaps = [b - a for a, b in zip(h.reaps, h.reaps[1:])]
        assert all(gap > worker_main.REAP_EVERY_SECONDS for gap in gaps), h.reaps

    def test_requeued_runs_are_reported_as_a_warning(self, harness, caplog):
        """Silent recovery hides a worker that keeps dying; the warning is the only
        signal an operator gets."""
        harness(None, reap=["run-a", "run-b"])
        with caplog.at_level(logging.WARNING, logger="api.backend.worker"):
            worker_main.main()
        assert "re-queued 2 run(s)" in caplog.text

    def test_a_quiet_reap_says_nothing(self, harness, caplog):
        harness(None, reap=[])
        with caplog.at_level(logging.WARNING, logger="api.backend.worker"):
            worker_main.main()
        assert "re-queued" not in caplog.text

    def test_a_broken_reaper_does_not_stop_the_worker_doing_its_real_job(
        self, harness, caplog
    ):
        """The reaper is recovery, not the job. A failure there must not stop runs
        being claimed — that would turn a recovery bug into a total outage."""
        h = harness(fake_run("still-works"), reap=RuntimeError("reaper down"))
        with caplog.at_level(logging.ERROR, logger="api.backend.worker"):
            assert worker_main.main() == 0
        assert [r.id for r in h.runner.calls] == ["still-works"]
        assert "Reaper failed" in caplog.text

    def test_a_broken_reaper_is_not_retried_immediately(self, harness):
        """`last_reap` is updated in the failure path too, so a reaper that throws
        every time cannot become a tight loop of tracebacks."""
        h = harness(None, None, None, reap=RuntimeError("reaper down"))
        worker_main.main()
        assert len(h.reaps) == 1


class TestStartup:
    def test_signal_handlers_are_installed_for_both_stop_signals(self, harness):
        """ECS sends SIGTERM; a developer sends SIGINT. Both must drain rather than
        abandon an in-flight stage."""
        harness(None)
        worker_main.main()
        assert signal.getsignal(signal.SIGTERM) is worker_main._request_shutdown
        assert signal.getsignal(signal.SIGINT) is worker_main._request_shutdown

    def test_the_schema_is_created_when_running_locally(self, harness, monkeypatch):
        """One command to start locally; Alembic owns the schema from dev onward."""
        created: list[bool] = []
        monkeypatch.setattr(worker_main, "create_all", lambda: created.append(True))
        monkeypatch.setattr(
            worker_main,
            "settings",
            SimpleNamespace(is_local=True, storage_backend="local", silos_dir="/silos"),
        )
        harness(None)
        worker_main.main()
        assert created == [True]

    def test_the_schema_is_never_created_outside_local(self, harness, monkeypatch):
        """Creating tables in a deployed environment would silently diverge from the
        migration history Alembic owns."""
        monkeypatch.setattr(
            worker_main,
            "create_all",
            lambda: pytest.fail("create_all must not run outside local"),
        )
        monkeypatch.setattr(
            worker_main,
            "settings",
            SimpleNamespace(is_local=False, storage_backend="s3", silos_dir="/silos"),
        )
        harness(None)
        assert worker_main.main() == 0

    def test_startup_logs_the_configuration_an_operator_needs(self, harness, caplog):
        """Which storage backend and which silo directory: the two things that make a
        container behave differently from the one next to it."""
        harness(None)
        with caplog.at_level(logging.INFO, logger="api.backend.worker"):
            worker_main.main()
        assert "storage=local" in caplog.text
        assert "silos" in caplog.text


# ── against the real database and the real queue ──────────────────────────────


class TestAgainstRealRuns:
    def test_the_worker_claims_a_real_queued_run_and_marks_it_running(
        self, client, session, probe, monkeypatch
    ):
        """The fakes above prove the loop's shape; this proves the loop is wired to the
        actual claim. `queue`, `SessionLocal` and `reap_once` are all real here — only
        the stage execution and the clock are replaced, because running the probe silo's
        stages is `test_engine.py`'s job.

        The exit seam here is the clock rather than the queue: with the real `claim`,
        the loop stops once the queue drains and the first idle sleep happens.
        """
        # A run row needs its owner to exist: runs.user_id is a real foreign key.
        owner = login(client)
        run = make_run(
            session, owner_id=owner["id"], title="real.txt", status="queued"
        )

        executed: list[str] = []

        class Runner:
            def run_one(self, db, claimed) -> str:
                executed.append(claimed.id)
                # Prove the worker handed over a real, claimed Run row.
                assert claimed.status == "running"
                assert claimed.claimed_by
                return "complete"

        monkeypatch.setattr(worker_main, "time", FakeClock(shutdown_after_sleeps=1))
        monkeypatch.setattr(worker_main, "runner", Runner())

        assert worker_main.main() == 0
        assert executed == [run.id]

        session.expire_all()
        refreshed = session.get(type(run), run.id)
        assert refreshed.status == "running"
        assert refreshed.claimed_by is not None

    def test_an_empty_real_queue_leaves_the_worker_idle_and_stoppable(
        self, session, probe, monkeypatch
    ):
        """No queued rows at all is the normal steady state, and it must not raise."""
        clock = FakeClock(shutdown_after_sleeps=2)
        monkeypatch.setattr(worker_main, "time", clock)
        monkeypatch.setattr(
            worker_main, "runner", SimpleNamespace(run_one=lambda *_: pytest.fail("no run"))
        )
        assert worker_main.main() == 0
        assert clock.sleeps == [worker_main.IDLE_SLEEP_SECONDS] * 2
