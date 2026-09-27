"""ISO plumbing: the stage contract, the scratch workspace and the range picker.

Deliberately free of network and of heavy PDF work. This file covers the
platform-facing half of the ISO port, so it stays runnable on a laptop with no
credentials; the ported extraction logic is tested elsewhere.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from da_platform.engine import queue, runner
from da_platform.engine.context import StageContext
from da_platform.settings import REPO_ROOT, settings
from da_platform.silo_registry import _build, _load_module
from da_platform.storage import get_object_store
from tests.conftest import login, make_run

ISO_DIR = REPO_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "silo.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def iso():
    """Load the real ISO silo, registering its package so submodules import."""
    module = _load_module("iso", ISO_DIR / "silo.py")
    router_module = _load_module("iso", ISO_DIR / "router.py", name="router")
    from da_silos.iso import workspace

    return {"silo": module, "workspace": workspace, "router_module": router_module}


@pytest.fixture
def iso_client(iso) -> TestClient:
    """An app with auth, the document routes and ISO's router mounted.

    Built here rather than reusing the shared `client` fixture because conftest points
    SILOS_DIR at the probe fixture, so the real ISO router is never mounted on the
    shared app.
    """
    from da_platform.api import auth as auth_api
    from da_platform.api import documents as documents_api

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    app.include_router(documents_api.router, prefix="/api")
    app.include_router(iso["router_module"].router, prefix="/api/silos/iso")
    return TestClient(app)


def iso_run(
    session,
    owner_id: str,
    *,
    status: str = "running",
    stage: str = "ingest",
    body: bytes = b"%PDF-1.4\nnot a real pdf\n",
):
    run = make_run(session, owner_id=owner_id, title="SOP.pdf", silo="iso", status=status)
    run.stage = stage
    session.commit()
    record = next(item for item in run.files if item.kind == "input")
    get_object_store().put(record.storage_key, body)
    return run


def context_for(session, run, iso) -> StageContext:
    silo = iso["silo"]
    return StageContext(
        session,
        run,
        storage_prefix=silo.STORAGE_PREFIX,
        storage_folders=silo.STORAGE_FOLDERS,
    )


def parked_run(session, owner_id: str, *, entries: int = 3):
    """A run stopped at `await_range` with both earlier stages checkpointed."""
    run = iso_run(session, owner_id, status="awaiting_user", stage="await_range")
    queue.save_checkpoint(
        session,
        run.id,
        "ingest",
        {
            "use_vision": False,
            "llm_pages_key": None,
            "total_pages": 12,
            "source_name": "SOP.pdf",
        },
    )
    queue.save_checkpoint(
        session,
        run.id,
        "outline",
        {
            "toc": [{"title": f"Section {index}", "page": index} for index in range(entries)],
            "doc_title": "ISO 20417:2021 Medical devices",
            "total_pages": 12,
        },
    )
    return run


# ── the contract ──────────────────────────────────────────────────────────────


def test_silo_declares_the_expected_contract(iso):
    silo = iso["silo"]
    assert silo.LABEL
    assert silo.ACCEPTS == [".pdf"]
    assert silo.STAGES == ["ingest", "outline", "await_range", "build"]
    assert silo.STORAGE_PREFIX == "ISO_Doc_Generation"
    assert silo.STORAGE_FOLDERS == {
        "input": "uploads",
        "output": "downloads",
        "media": "memry",
    }
    for stage in silo.STAGES:
        assert callable(getattr(silo, stage)), stage


def test_the_platform_discovers_iso_with_its_stages(monkeypatch, iso):
    """Discovery against the real silos directory rather than the probe fixture."""
    from da_platform import silo_registry

    monkeypatch.setattr(
        silo_registry,
        "settings",
        dataclasses.replace(settings, silos_dir=REPO_ROOT / "silos", enabled_silos=[]),
    )
    found = {silo.id: silo for silo in silo_registry.discover_silos()}

    assert "iso" in found, "the ISO silo was skipped; check the registry log for why"
    discovered = found["iso"]
    assert discovered.stages == ["ingest", "outline", "await_range", "build"]
    assert discovered.accepts == [".pdf"]
    assert discovered.storage_prefix == "ISO_Doc_Generation"
    assert discovered.storage_folders["media"] == "memry"
    # router.py imported cleanly, so the range picker will actually be mounted.
    assert discovered.router is not None


# ── the workspace ─────────────────────────────────────────────────────────────


def test_workspace_materialises_the_pdf_and_cleans_up(client, session, iso):
    asha = login(client)
    run = iso_run(session, asha["id"], body=b"%PDF-1.4 body bytes")
    ctx = context_for(session, run, iso)

    with iso["workspace"].open_run(run, ctx) as ws:
        root = Path(ws.root)
        assert root.is_dir()
        # A real local path, which is the whole point: fitz cannot open a store key.
        assert Path(ws.pdf_path).read_bytes() == b"%PDF-1.4 body bytes"
        # The name comes from the input file record, which `conftest.make_run` always
        # calls source.pdf — not from the run title.
        assert Path(ws.pdf_path).name == "source.pdf"
        assert ws.source_name == "source.pdf"

    # Nothing durable is left on local disk (spec section 10).
    assert not root.exists()


def test_workspace_pdf_path_opens_in_pymupdf(client, session, iso):
    """The eleven `fitz.open(pdf_path)` call sites in the ported code, proved once."""
    import fitz

    document = fitz.open()
    document.new_page()
    document.new_page()
    pdf_bytes = document.tobytes()
    document.close()

    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes)
    ctx = context_for(session, run, iso)

    with iso["workspace"].open_run(run, ctx) as ws:
        opened = fitz.open(ws.pdf_path)
        try:
            assert opened.page_count == 2
        finally:
            opened.close()


def test_workspace_is_removed_even_when_the_stage_raises(client, session, iso):
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, iso)

    root = None
    with pytest.raises(RuntimeError, match="stage blew up"):
        with iso["workspace"].open_run(run, ctx) as ws:
            root = Path(ws.root)
            raise RuntimeError("stage blew up")

    assert root is not None and not root.exists()


def test_put_media_writes_the_local_copy_and_the_durable_one(client, session, iso):
    """`_write_media` (backend.py:2200-2213). The local copy is load-bearing because
    downstream ported code passes paths around and `_media_as_bytes` reads them back;
    the durable copy is what survives the worker being replaced."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, iso)

    with iso["workspace"].open_run(run, ctx) as ws:
        path = ws.put_media("fig_p12_1.png", b"\x89PNG-ish")
        assert Path(path).read_bytes() == b"\x89PNG-ish"
        assert Path(path).parent.name == "memry"
        assert ws.read_media("fig_p12_1.png") == b"\x89PNG-ish"

    key = f"ISO_Doc_Generation/memry/{run.id}_fig_p12_1.png"
    with get_object_store().open(key) as handle:
        assert handle.read() == b"\x89PNG-ish"


def test_put_media_survives_a_storage_failure(client, session, iso, monkeypatch):
    """The source swallowed its upload error with a printed warning. A lost durable
    copy must not fail a run whose local copy is fine."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, iso)

    def explode(*args, **kwargs):
        raise RuntimeError("bucket on fire")

    monkeypatch.setattr(ctx.storage, "put_media", explode)
    with iso["workspace"].open_run(run, ctx) as ws:
        assert Path(ws.put_media("t.png", b"x")).read_bytes() == b"x"


def test_progress_from_a_long_loop_heartbeats_without_moving_the_bar(client, session, iso):
    """The reaper re-queues a run whose heartbeat is older than 900s. The two silent
    ported paths — the 60s RAG indexing wait (backend.py:206) and the 8-worker page
    fan-out (727-760) — report through this callback, which is what keeps a live run
    out of the reaper's hands."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, iso)
    ctx.progress("Generating the document", pct=35)
    run.heartbeat_at = None
    session.commit()

    with iso["workspace"].open_run(run, ctx) as ws:
        ws.progress("Extracting page 3 of 200")

    session.refresh(run)
    assert run.heartbeat_at is not None
    assert run.progress_message == "Extracting page 3 of 200"
    # The stage owns the percentage; an inner loop must not move it.
    assert run.progress_pct == 35
    assert iso["workspace"].no_progress("anything") is None


# ── the range picker ──────────────────────────────────────────────────────────


def test_toc_endpoint_returns_the_outline(iso_client, session, iso):
    asha = login(iso_client)
    run = parked_run(session, asha["id"])

    payload = iso_client.get(f"/api/silos/iso/documents/{run.id}/toc").json()
    assert payload["doc_title"] == "ISO 20417:2021 Medical devices"
    assert payload["total_pages"] == 12
    assert len(payload["toc"]) == 3


def test_toc_endpoint_is_a_conflict_before_the_outline_exists(iso_client, session, iso):
    asha = login(iso_client)
    run = iso_run(session, asha["id"])
    assert iso_client.get(f"/api/silos/iso/documents/{run.id}/toc").status_code == 409


def test_toc_endpoint_ignores_another_silos_run(iso_client, session, iso):
    """A BOP run must not be steerable through ISO's endpoints."""
    asha = login(iso_client)
    run = make_run(session, owner_id=asha["id"], title="A BOP doc", silo="bop")
    assert iso_client.get(f"/api/silos/iso/documents/{run.id}/toc").status_code == 404


@pytest.mark.parametrize("start_idx,end_idx", [(2, 1), (-1, 2), (0, 3), (0, 99)])
def test_range_endpoint_rejects_an_invalid_range(
    iso_client, session, iso, start_idx, end_idx
):
    """backend.py:5541-5543, message included."""
    asha = login(iso_client)
    run = parked_run(session, asha["id"], entries=3)

    response = iso_client.post(
        f"/api/silos/iso/documents/{run.id}/toc-range",
        json={"start_idx": start_idx, "end_idx": end_idx},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid section range selected."
    session.refresh(run)
    assert run.status == "awaiting_user"


def test_range_endpoint_reports_a_missing_outline_the_way_iso_did(
    iso_client, session, iso
):
    asha = login(iso_client)
    run = iso_run(session, asha["id"], status="awaiting_user", stage="await_range")

    response = iso_client.post(
        f"/api/silos/iso/documents/{run.id}/toc-range",
        json={"start_idx": 0, "end_idx": 0},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Session not found — please re-upload the PDF."


def test_range_endpoint_refuses_a_non_owner(iso_client, session, iso):
    asha = login(iso_client)
    run = parked_run(session, asha["id"])

    login(iso_client, "ben.carter")
    response = iso_client.post(
        f"/api/silos/iso/documents/{run.id}/toc-range",
        json={"start_idx": 0, "end_idx": 1},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {"can_fork": True}


def test_range_endpoint_refuses_a_run_that_is_not_parked(iso_client, session, iso):
    asha = login(iso_client)
    run = parked_run(session, asha["id"])
    run.status = "running"
    session.commit()

    response = iso_client.post(
        f"/api/silos/iso/documents/{run.id}/toc-range",
        json={"start_idx": 0, "end_idx": 1},
    )
    assert response.status_code == 409


def test_range_endpoint_requires_authentication(iso_client, session, iso):
    asha = login(iso_client)
    run = parked_run(session, asha["id"])
    iso_client.cookies.clear()
    assert (
        iso_client.post(
            f"/api/silos/iso/documents/{run.id}/toc-range",
            json={"start_idx": 0, "end_idx": 1},
        ).status_code
        == 401
    )


def test_router_stage_names_match_the_silo(iso):
    """A rename in silo.py must not silently strand every parked run."""
    module = iso["router_module"]
    assert module.RANGE_STAGE in iso["silo"].STAGES
    assert module.OUTLINE_STAGE in iso["silo"].STAGES


def test_the_chosen_range_resumes_the_run_and_skips_the_paused_stage(
    iso_client, session, iso, monkeypatch
):
    """The pause that used to be a thread waiting on an event, end to end: park,
    answer, re-queue, then start at `build` with the answer in hand."""
    asha = login(iso_client)
    run = parked_run(session, asha["id"], entries=3)

    response = iso_client.post(
        f"/api/silos/iso/documents/{run.id}/toc-range",
        json={"start_idx": 0, "end_idx": 2},
    )
    assert response.status_code == 202
    session.refresh(run)
    assert run.status == "queued"
    assert queue.checkpoint_output(session, run.id, "await_range") == {
        "start_idx": 0,
        "end_idx": 2,
    }

    calls: list[str] = []

    def fake_build(run_arg, ctx):
        calls.append("build")
        return {"bytes": 4321, "range": ctx.checkpoint("await_range")}

    # The real build wants the LLM and Textract; the stage plumbing is what is under
    # test here.
    monkeypatch.setattr(iso["silo"], "build", fake_build)
    silo = _build("iso", iso["silo"])

    claimed = queue.claim(session, "worker-2")
    assert claimed.id == run.id
    assert runner.run_one(session, claimed, silo) == "complete"

    # ingest, outline and await_range are all checkpointed, so only build runs.
    assert calls == ["build"]
    assert queue.checkpoint_output(session, run.id, "build")["range"] == {
        "start_idx": 0,
        "end_idx": 2,
    }
    session.refresh(run)
    assert run.status == "complete"
    assert run.progress_pct == 100
