"""PSA end to end: fetch -> report, through the real stages and the real runner.

No database of its own and no network. The Smartsheet read is the single seam that is faked —
`ingest_smartsheet_api.capture` and the psa.db rebuild — so the two stages run against genuine
`StageContext`, object store, checkpoint table and queue, driven by `runner.run_one` exactly as
a worker would.

That is the difference between this file and `tests/test_psa_silo.py`. That one stubs every
delegate to prove `silo.py` wires them in the right order. This one lets the platform half be
real, to prove a run actually gets claimed, advances stage by stage, checkpoints what it
produced, and reaches "complete" with the document attached — including the resume path, where
a second worker picks up a run whose `fetch` is already done and must not read Smartsheet again.

`engines.generate_report` is stubbed rather than run: it builds a real .docx from a real psa.db,
which needs the shipped template and a populated catalogue, and it is covered directly in
`tests/test_psa_workflow.py` and `tests/test_psa_render.py`. Letting it run here would mean a
failure could be the report builder's rather than the pipeline's.
"""

from __future__ import annotations

import json

import pytest

from api.backend.da_platform.engine import queue, runner
from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _build, _load_module
from tests.conftest import login, make_run

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)

SHEET = {
    "name": "NBE (Vials)",
    "columns": [{"id": 1, "title": "Program"}],
    "rows": [{"rowNumber": 20, "cells": []}],
}


@pytest.fixture(scope="module")
def silo_module():
    return _load_module("psa", SILO_DIR / "silo.py")


@pytest.fixture
def psa(silo_module, monkeypatch, tmp_path):
    """The real silo, with only the Smartsheet read and the psa.db rebuild faked.

    `capture` is patched rather than `snapshot.capture_bytes`, so the real canonicalising
    JSON dump runs and the bytes the run stores are the bytes the real code would store.
    """
    from da_silos.psa import aria, engines, ingest_smartsheet_api, snapshot

    document = tmp_path / "psa_assessment.docx"
    document.write_bytes(b"PK\x03\x04 pretend docx")

    state = {"captures": 0, "rebuilds": 0, "reports": 0, "photo": None,
             "document": document}

    def capture(sheet_id=None):
        state["captures"] += 1
        return SHEET, "SHEET-1"

    def rebuild(raw, skip_images=True):
        state["rebuilds"] += 1
        doc = json.loads(raw.decode("utf-8"))
        return {"products": len(doc["sheet"]["rows"])}

    def generate_report(program, source_row, refresh_first=True, photo=None):
        state["reports"] += 1
        state["photo"] = photo
        return {"status": "ok", "verify_ok": True, "output_exists": True,
                "output_path": str(state["document"]), "run_log": ["built"]}

    monkeypatch.setattr(aria, "install", lambda **kw: None)
    monkeypatch.setattr(ingest_smartsheet_api, "capture", capture)
    monkeypatch.setattr(snapshot, "rebuild", rebuild)
    monkeypatch.setattr(engines, "generate_report", generate_report)
    monkeypatch.setattr(
        ingest_smartsheet_api, "capture_row_image", lambda sheet, row: None
    )
    return {"silo": silo_module, "state": state,
            "ingest_api": ingest_smartsheet_api, "engines": engines}


@pytest.fixture
def run(session, client):
    """A queued PSA run with no input file, as `runs.py` creates one."""
    from api.backend.da_platform.db.models import RunFile

    asha = login(client)
    record = make_run(session, owner_id=asha["id"], title="PSA AGN-151586", silo="psa",
                      status="queued")
    session.query(RunFile).filter(RunFile.run_id == record.id).delete()
    record.meta = {"program": "AGN-151586", "source_row": 20, "vendor": None}
    record.stage = None
    session.commit()
    session.refresh(record)
    return record


def _context(session, run, silo):
    """The same context the runner builds, for a test that needs to run one stage by hand."""
    from api.backend.da_platform.engine.context import StageContext
    from api.backend.da_platform.storage import get_object_store

    return StageContext(session, run, store=get_object_store(),
                        storage_prefix=silo.STORAGE_PREFIX,
                        storage_folders=silo.STORAGE_FOLDERS)


def drive(session, psa, run) -> str:
    """Claim and run the run the way `worker/main.py` does.

    One `run_one` is enough: it advances a claimed run as far as it will go, and PSA never
    parks — `STAGES` is `fetch, report` with no `await_*` between them.
    """
    claimed = queue.claim(session, "worker-1")
    assert claimed is not None and claimed.id == run.id, "the run was not queued"
    return runner.run_one(session, claimed, _build("psa", psa["silo"]))


# ── the whole job ─────────────────────────────────────────────────────────────


def test_a_queued_run_is_claimed_and_completes(session, psa, run):
    assert drive(session, psa, run) == "complete"

    session.refresh(run)
    assert run.status == "complete"


def test_both_stages_leave_a_checkpoint(session, psa, run):
    """A checkpoint is the contract between stages and the only thing a resume on another
    worker has, so a stage that completes without one silently breaks the resume."""
    drive(session, psa, run)

    assert queue.checkpoint_output(session, run.id, "fetch") is not None
    assert queue.checkpoint_output(session, run.id, "report") is not None


def test_the_sheet_is_read_exactly_once(session, psa, run):
    """The capture-once guarantee, end to end. A second read could return a different sheet
    than the assessment was based on."""
    drive(session, psa, run)

    assert psa["state"]["captures"] == 1


def test_the_catalogue_is_rebuilt_from_the_snapshot(session, psa, run):
    drive(session, psa, run)

    assert psa["state"]["rebuilds"] >= 1


def test_the_document_is_attached_as_the_runs_output(session, psa, run):
    drive(session, psa, run)

    session.refresh(run)
    outputs = [f for f in run.files if f.kind == "output"]
    assert [f.filename for f in outputs] == ["psa_assessment.docx"]


def test_the_attached_document_carries_the_bytes_that_were_generated(session, psa, run):
    """An attachment whose size is zero would download as a corrupt file, and nothing else
    in the run would say so."""
    drive(session, psa, run)

    session.refresh(run)
    output = next(f for f in run.files if f.kind == "output")
    assert output.size_bytes == len(b"PK\x03\x04 pretend docx")


def test_the_attached_document_is_typed_as_a_word_file(session, psa, run):
    """The browser decides how to handle the download from this, so a wrong type turns a
    report into a file the user has to rename by hand."""
    drive(session, psa, run)

    session.refresh(run)
    output = next(f for f in run.files if f.kind == "output")
    assert output.content_type == psa["silo"].DOCX_MIME


def test_the_run_never_acquires_an_input_file(session, psa, run):
    """PSA accepts no upload. An input appearing would mean something bypassed `/runs`."""
    drive(session, psa, run)

    session.refresh(run)
    assert [f for f in run.files if f.kind == "input"] == []


def test_the_snapshot_is_stored_as_run_media(session, psa, run):
    """The snapshot is the audit record of what Smartsheet said when the assessment was
    made — which is what a GxP reviewer would ask for."""
    drive(session, psa, run)

    fetched = queue.checkpoint_output(session, run.id, "fetch")
    assert fetched["snapshot_key"].endswith("smartsheet.json")


def test_the_stored_snapshot_is_the_canonical_json(session, psa, run):
    """`sort_keys=True` is what makes "did the catalogue change?" a byte comparison, so the
    stored bytes must be the canonicalised form rather than whatever order the API gave."""
    from api.backend.da_platform.engine.context import StageContext
    from api.backend.da_platform.storage import get_object_store

    drive(session, psa, run)
    fetched = queue.checkpoint_output(session, run.id, "fetch")
    ctx = StageContext(session, run, store=get_object_store(),
                       storage_prefix=psa["silo"].STORAGE_PREFIX,
                       storage_folders=psa["silo"].STORAGE_FOLDERS)

    with ctx.storage.open_key(fetched["snapshot_key"]) as handle:
        raw = handle.read()

    assert raw == json.dumps({"sheet_id": "SHEET-1", "sheet": SHEET},
                             sort_keys=True, ensure_ascii=False).encode("utf-8")


def test_the_report_reads_the_program_from_the_run(session, psa, run):
    drive(session, psa, run)

    result = queue.checkpoint_output(session, run.id, "report")
    assert result["status"] == "ok"
    assert result["verify_ok"] is True


def test_the_progress_reaches_one_hundred(session, psa, run):
    """A completed run that still reads 80% is the bug the PCT ladder exists to prevent."""
    drive(session, psa, run)

    session.refresh(run)
    assert run.progress_pct == 100


def test_a_finished_run_holds_no_stage_and_no_claim(session, psa, run):
    """`queue.finish` clears both on purpose: a completed run is not "in" a stage, and a
    lingering `claimed_by` would make the reaper treat a finished run as a stalled one."""
    drive(session, psa, run)

    session.refresh(run)
    assert run.stage is None
    assert run.claimed_by is None
    assert run.finished_at is not None


# ── the resume path ───────────────────────────────────────────────────────────


def test_a_resumed_run_does_not_read_smartsheet_again(session, psa, run, monkeypatch):
    """The reason the snapshot exists. A worker that dies after `fetch` must be resumable
    without a second sheet read, or the rebuild could describe a different catalogue than
    the one the run captured.

    `run_one` advances a run as far as it will go in one call, so the crash is staged by
    making `report` raise the first time — which is what a worker being killed mid-stage
    looks like to the checkpoint table.
    """
    silo = _build("psa", psa["silo"])

    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("worker died")),
    )
    claimed = queue.claim(session, "worker-1")
    assert runner.run_one(session, claimed, silo) == "failed"
    assert psa["state"]["captures"] == 1

    # The report builder recovers and a different worker retries the run.
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda *a, **kw: {"status": "ok", "verify_ok": True, "output_exists": True,
                          "output_path": str(psa["state"]["document"])},
    )
    run.status = "queued"
    session.commit()

    second = queue.claim(session, "worker-2")
    assert second.id == run.id
    assert runner.run_one(session, second, silo) == "complete"

    assert psa["state"]["captures"] == 1, "the stored snapshot was reused"


def test_a_run_whose_fetch_is_checkpointed_skips_straight_to_the_report(session, psa, run):
    """Only the outstanding stage runs. Re-running `fetch` would both waste a sheet read and
    overwrite the audit snapshot the run is supposed to preserve."""
    silo = _build("psa", psa["silo"])
    fetched = psa["silo"].fetch(run, _context(session, run, psa["silo"]))
    queue.save_checkpoint(session, run.id, "fetch", fetched)
    captures_after_fetch = psa["state"]["captures"]

    claimed = queue.claim(session, "worker-1")
    assert runner.run_one(session, claimed, silo) == "complete"

    assert psa["state"]["captures"] == captures_after_fetch
    assert psa["state"]["reports"] == 1


# ── failure ───────────────────────────────────────────────────────────────────


def test_a_failing_report_marks_the_run_failed_rather_than_complete(session, psa, run,
                                                                    monkeypatch):
    """The run has to end in a state the screen can explain. "complete" on a run with no
    document is the worst outcome, because nothing invites a retry."""
    def boom(*args, **kwargs):
        raise RuntimeError("the template is missing")

    monkeypatch.setattr(psa["engines"], "generate_report", boom)

    drive(session, psa, run)

    session.refresh(run)
    assert run.status == "failed"


def test_a_failed_report_attaches_no_document(session, psa, run, monkeypatch):
    """A half-written output would be offered for download as if it were the assessment."""
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    drive(session, psa, run)

    session.refresh(run)
    assert [f for f in run.files if f.kind == "output"] == []


def test_the_fetch_checkpoint_survives_a_failed_report(session, psa, run, monkeypatch):
    """So a retry resumes from the snapshot rather than reading Smartsheet again — which is
    what `POST /documents/{id}/retry` depends on."""
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    drive(session, psa, run)

    assert queue.checkpoint_output(session, run.id, "fetch") is not None


def test_a_smartsheet_outage_fails_only_this_run(session, psa, run, monkeypatch):
    """`SystemExit` from the ingest CLI would otherwise unwind the worker and take every
    other silo's queued runs with it. `run_one` returning normally is the proof it was
    converted to something `except Exception` can catch."""
    def boom(sheet_id=None):
        raise SystemExit("Smartsheet API: 401 Unauthorized")

    monkeypatch.setattr(psa["ingest_api"], "capture", boom)
    silo = _build("psa", psa["silo"])

    claimed = queue.claim(session, "worker-1")
    assert runner.run_one(session, claimed, silo) == "failed"

    session.refresh(run)
    assert run.status == "failed"


def test_a_run_with_no_program_fails_instead_of_assessing_nothing(session, psa, run):
    """The shape a file uploaded to the platform's generic endpoint produces."""
    run.meta = {}
    session.commit()

    drive(session, psa, run)

    session.refresh(run)
    assert run.status == "failed"
