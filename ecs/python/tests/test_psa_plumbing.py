"""PSA plumbing: the stage contract, program-driven run creation, and the run result.

This silo has no uploaded document — a run starts from a program code and a Smartsheet row
chosen in the UI, so creation is owned by the silo's own router rather than the platform's
upload endpoint. The parameters live on the run's `meta` column, which the `fetch` stage reads.

No Smartsheet and no network: nothing here calls a stage, so the only PSA modules that load
are `silo.py`, `router.py` and `runs.py`. That is deliberate — this file covers the
platform-facing half of the port, and `tests/test_psa_silo.py` covers what the stages do.

`ACCEPTS = []` is the interesting part of the contract. The registry reads an empty list as
"no upload RESTRICTION", not "no upload" (`silo_registry.py:68-72`), so a file POSTed to
`/silos/psa/documents` WOULD create a run — which is why `_params()` fails such a run loudly
instead of half-running it, and why that failure is pinned here.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.backend.da_platform.engine import queue
from api.backend.da_platform.settings import BACKEND_ROOT, settings
from api.backend.da_platform.silo_registry import _build, _load_module
from tests.conftest import login, make_run

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    module = _load_module("psa", SILO_DIR / "silo.py")
    router_module = _load_module("psa", SILO_DIR / "router.py", name="router")
    runs_module = _load_module("psa", SILO_DIR / "runs.py", name="runs")
    return {"silo": module, "router_module": router_module, "runs_module": runs_module}


@pytest.fixture
def api(psa) -> TestClient:
    """An app with auth, the document routes and this silo's router.

    Built here because conftest points SILOS_DIR at the probe fixture, so the shared
    client never mounts a real silo's router.

    **`uploads` is included, in the same order `app.py` uses, and that matters.** It owns
    `POST /api/silos/{silo_id}/documents` for multipart uploads, and FastAPI matches routes
    in registration order — so a silo route registered later at a path that the wildcard also
    matches is unreachable. PSA's whole reason for creating runs at `/runs` is that
    `/documents` is unreachable, so a fixture that left `uploads` out would prove nothing.
    """
    from api.backend.da_platform.routers import auth as auth_api
    from api.backend.da_platform.routers import documents as documents_api
    from api.backend.da_platform.routers import uploads as uploads_api

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    app.include_router(uploads_api.router, prefix="/api")
    app.include_router(documents_api.router, prefix="/api")
    app.include_router(psa["router_module"].router, prefix="/api/silos/psa")
    return TestClient(app)


def create(api: TestClient, **body) -> object:
    payload = {"program": "AGN-151586", "source_row": 20, **body}
    return api.post("/api/silos/psa/runs", json=payload)


# ── the contract ──────────────────────────────────────────────────────────────


def test_the_silo_declares_no_accepted_upload(psa):
    """`ACCEPTS = []` is the honest declaration: there is nothing to upload. The registry
    treats an empty list as "no restriction", so the platform's upload endpoint would
    accept anything — which is why creation is owned by this silo's router instead."""
    silo = psa["silo"]

    assert silo.ACCEPTS == []
    assert silo.STAGES == ["fetch", "report"]
    assert silo.LABEL == "Product Similarity Assessment"
    for stage in silo.STAGES:
        assert callable(getattr(silo, stage)), stage


def test_the_storage_location_is_re_exported_for_the_registry(psa):
    """`silo.py` re-exports these two names from `location.py` because the registry reads
    them off the contract module, not off a submodule it does not know about."""
    silo = psa["silo"]

    assert silo.STORAGE_PREFIX == "psa"
    assert silo.STORAGE_FOLDERS == {"output": "reports", "media": "images"}


def test_there_is_no_input_folder_because_nothing_is_uploaded(psa):
    """A silo that accepts no upload has nothing to put in an `input` folder, and a stray
    key there would be a sign something bypassed `/runs`."""
    assert "input" not in psa["silo"].STORAGE_FOLDERS


def test_the_platform_discovers_the_silo(monkeypatch, psa):
    from api.backend.da_platform import silo_registry

    monkeypatch.setattr(
        silo_registry,
        "settings",
        dataclasses.replace(settings, silos_dir=BACKEND_ROOT / "silos", enabled_silos=[]),
    )
    found = {silo.id: silo for silo in silo_registry.discover_silos()}

    assert "psa" in found, "the silo was skipped; check the registry log"
    assert found["psa"].stages == ["fetch", "report"]
    assert found["psa"].router is not None


def test_discovery_does_not_disturb_the_other_silos(monkeypatch, psa):
    """PSA is additive. If adding it changed what the registry says about bop/iso/mfg_atr,
    the folder-scan contract would be broken rather than extended."""
    from api.backend.da_platform import silo_registry

    monkeypatch.setattr(
        silo_registry,
        "settings",
        dataclasses.replace(settings, silos_dir=BACKEND_ROOT / "silos", enabled_silos=[]),
    )
    found = {silo.id: silo for silo in silo_registry.discover_silos()}

    assert found["mfg_atr"].stages == ["fetch", "generate", "await_edits", "finalize"]
    assert found["iso"].stages == ["ingest", "outline", "await_range", "build"]
    assert found["bop"].accepts == [".pdf", ".docx"]


def test_the_silo_builds_into_a_real_platform_object(psa):
    """`_build` is what the engine actually runs: it validates the contract and resolves
    every stage name to a callable, so a typo in STAGES fails here rather than mid-run."""
    silo = _build("psa", psa["silo"])

    assert silo.id == "psa"
    assert silo.stages == ["fetch", "report"]
    for stage in silo.stages:
        assert callable(silo.stage_callable(stage)), stage


def test_the_progress_ladder_is_one_monotonic_sequence(psa):
    """Found by executing a run: each stage used to count to 100 on its own, so a job with
    the report still to come reported itself complete. The ladder is the fix, and a new
    stage inserted out of order would silently undo it."""
    pct = psa["silo"].PCT

    ordered = [
        pct["fetch_start"], pct["fetch_done"],
        pct["report_start"], pct["report_generating"], pct["report_done"],
    ]
    assert ordered == sorted(ordered), pct
    assert ordered[-1] == 100, "the last step must actually reach complete"
    assert len(set(ordered)) == len(ordered), "a repeated value stalls the bar"


def test_the_media_names_are_stable(psa):
    """These name objects in the store. Renaming one orphans every run already made."""
    silo = psa["silo"]

    assert silo.REPORT_MEDIA == "psa_assessment.docx"
    assert silo.PHOTO_MEDIA_PREFIX == "product_photo"
    assert silo.DOCX_MIME == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_the_stage_constants_in_runs_name_real_stages(psa):
    """`runs.py` names its stage as a constant rather than importing `silo.py`, so that it
    never drags the pipeline into the API process. The cost is that the two can drift, and
    a `REPORT_STAGE` that named nothing would make `/runs/{id}/result` silently return an
    empty checkpoint forever. This is the check that stops that."""
    runs_module = psa["runs_module"]
    stages = psa["silo"].STAGES

    assert runs_module.REPORT_STAGE in stages
    assert runs_module.SILO_ID == "psa"


# ── run creation from a program code ──────────────────────────────────────────


def test_creating_a_run_needs_no_file(api, session, psa):
    asha = login(api)

    response = create(api)

    assert response.status_code == 202, response.text
    run_id = response.json()["id"]

    from api.backend.da_platform.db.models import Run

    run = session.get(Run, run_id)
    assert run is not None
    assert run.silo_id == "psa"
    assert run.user_id == asha["id"]
    assert run.status == "queued"
    assert run.files == [], "there is no input document"


def test_the_parameters_land_on_the_run_metadata(api, session, psa):
    """`fetch` reads these; the `meta` JSON column exists for exactly this."""
    login(api)

    run_id = create(api, program="AGN-151586", source_row=20).json()["id"]

    from api.backend.da_platform.db.models import Run

    meta = session.get(Run, run_id).meta
    assert meta["program"] == "AGN-151586"
    assert meta["source_row"] == 20


def test_the_run_starts_unclaimed_and_before_the_first_stage(api, session, psa):
    """`stage=None` is what tells the runner to begin at the first stage rather than
    resume, and the message is what the history list shows until a worker picks it up."""
    login(api)
    run_id = create(api).json()["id"]

    from api.backend.da_platform.db.models import Run

    run = session.get(Run, run_id)
    assert run.stage is None
    assert run.progress_pct == 0
    assert run.progress_message == "Waiting for a worker"


def test_no_vendor_is_recorded_as_none_rather_than_a_default(api, session, psa):
    """None means EVERY supplier. A default here would silently restrict the whole job to
    one catalogue — the first real execution recorded `vendors_considered: ['Datwyler']`
    while the message it displayed said "across every supplier". At assessment time the cap
    is often not yet tooled, so "no supplier named" means the supplier is still open."""
    login(api)
    run_id = create(api).json()["id"]

    from api.backend.da_platform.db.models import Run

    assert session.get(Run, run_id).meta["vendor"] is None


def test_a_named_vendor_is_recorded_for_the_audit_trail(api, session, psa):
    login(api)
    run_id = create(api, vendor="Datwyler").json()["id"]

    from api.backend.da_platform.db.models import Run

    assert session.get(Run, run_id).meta["vendor"] == "Datwyler"


def test_the_screen_supplies_the_title(api, session, psa):
    """Only the screen knows the readable labels: this endpoint never reads Smartsheet, so
    it cannot turn row 20 into '2R (2.00 mL) vial'."""
    login(api)
    run_id = create(api, title="AGN-151586 — 2R (2.00 mL) vial").json()["id"]

    from api.backend.da_platform.db.models import Run

    assert session.get(Run, run_id).title == "AGN-151586 — 2R (2.00 mL) vial"


def test_the_fallback_title_names_the_program_and_not_the_row(api, session, psa):
    """The row number is our internal key for a Smartsheet row and means nothing to
    whoever reads the document list."""
    login(api)
    run_id = create(api, source_row=20).json()["id"]

    from api.backend.da_platform.db.models import Run

    title = session.get(Run, run_id).title
    assert title == "PSA AGN-151586"
    assert "20" not in title


def test_a_string_source_row_is_accepted(api, session, psa):
    """The screen sends whatever Smartsheet gave it, and the row arrives as a string from
    a JSON form as readily as an int."""
    login(api)

    response = create(api, source_row="20")

    assert response.status_code == 202, response.text
    from api.backend.da_platform.db.models import Run

    assert session.get(Run, response.json()["id"]).meta["source_row"] == "20"


def test_a_blank_program_is_rejected(api, psa):
    login(api)

    response = create(api, program="   ")

    assert response.status_code == 400
    assert response.json()["detail"] == "A program code is required."


def test_the_program_is_trimmed_before_it_is_stored(api, session, psa):
    login(api)
    run_id = create(api, program="  AGN-151586  ").json()["id"]

    from api.backend.da_platform.db.models import Run

    assert session.get(Run, run_id).meta["program"] == "AGN-151586"


def test_a_missing_program_is_a_validation_error(api, psa):
    login(api)

    response = api.post("/api/silos/psa/runs", json={"source_row": 20})

    assert response.status_code == 422


def test_a_missing_source_row_is_a_validation_error(api, psa):
    """`source_row` has no default because it is the key that survives a database rebuild;
    guessing one would assess the wrong presentation."""
    login(api)

    response = api.post("/api/silos/psa/runs", json={"program": "AGN-151586"})

    assert response.status_code == 422


def test_no_run_is_created_when_the_program_is_blank(api, session, psa):
    """A rejected request must leave nothing in the history."""
    login(api)
    from api.backend.da_platform.db.models import Run

    before = session.query(Run).count()
    create(api, program="")
    session.expire_all()

    assert session.query(Run).count() == before


def test_creation_requires_authentication(api, psa):
    api.cookies.clear()
    assert create(api).status_code == 401


def test_the_create_path_is_not_shadowed_by_the_platforms_upload_route(api, psa):
    """Creation lives at `/runs` because `/documents` is unreachable here.

    `routers/uploads.py` owns `POST /api/silos/{silo_id}/documents` for multipart uploads,
    and FastAPI matches in registration order with core routers first. A silo route at
    `/documents` is therefore never reached: the wildcard answers first and rejects a JSON
    body. This pins the symptom, because the failure mode is a confusing validation error
    rather than a 404.
    """
    login(api)

    shadowed = api.post(
        "/api/silos/psa/documents", json={"program": "AGN-151586", "source_row": 20}
    )
    assert shadowed.status_code == 422, "still the upload route, so still not ours"

    assert create(api).status_code == 202, "and /runs is ours"


def test_a_run_with_no_program_fails_loudly_rather_than_half_running(psa):
    """The case an uploaded file produces. `ACCEPTS = []` means "no upload RESTRICTION",
    so a file POSTed to the platform's generic endpoint WOULD create a PSA run — one with
    no program code. A run that cannot say which product it is about has nothing to
    assess, so `_params` raises instead of guessing."""
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta: dict = {}

    with pytest.raises(ValueError, match="no program code"):
        silo._params(Run())


def test_the_failure_names_the_upload_as_the_likely_cause(psa):
    """The message has to explain itself: whoever sees it uploaded a file and got a
    stage error, and nothing else in the trace connects the two."""
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta = {"source_row": 20}

    with pytest.raises(ValueError, match="cannot be started by uploading a file"):
        silo._params(Run())


def test_a_run_with_no_metadata_at_all_fails_the_same_way(psa):
    """`meta` is nullable, so `None` is reachable and must not become an AttributeError."""
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta = None

    with pytest.raises(ValueError, match="no program code"):
        silo._params(Run())


def test_params_normalises_a_blank_vendor_to_none(psa):
    """An empty string from a form field is "no supplier named", not a supplier called ""."""
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta = {"program": "AGN-151586", "source_row": 20, "vendor": "   "}

    assert silo._params(Run())["vendor"] is None


def test_params_keeps_a_real_vendor(psa):
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta = {"program": "AGN-151586", "source_row": 20, "vendor": " Datwyler "}

    assert silo._params(Run())["vendor"] == "Datwyler"


def test_params_passes_the_source_row_through_untouched(psa):
    """It is Smartsheet's key, not ours to reinterpret."""
    silo = psa["silo"]

    class Run:
        id = "run-1"
        meta = {"program": "AGN-151586", "source_row": "20"}

    assert silo._params(Run())["source_row"] == "20"


# ── what the screen reads back ────────────────────────────────────────────────


def queued(session, owner_id: str, **meta):
    """A PSA run as `runs.py` creates one: no input file, nothing checkpointed yet.

    `make_run` attaches an input `source.pdf` and, when complete, an output `report.docx`.
    PSA has no upload, so the input row is removed here — otherwise every assertion about
    `files` would be describing the fixture rather than the silo.
    """
    from api.backend.da_platform.db.models import RunFile

    run = make_run(session, owner_id=owner_id, title="PSA AGN-151586", silo="psa",
                   status="queued")
    session.query(RunFile).filter(RunFile.run_id == run.id).delete()
    run.meta = {"program": "AGN-151586", "source_row": 20, "vendor": None, **meta}
    session.commit()
    session.refresh(run)
    return run


def test_the_result_reports_the_report_checkpoint(api, session, psa):
    """The screen used to get this from the synchronous `POST /report`, which returned the
    engine's whole result dict. Now a worker produces the report, so that dict lives in the
    stage's checkpoint — without this endpoint the screen would show only "done"."""
    asha = login(api)
    run = queued(session, asha["id"])
    queue.save_checkpoint(
        session, run.id, "report",
        {"status": "ok", "verify_ok": True, "output_exists": True},
    )

    payload = api.get(f"/api/silos/psa/runs/{run.id}/result").json()

    assert payload["run_id"] == run.id
    assert payload["status"] == "ok"
    assert payload["verify_ok"] is True


def test_the_result_hides_the_storage_key(api, session, psa):
    """A storage key is not the client's business to address; `files` carries the
    platform's own file ids instead, which is what the download endpoint takes."""
    asha = login(api)
    run = queued(session, asha["id"])
    queue.save_checkpoint(
        session, run.id, "report", {"status": "ok", "output_key": "psa/reports/x.docx"},
    )

    payload = api.get(f"/api/silos/psa/runs/{run.id}/result").json()

    assert "output_key" not in payload


def test_the_result_lists_only_output_files(api, session, psa):
    """An input would be the wrong document to offer, and PSA should never have one."""
    from api.backend.da_platform.db.models import RunFile

    asha = login(api)
    run = queued(session, asha["id"])
    session.add(
        RunFile(run_id=run.id, kind="output", filename="psa_assessment.docx",
                storage_key=f"runs/{run.id}/output/psa_assessment.docx",
                size_bytes=2048, content_type="application/octet-stream")
    )
    session.add(
        RunFile(run_id=run.id, kind="input", filename="stray.pdf",
                storage_key=f"runs/{run.id}/input/stray.pdf",
                size_bytes=10, content_type="application/pdf")
    )
    session.commit()

    payload = api.get(f"/api/silos/psa/runs/{run.id}/result").json()

    assert [f["filename"] for f in payload["files"]] == ["psa_assessment.docx"]


def test_the_result_reports_the_run_state_before_the_report_exists(api, session, psa):
    """A queued run has no checkpoint yet, and the screen polls this endpoint from the
    moment it starts — so an empty checkpoint must be an answer, not an error."""
    asha = login(api)
    run = queued(session, asha["id"])

    payload = api.get(f"/api/silos/psa/runs/{run.id}/result").json()

    assert payload["run_status"] == "queued"
    assert payload["files"] == []


def test_the_result_is_readable_by_a_non_owner(api, session, psa):
    """Anyone may read a document; PSA gates no reads. Nothing here acts on the run, so
    there is no owner-only action to enforce."""
    asha = login(api)
    run = queued(session, asha["id"])

    login(api, "ben.carter")

    assert api.get(f"/api/silos/psa/runs/{run.id}/result").status_code == 200


def test_the_result_does_not_reach_another_silos_run(api, session, psa):
    """Another silo's run must not be readable through PSA's endpoints."""
    asha = login(api)
    run = make_run(session, owner_id=asha["id"], title="An ISO doc", silo="iso")

    assert api.get(f"/api/silos/psa/runs/{run.id}/result").status_code == 404


def test_the_result_404s_on_an_unknown_run(api, psa):
    login(api)

    assert api.get("/api/silos/psa/runs/no-such-run/result").status_code == 404


def test_the_result_requires_authentication(api, session, psa):
    asha = login(api)
    run = queued(session, asha["id"])
    api.cookies.clear()

    assert api.get(f"/api/silos/psa/runs/{run.id}/result").status_code == 401
