"""MFG ATR plumbing: the stage contract, identifier-based run creation, and the pause.

This silo has no uploaded document — a run starts from an identifier chosen in the UI, so
creation is owned by the silo's own router rather than the platform's upload endpoint. The
parameters live on the run's `meta` column, which the `fetch` stage reads.

No database and no warehouse: the connection is injected, so these tests cover the
platform-facing half of the port.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.backend.da_platform.engine import queue, runner
from api.backend.da_platform.settings import BACKEND_ROOT, settings
from api.backend.da_platform.silo_registry import _build, _load_module
from tests.conftest import login, make_run

SILO_DIR = BACKEND_ROOT / "silos" / "mfg_atr"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the MFG ATR silo is not present"
)


@pytest.fixture(scope="module")
def mfg():
    module = _load_module("mfg_atr", SILO_DIR / "silo.py")
    router_module = _load_module("mfg_atr", SILO_DIR / "router.py", name="router")
    return {"silo": module, "router_module": router_module}


@pytest.fixture
def api(mfg) -> TestClient:
    """An app with auth, the document routes and this silo's router.

    Built here because conftest points SILOS_DIR at the probe fixture, so the shared
    client never mounts a real silo's router.

    **`uploads` is included, in the same order `main.py` uses, and that matters.** It owns
    `POST /api/silos/{silo_id}/documents` for multipart uploads, and FastAPI matches routes
    in registration order — so a silo route registered later at a path that the wildcard also
    matches is unreachable. Leaving `uploads` out of this fixture once hid exactly that: the
    creation tests passed against an app whose routing did not match the real one.
    """
    from api.backend.da_platform.routers import auth as auth_api
    from api.backend.da_platform.routers import documents as documents_api
    from api.backend.da_platform.routers import uploads as uploads_api

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    app.include_router(uploads_api.router, prefix="/api")
    app.include_router(documents_api.router, prefix="/api")
    app.include_router(mfg["router_module"].router, prefix="/api/silos/mfg_atr")
    return TestClient(app)


def create(api: TestClient, **body) -> object:
    payload = {"report_type": "atr", "identifier": "CMC-10352", **body}
    return api.post("/api/silos/mfg_atr/runs", json=payload)


# ── the contract ──────────────────────────────────────────────────────────────


def test_the_silo_declares_no_accepted_upload(mfg):
    """`ACCEPTS = []` is the honest declaration: there is nothing to upload. The registry
    treats an empty list as "no restriction", so the platform's upload endpoint would
    accept anything — which is why creation is owned by this silo's router instead."""
    silo = mfg["silo"]

    assert silo.ACCEPTS == []
    assert silo.STAGES == ["fetch", "generate", "await_edits", "finalize"]
    assert silo.STORAGE_PREFIX == "ATR_MFG"
    for stage in silo.STAGES:
        assert callable(getattr(silo, stage)), stage


def test_the_platform_discovers_the_silo(monkeypatch, mfg):
    from api.backend.da_platform import silo_registry

    monkeypatch.setattr(
        silo_registry,
        "settings",
        dataclasses.replace(settings, silos_dir=BACKEND_ROOT / "silos", enabled_silos=[]),
    )
    found = {silo.id: silo for silo in silo_registry.discover_silos()}

    assert "mfg_atr" in found, "the silo was skipped; check the registry log"
    assert found["mfg_atr"].stages == ["fetch", "generate", "await_edits", "finalize"]
    assert found["mfg_atr"].router is not None


def test_both_report_types_normalise_their_identifier(mfg):
    """The two id schemes are the silo's input contract."""
    config = _load_module("mfg_atr", SILO_DIR / "config.py", name="config")
    mfgr_config = _load_module("mfg_atr", SILO_DIR / "mfgr_config.py", name="mfgr_config")

    assert config.normalize_request_id("10352") == {
        "short": "10352",
        "display": "CMC-10352",
        "query": "PEGA-PROD-CMC-10352",
    }
    assert config.normalize_request_id("CMC-10352")["query"] == "PEGA-PROD-CMC-10352"

    # NEST-registered batches get the prefix; manually-registered ones do not.
    assert mfgr_config.normalize_batch_id("BAX000584") == {
        "short": "BAX000584",
        "display": "BAX000584",
        "query": "nest-br-prod-BAX000584",
        "registration": "nest",
    }
    assert mfgr_config.normalize_batch_id("BA259821-08") == {
        "short": "BA259821",
        "display": "BA259821",
        "query": "BA259821",
        "registration": "manual",
    }


# ── run creation from an identifier ───────────────────────────────────────────


def test_creating_a_run_needs_no_file(api, session, mfg):
    asha = login(api)

    response = create(api)

    assert response.status_code == 202, response.text
    run_id = response.json()["id"]

    from api.backend.da_platform.db.models import Run

    run = session.get(Run, run_id)
    assert run is not None
    assert run.silo_id == "mfg_atr"
    assert run.user_id == asha["id"]
    assert run.status == "queued"
    assert run.files == [], "there is no input document"


def test_the_parameters_land_on_the_run_metadata(api, session, mfg):
    """`fetch` reads these; the `meta` JSON column exists for exactly this."""
    login(api)

    run_id = create(api, identifier="10352").json()["id"]

    from api.backend.da_platform.db.models import Run

    meta = session.get(Run, run_id).meta
    assert meta["report_type"] == "atr"
    assert meta["ids"]["display"] == "CMC-10352"
    assert meta["ids"]["query"] == "PEGA-PROD-CMC-10352"
    assert meta["source"] == "live", "the warehouse is the only data source"


def test_the_title_is_the_display_identifier(api, session, mfg):
    login(api)
    run_id = create(api, report_type="mfgr", identifier="BAX000584").json()["id"]

    from api.backend.da_platform.db.models import Run

    assert "BAX000584" in session.get(Run, run_id).title


def test_a_bad_identifier_keeps_the_original_message(api, mfg):
    login(api)

    response = create(api, identifier="not-an-id")

    assert response.status_code == 400
    assert "could not find a numeric CMC request id" in response.json()["detail"]


def test_a_bad_batch_id_keeps_the_original_message(api, mfg):
    login(api)

    response = create(api, report_type="mfgr", identifier="nonsense")

    assert response.status_code == 400
    assert "could not find a BA/BAX batch id" in response.json()["detail"]


def test_an_unknown_report_type_is_rejected(api, mfg):
    login(api)
    assert create(api, report_type="widget").status_code == 400


def test_no_run_is_created_when_the_identifier_is_invalid(api, session, mfg):
    """A rejected request must leave nothing in the history."""
    login(api)
    from api.backend.da_platform.db.models import Run

    before = session.query(Run).count()
    create(api, identifier="rubbish")
    session.expire_all()

    assert session.query(Run).count() == before


def test_creation_requires_authentication(api, mfg):
    api.cookies.clear()
    assert create(api).status_code == 401


def test_the_create_path_is_not_shadowed_by_the_platforms_upload_route(api, mfg):
    """Creation lives at `/runs` because `/documents` is unreachable here.

    `api/uploads.py` owns `POST /api/silos/{silo_id}/documents` for multipart uploads, and
    FastAPI matches in registration order with core routers first. A silo route at
    `/documents` is therefore never reached: the wildcard answers first and rejects a JSON
    body with 422 for a missing `file`. This pins the symptom, because the failure mode is a
    confusing validation error rather than a 404.
    """
    login(api)

    shadowed = api.post(
        "/api/silos/mfg_atr/documents",
        json={"report_type": "atr", "identifier": "CMC-10352"},
    )
    assert shadowed.status_code == 422, "still the upload route, so still not ours"

    assert create(api).status_code == 202, "and /runs is ours"


# ── the human edit pause ──────────────────────────────────────────────────────


def parked(session, owner_id: str):
    """A run stopped at `await_edits` with the earlier stages checkpointed."""
    run = make_run(session, owner_id=owner_id, title="CMC-10352", silo="mfg_atr")
    run.status = "awaiting_user"
    run.stage = "await_edits"
    run.meta = {
        "report_type": "atr",
        "identifier": "CMC-10352",
        "ids": {"short": "10352", "display": "CMC-10352", "query": "PEGA-PROD-CMC-10352"},
        "source": "live",
    }
    session.commit()
    queue.save_checkpoint(session, run.id, "fetch", {"has_data": True, "raw_key": "k"})
    queue.save_checkpoint(session, run.id, "generate", {"generated_at": "2026-08-07T00:00:00Z"})
    return run


def test_the_authors_values_complete_the_paused_stage(api, session, mfg):
    asha = login(api)
    run = parked(session, asha["id"])

    response = api.post(
        f"/api/silos/mfg_atr/documents/{run.id}/field-values",
        json={"field_values": {"regulatory": "Yes", "hqc_assessment": "meets"}},
    )

    assert response.status_code == 202, response.text
    session.refresh(run)
    assert run.status == "queued"
    assert queue.checkpoint_output(session, run.id, "await_edits") == {
        "field_values": {"regulatory": "Yes", "hqc_assessment": "meets"}
    }


def test_empty_values_are_allowed(api, session, mfg):
    """Every editable field is optional in the source — the author may sign off with the
    defaults, so an empty submission must not be rejected."""
    asha = login(api)
    run = parked(session, asha["id"])

    response = api.post(
        f"/api/silos/mfg_atr/documents/{run.id}/field-values", json={"field_values": {}}
    )

    assert response.status_code == 202


def test_a_non_owner_cannot_submit_values(api, session, mfg):
    asha = login(api)
    run = parked(session, asha["id"])

    login(api, "ben.carter")
    response = api.post(
        f"/api/silos/mfg_atr/documents/{run.id}/field-values", json={"field_values": {}}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {"can_fork": True}


def test_values_are_refused_when_the_run_is_not_parked(api, session, mfg):
    asha = login(api)
    run = parked(session, asha["id"])
    run.status = "running"
    session.commit()

    response = api.post(
        f"/api/silos/mfg_atr/documents/{run.id}/field-values", json={"field_values": {}}
    )

    assert response.status_code == 409


def test_another_silos_run_is_not_reachable(api, session, mfg):
    asha = login(api)
    run = make_run(session, owner_id=asha["id"], title="An ISO doc", silo="iso")

    response = api.post(
        f"/api/silos/mfg_atr/documents/{run.id}/field-values", json={"field_values": {}}
    )

    assert response.status_code == 404


def test_submitting_values_resumes_the_run_at_finalize(api, session, mfg, monkeypatch):
    """The pause that replaces the original's in-memory TTL cache, end to end."""
    asha = login(api)
    run = parked(session, asha["id"])

    assert (
        api.post(
            f"/api/silos/mfg_atr/documents/{run.id}/field-values",
            json={"field_values": {"regulatory": "No"}},
        ).status_code
        == 202
    )

    calls: list[str] = []

    def fake_finalize(run_arg, ctx):
        calls.append("finalize")
        return {"values": ctx.checkpoint("await_edits")["field_values"]}

    monkeypatch.setattr(mfg["silo"], "finalize", fake_finalize)
    silo = _build("mfg_atr", mfg["silo"])

    claimed = queue.claim(session, "worker-9")
    assert claimed.id == run.id
    assert runner.run_one(session, claimed, silo) == "complete"

    # fetch, generate and await_edits are all checkpointed, so only finalize runs.
    assert calls == ["finalize"]
    assert queue.checkpoint_output(session, run.id, "finalize")["values"] == {
        "regulatory": "No"
    }


# ── the warehouse status probe ─────────────────────────────────────────────────


class StubWarehouse:
    def __init__(self, reachable: bool, rows: list | None = None):
        self._reachable = reachable
        self._rows = rows or []
        self.schema = "DEVSCI_DM"
        self.results_object = "mv_combined_results"
        self.results_view = "DEVSCI_DM.mv_combined_results"

    def reachable(self) -> bool:
        return self._reachable

    @contextlib.contextmanager
    def connect(self):
        rows = self._rows
        if not self._reachable:
            raise RuntimeError("warehouse is unreachable")

        class Cur:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, **binds):
                self_inner.description = [("SHORT_ID",)]
                return self_inner

            def fetchall(self_inner):
                return [(r,) for r in rows]

            def fetchone(self_inner):
                return (rows[0],) if rows else None

        class Conn:
            def cursor(self_inner):
                return Cur()

        yield Conn()


def test_status_reports_unreachable_instead_of_failing(api, mfg, monkeypatch):
    """The picker screen has to render when the warehouse is down.

    Patched on the *router module*, not on `api.backend.da_platform.warehouse`: the router does
    `from ... import get_warehouse`, so it holds its own reference and patching the origin
    module would do nothing. Without credentials `reachable()` returns False regardless,
    so that mistake would make this test pass while proving nothing — see the companion
    test below, which cannot pass by accident.
    """
    monkeypatch.setattr(
        mfg["router_module"], "get_warehouse", lambda: StubWarehouse(False)
    )
    login(api)

    response = api.get("/api/silos/mfg_atr/status")

    assert response.status_code == 200
    assert response.json()["warehouse_reachable"] is False
    assert response.json()["results_view"] is None


def test_status_reports_the_results_view_when_reachable(api, mfg, monkeypatch):
    """The one that cannot pass vacuously: no warehouse credentials are configured in the
    test environment, so a True answer can only come from the patch taking effect."""
    monkeypatch.setattr(
        mfg["router_module"], "get_warehouse", lambda: StubWarehouse(True)
    )
    login(api)

    payload = api.get("/api/silos/mfg_atr/status").json()

    assert payload["warehouse_reachable"] is True
    assert payload["results_view"] == "DEVSCI_DM.mv_combined_results"


# ── what the edit screen reads ────────────────────────────────────────────────


def attach_output(session, run, filename: str) -> str:
    """Attach an output file the way the storage helper does, and return its id."""
    from api.backend.da_platform.db.models import RunFile

    record = RunFile(
        run_id=run.id,
        kind="output",
        filename=filename,
        storage_key=f"runs/{run.id}/output/{filename}",
        size_bytes=2048,
        content_type="application/pdf",
    )
    session.add(record)
    session.commit()
    return record.id


def test_review_returns_the_flags_from_the_generate_checkpoint(api, session, mfg):
    """The flags describe that stage's output, so they live on its checkpoint rather than on
    the run — this endpoint is how the edit screen gets at them."""
    asha = login(api)
    run = parked(session, asha["id"])
    queue.save_checkpoint(
        session,
        run.id,
        "generate",
        {
            "generated": True,
            "flags": [
                {"severity": "warn", "area": "results", "message": "Missing value"}
            ],
        },
    )

    payload = api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").json()

    assert payload["generated"] is True
    assert payload["display"] == "CMC-10352"
    assert payload["report_type"] == "atr"
    assert payload["flags"] == [
        {"severity": "warn", "area": "results", "message": "Missing value"}
    ]


def test_review_names_the_clean_pdf_as_the_preview(api, session, mfg):
    asha = login(api)
    run = parked(session, asha["id"])
    file_id = attach_output(session, run, "ATR_CMC-10352.pdf")

    payload = api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").json()

    assert payload["preview_file_id"] == file_id


def test_review_does_not_offer_an_input_pdf_as_the_preview(api, session, mfg):
    """The fixture run carries `source.pdf` as an *input*. Binding the editor to an input
    would put the form over the wrong document, so the filter is on kind as well as
    extension."""
    asha = login(api)
    run = parked(session, asha["id"])

    payload = api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").json()

    assert payload["preview_file_id"] is None


def test_review_skips_the_locked_edited_pdf(api, session, mfg):
    """Finalize encrypts the edited copy, so its fields no longer round-trip — the editor
    must bind to the clean render even when both are attached."""
    asha = login(api)
    run = parked(session, asha["id"])
    edited = attach_output(session, run, "ATR_CMC-10352_edited.pdf")
    clean = attach_output(session, run, "ATR_CMC-10352.pdf")

    payload = api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").json()

    assert payload["preview_file_id"] == clean
    assert payload["preview_file_id"] != edited


def test_review_is_readable_by_a_non_owner(api, session, mfg):
    """Anyone may read a document (D13); ownership gates acting on it, which is the
    field-values endpoint."""
    asha = login(api)
    run = parked(session, asha["id"])

    login(api, "ben.carter")

    assert api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").status_code == 200


def test_review_does_not_reach_another_silos_run(api, session, mfg):
    asha = login(api)
    run = make_run(session, owner_id=asha["id"], title="An ISO doc", silo="iso")

    assert api.get(f"/api/silos/mfg_atr/documents/{run.id}/review").status_code == 404


# ── the pickers ───────────────────────────────────────────────────────────────


@pytest.fixture
def picker(api, mfg, monkeypatch):
    """Point the router's pickers at a stub warehouse, with its answer cache emptied.

    The router caches each picker answer for an hour, so without the clear a test would
    silently read whichever answer ran first and pass for the wrong reason.
    """
    module = mfg["router_module"]

    def use(reachable: bool, rows: list | None = None):
        module._cache.clear()
        monkeypatch.setattr(
            module, "get_warehouse", lambda: StubWarehouse(reachable, rows)
        )
        login(api)
        return api

    module._cache.clear()
    return use


def test_request_ids_are_returned_in_their_display_form(picker):
    """The warehouse holds the fully-qualified id; the picker offers what the user reads and
    what `normalize_request_id` accepts back."""
    client = picker(True, ["PEGA-PROD-CMC-10352", "PEGA-PROD-CMC-10400"])

    payload = client.get("/api/silos/mfg_atr/request-ids").json()

    assert payload["request_ids"] == ["CMC-10352", "CMC-10400"]
    assert payload["reachable"] is True


def test_an_unparseable_request_id_is_dropped_rather_than_breaking_the_picker(picker):
    client = picker(True, ["PEGA-PROD-CMC-10352", "junk"])

    assert client.get("/api/silos/mfg_atr/request-ids").json()["request_ids"] == ["CMC-10352"]


def test_request_ids_answers_with_an_empty_picker_when_the_warehouse_is_down(picker):
    """An empty picker beats a broken page — the user can still type an id."""
    client = picker(False)

    response = client.get("/api/silos/mfg_atr/request-ids")

    assert response.status_code == 200
    assert response.json() == {"request_ids": [], "reachable": False}


def test_batch_ids_lists_what_the_warehouse_returns(picker):
    client = picker(True, ["BAX000584", "BA259821"])

    payload = client.get("/api/silos/mfg_atr/batch-ids").json()

    assert payload["batch_ids"] == ["BAX000584", "BA259821"]
    assert payload["reachable"] is True


def test_batch_ids_answers_with_an_empty_picker_when_the_warehouse_is_down(picker):
    client = picker(False)

    response = client.get("/api/silos/mfg_atr/batch-ids")

    assert response.status_code == 200
    assert response.json() == {"batch_ids": [], "reachable": False}


def test_an_existing_batch_is_reported_as_present(picker):
    client = picker(True, ["BAX000701"])

    assert client.get("/api/silos/mfg_atr/exists?id=BAX000701").json() == {
        "id": "BAX000701",
        "exists": True,
    }


def test_a_missing_batch_is_reported_as_absent_rather_than_failing(picker):
    client = picker(True, [])

    assert client.get("/api/silos/mfg_atr/exists?id=BAX000999").json() == {
        "id": "BAX000999",
        "exists": False,
    }


def test_a_malformed_batch_id_is_rejected_before_the_warehouse_is_touched(api, mfg):
    login(api)

    response = api.get("/api/silos/mfg_atr/exists?id=not-a-batch")

    assert response.status_code == 400
    assert "BAX000584" in response.json()["detail"], "the source's own guidance"


def test_a_stage_suffixed_batch_id_normalises_before_the_lookup(picker):
    client = picker(True, ["BA259821"])

    assert client.get("/api/silos/mfg_atr/exists?id=BA259821-08").json()["id"] == "BA259821"


def test_the_pickers_require_authentication(api, mfg):
    api.cookies.clear()
    assert api.get("/api/silos/mfg_atr/batch-ids").status_code == 401
    assert api.get("/api/silos/mfg_atr/request-ids").status_code == 401
