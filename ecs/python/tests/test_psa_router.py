"""The fourteen standalone endpoints: the Cap Colour Selection screen's data contract.

`tests/test_psa_plumbing.py` covers the run routes and the platform contract. This file covers
everything else `router.py` serves, with `engines` stubbed — so a failure is the endpoint's
translation of an engine answer into an HTTP one, rather than the engine's answer.

Three properties are the reason the file exists:

  * **Nothing here 500s on a data gap.** These endpoints back a screen that has to render while
    Smartsheet is unreachable, the database is unbuilt, or a presentation is missing from a
    rebuilt catalogue. Each of those is an ANSWER — `/health` reporting `smartsheet_live: false`,
    a recommendation carrying an `error` string — not an exception.

  * **A generated file's path never reaches the client.** `/cap-export` and `/report` swap the
    engine's absolute path for an opaque download id, because handing over the path would make
    any file the server process can read downloadable.

  * **The process is configured on the first request.** A stage and an endpoint do not share a
    process: the API serves the router while a separate worker runs the stages, so nothing else
    configures the API. Without `_ensure_configured` every endpoint answered from
    `config.defaults()`, which is repo-relative — a single request would have created
    `silos/Database/psa.db` inside the source tree and reported `smartsheet_live: false`
    forever, leaving the Product picker permanently empty.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.conftest import login

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    router_module = _load_module("psa", SILO_DIR / "router.py", name="router")
    from da_silos.psa import cap_colors, config, downloads, engines, palette

    return {"router_module": router_module, "engines": engines, "palette": palette,
            "cap_colors": cap_colors, "downloads": downloads, "config": config}


@pytest.fixture
def api(psa) -> TestClient:
    """An app with auth and PSA's router, as `app.py` mounts it."""
    from api.backend.da_platform.routers import auth as auth_api

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    app.include_router(psa["router_module"].router, prefix="/api/silos/psa")
    client = TestClient(app)
    login(client)
    return client


@pytest.fixture(autouse=True)
def stub_engines(psa, monkeypatch, tmp_path):
    """Neutral engine answers, so each test overrides only what it is about.

    Patched on the modules the router imported, not on a name in `router.py`: it does
    `from . import engines`, so it holds its own reference to the module object.
    """
    monkeypatch.setattr(psa["engines"], "smartsheet_live", lambda: True)
    monkeypatch.setattr(psa["engines"], "db_exists", lambda: True)
    monkeypatch.setattr(psa["engines"], "refresh", lambda skip_images=True: (True, "done"))
    monkeypatch.setattr(psa["engines"], "programs", lambda: [])
    monkeypatch.setattr(psa["engines"], "presentations", lambda program: [])
    monkeypatch.setattr(psa["engines"], "products_by_color", lambda vendor, color: [])
    monkeypatch.setattr(psa["engines"], "site_product_counts", lambda: [])
    monkeypatch.setattr(
        psa["engines"], "recommendation",
        lambda program, row, vendor: ({"recommended": [], "taken": []}, "P", "R"),
    )
    monkeypatch.setattr(
        psa["engines"], "export_recommendation",
        lambda program, row, vendor: (None, "nothing to export"),
    )
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda program, presentation: {"status": "ok", "output_exists": False},
    )


def docx(tmp_path, name="report.docx") -> str:
    path = tmp_path / name
    path.write_bytes(b"PK\x03\x04 pretend docx")
    return str(path)


# ── health, which has to answer even when nothing works ───────────────────────


def test_health_reports_the_two_facts_that_change_every_other_answer(api, psa):
    """Whether the data is live Smartsheet or the stale export, and whether the palette
    carries local edits."""
    payload = api.get("/api/silos/psa/health").json()

    assert payload["ok"] is True
    assert payload["smartsheet_live"] is True
    assert payload["db_built"] is True


def test_health_reports_a_missing_token_rather_than_failing(api, psa, monkeypatch):
    """The screen has to render when the credential is absent, and this is the field that
    tells it to say so. A 500 here would leave the user with no explanation at all."""
    monkeypatch.setattr(psa["engines"], "smartsheet_live", lambda: False)

    response = api.get("/api/silos/psa/health")

    assert response.status_code == 200
    assert response.json()["smartsheet_live"] is False


def test_health_reports_an_unbuilt_database(api, psa, monkeypatch):
    """True on a fresh deployment, before any run has captured a snapshot."""
    monkeypatch.setattr(psa["engines"], "db_exists", lambda: False)

    assert api.get("/api/silos/psa/health").json()["db_built"] is False


def test_health_reports_whether_the_palette_has_been_edited(api, psa):
    """A property of the catalogue, so it is reported whichever vendor is being viewed."""
    assert "palette_is_override" in api.get("/api/silos/psa/health").json()


def test_health_requires_authentication(api, psa):
    api.cookies.clear()

    assert api.get("/api/silos/psa/health").status_code == 401


# ── the pickers ───────────────────────────────────────────────────────────────


def test_the_programs_picker_lists_in_scope_products(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["engines"], "programs",
        lambda: [{"program_no": "AGN-151586", "program_name": "BoNTE",
                  "label": "AGN-151586 (BoNTE)"}],
    )

    payload = api.get("/api/silos/psa/programs").json()

    assert payload[0]["program_no"] == "AGN-151586"
    assert payload[0]["label"] == "AGN-151586 (BoNTE)"


def test_a_program_with_no_name_still_lists(api, psa, monkeypatch):
    """`program_name` defaults to empty, because a pipeline product often has a code and no
    approved name yet — and it must still be assessable."""
    monkeypatch.setattr(
        psa["engines"], "programs",
        lambda: [{"program_no": "RGX-314", "label": "RGX-314"}],
    )

    payload = api.get("/api/silos/psa/programs").json()

    assert payload[0]["program_name"] == ""


def test_an_empty_programs_picker_is_a_valid_answer(api, psa):
    """A missing token or an unbuilt database produces this, and the screen shows an empty
    picker rather than an error — the user can still see why from `/health`."""
    response = api.get("/api/silos/psa/programs")

    assert response.status_code == 200
    assert response.json() == []


def test_the_presentations_picker_needs_a_program(api, psa):
    """Without one there is nothing to list, and defaulting to "all" would offer
    presentations from unrelated products."""
    assert api.get("/api/silos/psa/presentations").status_code == 422


def test_the_presentations_picker_passes_the_program_through(api, psa, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        psa["engines"], "presentations",
        lambda program: seen.append(program) or [],
    )

    api.get("/api/silos/psa/presentations", params={"program": "AGN-151586"})

    assert seen == ["AGN-151586"]


def test_a_presentation_is_keyed_by_its_smartsheet_row(api, psa, monkeypatch):
    """`source_row` is the key that survives a database rebuild, unlike `product_id`."""
    monkeypatch.setattr(
        psa["engines"], "presentations",
        lambda program: [{"source_row": 20, "label": "2R (2.00 mL) vial"}],
    )

    payload = api.get(
        "/api/silos/psa/presentations", params={"program": "AGN-151586"}
    ).json()

    assert payload[0]["source_row"] == 20


def test_the_pickers_require_authentication(api, psa):
    api.cookies.clear()

    assert api.get("/api/silos/psa/programs").status_code == 401
    assert api.get(
        "/api/silos/psa/presentations", params={"program": "X"}
    ).status_code == 401


# ── the palette, read and edited ──────────────────────────────────────────────


def test_the_palette_is_returned_for_the_swatch_grid(api, psa):
    payload = api.get("/api/silos/psa/palette").json()

    assert payload["colors"], "the shipped catalogue should not be empty"
    assert "is_override" in payload


def test_the_palette_defaults_to_one_vendor(api, psa):
    """A grid has to show one supplier's catalogue; the RANKING is what spans both."""
    payload = api.get("/api/silos/psa/palette").json()

    assert {c["vendor"].upper() for c in payload["colors"]} == {"DATWYLER"}


def test_another_vendors_palette_can_be_asked_for(api, psa):
    payload = api.get("/api/silos/psa/palette", params={"vendor": "West"}).json()

    assert {c["vendor"].upper() for c in payload["colors"]} == {"WEST"}


def test_the_palette_rows_drop_the_internal_normalised_code(api, psa):
    """`_code_norm` is an implementation detail of the matcher, and shipping it would invite
    a client to depend on it."""
    payload = api.get("/api/silos/psa/palette").json()

    assert "_code_norm" not in payload["colors"][0]


def test_every_palette_row_is_complete(api, psa):
    """The response model coerces absent fields to empty strings rather than null, because
    the grid renders them directly."""
    colour = api.get("/api/silos/psa/palette").json()["colors"][0]

    for field in ("vendor", "vendor_color_name", "canonical_color", "hex", "component"):
        assert colour[field] is not None, field


def test_adding_a_colour_reports_success(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["cap_colors"], "add_color",
        lambda *a, **kw: (True, "Added Datwyler 'Test Teal' to the palette."),
    )

    payload = api.post("/api/silos/psa/palette", json={
        "vendor": "Datwyler", "vendor_color_name": "Test Teal",
        "canonical_color": "Teal", "hex": "#008080",
    }).json()

    assert payload["ok"] is True
    assert "Test Teal" in payload["message"]


def test_a_rejected_addition_is_reported_rather_than_raised(api, psa, monkeypatch):
    """The engine answers with a reason — a duplicate, a bad hex — and the screen shows it."""
    monkeypatch.setattr(
        psa["cap_colors"], "add_color", lambda *a, **kw: (False, "that hex is not valid")
    )

    response = api.post("/api/silos/psa/palette", json={
        "vendor": "Datwyler", "vendor_color_name": "X", "canonical_color": "Teal",
    })

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_an_addition_needs_a_vendor_and_a_name(api, psa):
    """They are the key a palette row is identified by, so neither can be guessed."""
    assert api.post("/api/silos/psa/palette", json={"canonical_color": "Teal"}).status_code \
        == 422


def test_removing_a_colour_by_delete(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["cap_colors"], "remove_color", lambda vendor, name: (True, f"Removed {name}.")
    )

    payload = api.request(
        "DELETE", "/api/silos/psa/palette",
        params={"vendor": "Datwyler", "vendor_color_name": "Blue 6043"},
    ).json()

    assert payload["ok"] is True


def test_removing_a_colour_by_post_gives_the_same_answer(api, psa, monkeypatch):
    """The POST alias exists because ARIA's shared HTTP client exports only get/post/put, so
    a DELETE from the React feature would mean editing a platform file. Both routes must
    reach the same engine call or the two clients would diverge."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        psa["cap_colors"], "remove_color",
        lambda vendor, name: calls.append((vendor, name)) or (True, "Removed."),
    )

    api.request("DELETE", "/api/silos/psa/palette",
                params={"vendor": "Datwyler", "vendor_color_name": "Blue 6043"})
    api.post("/api/silos/psa/palette/remove",
             json={"vendor": "Datwyler", "vendor_color_name": "Blue 6043"})

    assert calls == [("Datwyler", "Blue 6043"), ("Datwyler", "Blue 6043")]


def test_removing_a_colour_that_is_not_there_is_reported(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["cap_colors"], "remove_color", lambda *a: (False, "not in the palette")
    )

    assert api.post("/api/silos/psa/palette/remove", json={
        "vendor": "Datwyler", "vendor_color_name": "Nope"
    }).json()["ok"] is False


def test_a_delete_needs_both_keys(api, psa):
    assert api.request(
        "DELETE", "/api/silos/psa/palette", params={"vendor": "Datwyler"}
    ).status_code == 422


def test_the_palette_routes_require_authentication(api, psa):
    api.cookies.clear()

    assert api.get("/api/silos/psa/palette").status_code == 401
    assert api.post("/api/silos/psa/palette", json={
        "vendor": "D", "vendor_color_name": "X", "canonical_color": "Teal"
    }).status_code == 401


# ── the tables beside the grid ────────────────────────────────────────────────


def test_the_products_by_colour_table_needs_both_keys(api, psa):
    """A colour without a vendor is ambiguous: both suppliers have a 'Blue'."""
    assert api.get(
        "/api/silos/psa/products-by-color", params={"vendor": "Datwyler"}
    ).status_code == 422


def test_the_products_by_colour_table_is_returned(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["engines"], "products_by_color",
        lambda vendor, color: [{"product": "ABBV-400", "mfr_sites": "AP01"}],
    )

    payload = api.get("/api/silos/psa/products-by-color",
                      params={"vendor": "Datwyler", "color": "Blue 6043"}).json()

    assert payload[0]["product"] == "ABBV-400"


def test_an_unused_colour_returns_an_empty_table(api, psa):
    """Which is the useful answer — it means the colour is free."""
    response = api.get("/api/silos/psa/products-by-color",
                       params={"vendor": "Datwyler", "color": "Blue 6043"})

    assert response.status_code == 200
    assert response.json() == []


def test_the_site_counts_are_returned_for_the_bar_chart(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["engines"], "site_product_counts",
        lambda: [{"site_code": "AP01", "n_products": 12}],
    )

    payload = api.get("/api/silos/psa/site-product-counts").json()

    assert payload[0]["n_products"] == 12


def test_the_tables_require_authentication(api, psa):
    api.cookies.clear()

    assert api.get("/api/silos/psa/site-product-counts").status_code == 401


# ── the recommendation ────────────────────────────────────────────────────────


def test_a_recommendation_is_returned_with_its_labels(api, psa, monkeypatch):
    """The labels come from the engine because only it has read the catalogue; the endpoint
    never reads Smartsheet."""
    monkeypatch.setattr(
        psa["engines"], "recommendation",
        lambda program, row, vendor: (
            {"recommended": [{"cap": "Blue 6043", "vendor": "Datwyler"}], "taken": []},
            "AGN-151586 (BoNTE)", "2R (2.00 mL) vial",
        ),
    )

    payload = api.post("/api/silos/psa/recommend", json={
        "program": "AGN-151586", "source_row": 20,
    }).json()

    assert payload["program_label"] == "AGN-151586 (BoNTE)"
    assert payload["presentation_label"] == "2R (2.00 mL) vial"
    assert payload["recommended"][0]["cap"] == "Blue 6043"


def test_a_missing_presentation_is_answered_not_raised(api, psa, monkeypatch):
    """A stale row after a database rebuild is normal, and the message tells the user to
    refresh rather than leaving them with a 500."""
    monkeypatch.setattr(
        psa["engines"], "recommendation", lambda program, row, vendor: (None, "P", "R"),
    )

    response = api.post("/api/silos/psa/recommend", json={
        "program": "AGN-151586", "source_row": 999,
    })

    assert response.status_code == 200
    assert "refresh from Smartsheet" in response.json()["error"]


def test_the_recommendation_leaves_the_vendor_unset_by_default(api, psa, monkeypatch):
    """The screen does not send one: the best colour names its own supplier, because at
    assessment time the cap is often not yet tooled."""
    seen: list = []
    monkeypatch.setattr(
        psa["engines"], "recommendation",
        lambda program, row, vendor: seen.append(vendor) or ({}, "P", "R"),
    )

    api.post("/api/silos/psa/recommend", json={"program": "AGN-151586", "source_row": 20})

    assert seen == [None]


def test_an_explicit_vendor_is_passed_through(api, psa, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        psa["engines"], "recommendation",
        lambda program, row, vendor: seen.append(vendor) or ({}, "P", "R"),
    )

    api.post("/api/silos/psa/recommend", json={
        "program": "AGN-151586", "source_row": 20, "vendor": "West",
    })

    assert seen == ["West"]


def test_a_recommendation_needs_a_program_and_a_row(api, psa):
    assert api.post("/api/silos/psa/recommend", json={"program": "X"}).status_code == 422


# ── the exports, where a path must not escape ─────────────────────────────────


def test_an_export_returns_an_opaque_id_rather_than_a_path(api, psa, monkeypatch, tmp_path):
    """The engine returns an absolute path; handing that to the client would make any file
    the server process can read downloadable."""
    path = docx(tmp_path, "cap_recommendation.docx")
    monkeypatch.setattr(
        psa["engines"], "export_recommendation", lambda p, r, v: (path, "exported")
    )

    payload = api.post("/api/silos/psa/cap-export", json={
        "program": "AGN-151586", "source_row": 20,
    }).json()

    assert payload["ok"] is True
    assert payload["download_id"]
    assert path not in str(payload)
    assert payload["filename"] == "cap_recommendation.docx"


def test_a_failed_export_carries_no_download(api, psa):
    response = api.post("/api/silos/psa/cap-export", json={
        "program": "AGN-151586", "source_row": 20,
    })

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["download_id"] is None


def test_a_report_returns_an_opaque_id_rather_than_a_path(api, psa, monkeypatch, tmp_path):
    path = docx(tmp_path, "AGN-151586_PSA_generated.docx")
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda program, presentation: {
            "status": "ok", "verify_ok": True, "output_exists": True, "output_path": path,
        },
    )

    payload = api.post("/api/silos/psa/report", json={
        "program": "AGN-151586", "presentation": 20,
    }).json()

    assert payload["status"] == "ok"
    assert payload["download_id"]
    assert "output_path" not in payload


def test_a_validation_answer_is_reported_without_a_file(api, psa, monkeypatch):
    """`status` is 'ok', 'message' or 'needs_override', and only the first has a document."""
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda program, presentation: {
            "status": "message", "message": "pick a presentation", "output_exists": False,
        },
    )

    payload = api.post("/api/silos/psa/report", json={
        "program": "AGN-151586", "presentation": 20,
    }).json()

    assert payload["status"] == "message"
    assert payload["download_id"] is None


def test_a_failed_verify_still_offers_the_report(api, psa, monkeypatch, tmp_path):
    """A false `verify_ok` still leaves a downloadable report whose run log explains what
    failed — withholding it would leave the assessor nothing to inspect."""
    monkeypatch.setattr(
        psa["engines"], "generate_report",
        lambda program, presentation: {
            "status": "ok", "verify_ok": False, "output_exists": True,
            "output_path": docx(tmp_path),
        },
    )

    payload = api.post("/api/silos/psa/report", json={
        "program": "AGN-151586", "presentation": 20,
    }).json()

    assert payload["verify_ok"] is False
    assert payload["download_id"]


# ── downloading ───────────────────────────────────────────────────────────────


def test_a_registered_download_is_served(api, psa, tmp_path):
    path = docx(tmp_path, "report.docx")
    token = REDACTED

    response = api.get(f"/api/silos/psa/download/{token}")

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04 pretend docx"


def test_a_download_is_typed_as_a_word_document(api, psa, tmp_path):
    """The browser decides how to handle it from this, and a wrong type turns a report into
    a file the user has to rename by hand."""
    token = REDACTED

    response = api.get(f"/api/silos/psa/download/{token}")

    assert response.headers["content-type"] == psa["router_module"].DOCX_MIME


def test_a_download_is_named_for_the_file(api, psa, tmp_path):
    token = REDACTED

    response = api.get(f"/api/silos/psa/download/{token}")

    assert "AGN-151586_PSA_generated.docx" in response.headers["content-disposition"]


def test_an_unknown_download_id_is_a_404_with_an_explanation(api, psa):
    """Ids are single-process, so they do not survive a restart. The message tells the user
    to regenerate, which costs one run — a bare 404 would look like a bug."""
    response = api.get("/api/silos/psa/download/not-a-real-id")

    assert response.status_code == 404
    assert "generate it again" in response.json()["detail"]


def test_a_download_whose_file_has_gone_is_a_404(api, psa, tmp_path):
    """The scratch directory is temporary, so a file can vanish between generating and
    downloading."""
    path = docx(tmp_path)
    token = REDACTED
    os.unlink(path)

    assert api.get(f"/api/silos/psa/download/{token}").status_code == 404


def test_a_download_requires_authentication(api, psa, tmp_path):
    """The report is the deliverable, so an unauthenticated fetch of a guessed id must fail
    before the file is opened."""
    token = REDACTED
    api.cookies.clear()

    assert api.get(f"/api/silos/psa/download/{token}").status_code == 401


# ── refreshing, and configuring on the first request ──────────────────────────


def test_a_refresh_reports_what_it_did(api, psa, monkeypatch):
    monkeypatch.setattr(
        psa["engines"], "refresh", lambda skip_images=True: (True, "Ingested 37 products")
    )

    payload = api.post("/api/silos/psa/refresh").json()

    assert payload["ok"] is True
    assert "37" in payload["message"]


def test_a_refresh_skips_the_images(api, psa, monkeypatch):
    """The screen does not show them, and downloading ~37 photos would make a refresh take
    minutes instead of seconds."""
    seen: dict = {}
    monkeypatch.setattr(
        psa["engines"], "refresh",
        lambda skip_images=True: seen.update(skip=skip_images) or (True, "done"),
    )

    api.post("/api/silos/psa/refresh")

    assert seen["skip"] is True


def test_a_failed_refresh_is_reported_rather_than_raised(api, psa, monkeypatch):
    """An expired token reaches here, and the message is the only thing that explains it."""
    monkeypatch.setattr(
        psa["engines"], "refresh",
        lambda skip_images=True: (False, "Smartsheet API: 401 Unauthorized"),
    )

    response = api.post("/api/silos/psa/refresh")

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_the_process_is_configured_before_any_endpoint_reads_a_path(api, psa):
    """A stage and an endpoint do not share a process, so nothing else configures the API.
    Without this, every endpoint answered from the repo-relative defaults and a single request
    would have created `silos/Database/psa.db` inside the source tree."""
    api.get("/api/silos/psa/health")

    assert psa["config"].installed() is True


def test_the_configuration_dependency_is_declared_on_the_router_itself(api, psa):
    """It is a router-level dependency rather than a call in each handler, so a route added
    later cannot forget it. Checked structurally because FastAPI resolves the dependency at
    decoration time — patching the module attribute afterwards would not be seen, so a
    call-counting test would fail even though the wiring is correct."""
    router = psa["router_module"].router
    ensure = psa["router_module"]._ensure_configured

    assert any(d.dependency is ensure for d in router.dependencies), (
        "the config dependency must be on the router, not in individual handlers"
    )


def test_every_psa_route_inherits_the_configuration_dependency(api, psa):
    """The consequence of declaring it once: every path served under this router carries it,
    including the run routes included from `runs.py`."""
    from fastapi.routing import APIRoute

    router = psa["router_module"].router
    ensure = psa["router_module"]._ensure_configured
    routes = [r for r in router.routes if isinstance(r, APIRoute)]

    assert len(routes) >= 14, "the fourteen standalone endpoints at least"
    for route in routes:
        assert any(d.dependency is ensure for d in route.dependencies), route.path


def test_the_run_routes_are_reachable_under_the_same_prefix(api, psa):
    """`runs.py` is included into this router rather than registered separately, which is what
    keeps the mount prefix and the config dependency identical for every PSA path.

    Asserted by reaching the paths rather than by reading the route table: this FastAPI keeps
    an `include_router` as a nested router and resolves it per request, so the sub-routes never
    appear as flat `APIRoute` entries. A 401 rather than a 404 is the proof the path exists —
    it got as far as the auth dependency.
    """
    api.cookies.clear()

    assert api.post("/api/silos/psa/runs", json={}).status_code == 401
    assert api.get("/api/silos/psa/runs/some-id/result").status_code == 401


def test_an_unmounted_path_really_does_answer_404(api, psa):
    """The counterpart, so the test above distinguishes "exists but unauthenticated" from
    "does not exist" rather than accepting any non-404."""
    api.cookies.clear()

    assert api.get("/api/silos/psa/no-such-endpoint").status_code == 404


def test_the_configured_process_is_not_reconfigured_on_every_request(api, psa):
    """`_ensure_configured` returns early when a config is installed. Rebuilding it per
    request would swap the scratch paths mid-flight, and a download id registered against the
    old ones would stop resolving."""
    api.get("/api/silos/psa/health")
    first = psa["config"].config()

    api.get("/api/silos/psa/programs")

    assert psa["config"].config() == first
