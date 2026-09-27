"""Engine behaviour, proved with the fixture silo.

These are the properties spec section 1 says are broken today: work survives a
restart, a human pause does not hold a thread, and a killed worker's runs are
recovered.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from api.backend.da_platform.db.models import Run, RunFile, utcnow
from api.backend.da_platform.engine import queue, runner
from api.backend.da_platform.storage import get_object_store
from tests.conftest import login


def upload(client, content: bytes = b"hello", filename: str = "source.txt"):
    response = client.post(
        "/api/silos/probe/documents",
        files={"file": (filename, content, "text/plain")},
    )
    return response


def queued_run(client, session) -> Run:
    login(client)
    response = upload(client)
    assert response.status_code == 202, response.text
    return session.get(Run, response.json()["id"])


# ── discovery ─────────────────────────────────────────────────────────────────


def test_fixture_silo_is_discovered_with_its_contract(probe):
    assert probe.id == "probe"
    assert probe.label == "Engine Probe"
    assert probe.stages == ["prepare", "await_input", "finish"]
    assert probe.accepts == [".txt", ".pdf"]
    # Every declared stage resolves to a callable, checked at discovery.
    for stage in probe.stages:
        assert callable(probe.stage_callable(stage))


def test_silos_endpoint_exposes_bare_extensions(client, probe):
    login(client)
    payload = client.get("/api/silos").json()
    assert payload == [
        {
            "id": "probe",
            "label": "Engine Probe",
            "accepts": [".txt", ".pdf"],
            "stages": ["prepare", "await_input", "finish"],
        }
    ]


# ── upload ────────────────────────────────────────────────────────────────────


def test_upload_creates_a_queued_run_and_stores_the_input(client, session, probe):
    login(client)
    response = upload(client, b"file body", "standard.pdf")
    assert response.status_code == 202

    run = session.get(Run, response.json()["id"])
    assert run.status == "queued"
    assert run.title == "standard.pdf"

    record = session.query(RunFile).filter(RunFile.run_id == run.id).one()
    assert record.kind == "input"
    # Prefixed with the silo's own location, which defaults to the silo id.
    assert record.storage_key == f"probe/runs/{run.id}/input/standard.pdf"
    assert get_object_store().open(record.storage_key).read() == b"file body"


def test_upload_rejects_an_extension_the_silo_does_not_accept(client, probe):
    login(client)
    response = upload(client, b"x", "notes.docx")
    assert response.status_code == 415
    assert ".txt" in response.json()["detail"]


def test_upload_rejects_an_unknown_silo(client):
    login(client)
    response = client.post(
        "/api/silos/nope/documents", files={"file": ("a.txt", b"x", "text/plain")}
    )
    assert response.status_code == 404


def test_oversized_upload_is_rejected_and_leaves_no_orphan_run(client, session, probe):
    login(client)
    # MAX_UPLOAD_MB is 1 in tests.
    response = upload(client, b"x" * (2 * 1024 * 1024))
    assert response.status_code == 413
    # The history must not gain a row for an upload that was refused.
    assert session.query(Run).count() == 0


def test_upload_requires_authentication(client, probe):
    assert upload(client).status_code == 401


# ── the pipeline ──────────────────────────────────────────────────────────────


def test_run_pauses_for_the_user_and_frees_the_worker(client, session, probe):
    run = queued_run(client, session)

    claimed = queue.claim(session, "worker-1")
    assert claimed.id == run.id

    status = runner.run_one(session, claimed, probe)

    assert status == "awaiting_user"
    assert probe.module.CALLS == ["prepare", "await_input"]
    session.refresh(claimed)
    assert claimed.status == "awaiting_user"
    assert claimed.stage == "await_input"
    assert claimed.progress_message == "Waiting for the reviewer"
    # The claim is released, so the worker is free for other work rather than
    # blocking on a person.
    assert claimed.claimed_by is None
    # No checkpoint for the paused stage: the user's answer completes it.
    assert queue.completed_stages(session, claimed.id) == {"prepare"}


def test_user_answer_resumes_at_the_next_stage(client, session, probe):
    run = queued_run(client, session)
    claimed = queue.claim(session, "worker-1")
    runner.run_one(session, claimed, probe)

    response = client.post(
        f"/api/documents/{run.id}/build", json={"choice": "option-b"}
    )
    assert response.status_code == 202
    session.refresh(claimed)
    assert claimed.status == "queued"

    probe.module.CALLS.clear()
    resumed = queue.claim(session, "worker-2")
    status = runner.run_one(session, resumed, probe)

    assert status == "complete"
    # prepare and await_input are both checkpointed, so only finish runs.
    assert probe.module.CALLS == ["finish"]

    output = (
        session.query(RunFile)
        .filter(RunFile.run_id == run.id, RunFile.kind == "output")
        .one()
    )
    body = get_object_store().open(output.storage_key).read().decode()
    # Proves the user's answer reached the later stage via the checkpoint.
    assert "choice=option-b" in body
    assert "input=source.txt" in body


def test_completed_run_is_finalised(client, session, probe):
    run = queued_run(client, session)
    claimed = queue.claim(session, "worker-1")
    runner.run_one(session, claimed, probe)
    client.post(f"/api/documents/{run.id}/build", json={"choice": "x"})
    runner.run_one(session, queue.claim(session, "worker-1"), probe)

    session.refresh(claimed)
    assert claimed.status == "complete"
    assert claimed.progress_pct == 100
    assert claimed.finished_at is not None
    assert claimed.duration_ms is not None
    assert claimed.claimed_by is None


def test_failure_keeps_checkpoints_so_retry_resumes(client, session, probe):
    run = queued_run(client, session)
    probe.module.FAIL_IN.add("await_input")

    claimed = queue.claim(session, "worker-1")
    status = runner.run_one(session, claimed, probe)

    assert status == "failed"
    session.refresh(claimed)
    assert claimed.status == "failed"
    assert "await_input" in claimed.error_message
    # Traceback stored for operators, not returned to non-admins.
    assert "Traceback" in (claimed.meta or {}).get("traceback", "")
    # The earlier stage's work is preserved.
    assert queue.completed_stages(session, claimed.id) == {"prepare"}

    probe.module.FAIL_IN.clear()
    probe.module.CALLS.clear()

    assert client.post(f"/api/documents/{run.id}/retry").status_code == 202
    retried = queue.claim(session, "worker-1")
    assert runner.run_one(session, retried, probe) == "awaiting_user"
    # prepare was not repeated.
    assert probe.module.CALLS == ["await_input"]


def test_run_for_a_missing_silo_fails_loudly(client, session, probe):
    run = queued_run(client, session)
    run.silo_id = "was-removed"
    session.commit()

    claimed = queue.claim(session, "worker-1")
    assert runner.run_one(session, claimed) == "failed"
    session.refresh(claimed)
    assert "No silo registered" in claimed.error_message


def test_progress_updates_are_visible_to_the_status_poll(client, session, probe):
    run = queued_run(client, session)
    runner.run_one(session, queue.claim(session, "worker-1"), probe)

    payload = client.get(f"/api/documents/{run.id}/status").json()
    assert payload["status"] == "awaiting_user"
    assert payload["stage"] == "await_input"
    assert payload["progress_pct"] == 30
    assert payload["progress_message"] == "Waiting for the reviewer"


# ── claiming and recovery ─────────────────────────────────────────────────────


def test_two_workers_never_claim_the_same_run(client, session, probe):
    login(client)
    for index in range(4):
        assert upload(client, b"x", f"file{index}.txt").status_code == 202

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        from api.backend.da_platform.db.session import SessionLocal

        own_session = SessionLocal()
        try:
            while True:
                run = queue.claim(own_session, name)
                if run is None:
                    return
                with lock:
                    claimed.append(run.id)
        finally:
            own_session.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Every run claimed exactly once. SQLite serialises writers, so this proves the
    # claim logic rather than contention; Postgres covers that in phase 6.
    assert sorted(claimed) == sorted({run_id for run_id in claimed})
    assert len(claimed) == 4


def test_reaper_requeues_a_run_whose_worker_stopped_heartbeating(
    client, session, probe
):
    run = queued_run(client, session)
    claimed = queue.claim(session, "doomed-worker")
    assert claimed.status == "running"

    # Simulate the worker dying: the claim stands but the heartbeat goes stale.
    claimed.heartbeat_at = utcnow() - timedelta(hours=2)
    session.commit()

    requeued = queue.requeue_stale(session)

    assert requeued == [run.id]
    session.refresh(claimed)
    assert claimed.status == "queued"
    assert claimed.claimed_by is None
    # Another worker can now pick it up.
    assert queue.claim(session, "healthy-worker").id == run.id


def test_reaper_leaves_a_healthy_run_alone(client, session, probe):
    queued_run(client, session)
    claimed = queue.claim(session, "busy-worker")
    queue.heartbeat(session, claimed)

    assert queue.requeue_stale(session) == []
    session.refresh(claimed)
    assert claimed.status == "running"


# ── authorisation on run control ──────────────────────────────────────────────


def test_non_owner_cannot_resume_someone_elses_run(client, session, probe):
    run = queued_run(client, session)
    runner.run_one(session, queue.claim(session, "worker-1"), probe)

    login(client, "ben.carter")
    response = client.post(f"/api/documents/{run.id}/build", json={})
    assert response.status_code == 403
    assert response.json()["detail"] == {"can_fork": True}


def test_build_on_a_run_that_is_not_paused_is_a_conflict(client, session, probe):
    run = queued_run(client, session)
    response = client.post(f"/api/documents/{run.id}/build", json={})
    assert response.status_code == 409


def test_retry_on_a_run_that_did_not_fail_is_a_conflict(client, session, probe):
    run = queued_run(client, session)
    assert client.post(f"/api/documents/{run.id}/retry").status_code == 409


@pytest.mark.parametrize("route", ["build", "retry"])
def test_run_control_requires_authentication(client, session, probe, route):
    run = queued_run(client, session)
    client.cookies.clear()
    assert client.post(f"/api/documents/{run.id}/{route}").status_code == 401


def test_stage_context_offers_textract_and_allows_injection(client, session, probe):
    """ISO needs per-page Textract analysis, and a silo may not build its own client.

    The isolation test fails the build for that, and the credential refresh plus the
    per-page concurrency ceiling belong in one place, so the capability arrives through
    the stage context like every other one.
    """
    from api.backend.da_platform.engine.context import StageContext

    run = queued_run(client, session)
    assert callable(StageContext(session, run).textract)

    recorded: list[bytes] = []
    injected = StageContext(session, run, textract=recorded.append)
    injected.textract(b"\x89PNG-ish")
    assert recorded == [b"\x89PNG-ish"]


def test_stage_context_does_not_build_an_aws_client_until_textract_is_used(
    client, session, probe, monkeypatch
):
    """Constructing a context must not require AWS credentials: BOP never calls
    Textract, and every run builds a context."""
    from api.backend.da_platform.engine import context as context_module

    def explode():
        raise AssertionError("an AWS client was built during construction")

    monkeypatch.setattr(context_module, "get_clients", explode)
    run = queued_run(client, session)
    assert context_module.StageContext(session, run) is not None
