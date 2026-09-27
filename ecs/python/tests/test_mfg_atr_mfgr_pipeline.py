"""MFGR end to end: fetch -> generate -> edit -> finalize, through the real stages.

No database and no network. The warehouse connection is injected, so the four stages run
against a fake cursor and produce genuine PDF and DOCX bytes via reportlab and python-docx.

The point of this file is the seam. Two MFGR-specific behaviours matter most and neither is
visible in a unit test of the shaping code:

* the batch's registration namespace is **resolved against the warehouse** before the pack
  runs, on the same connection, and everything downstream must use the root that was
  actually queried;
* the validation query is **not** run on the report path.
"""

from __future__ import annotations

import contextlib
import io
import json
import zipfile

import pytest

from api.backend.da_platform.engine import queue, runner
from api.backend.da_platform.engine.context import StageContext
from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _build, _load_module
from tests.conftest import login, make_run

SILO_DIR = BACKEND_ROOT / "silos" / "mfg_atr"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "mfgr_pipeline.py").is_file(), reason="the MFGR pipeline is not present"
)

MILLIL = "http://qudt.org/vocab/unit/MilliL"
MG_PER_ML = "http://qudt.org/vocab/unit/MilliGM-PER-MilliL"

# The namespace the warehouse reports, deliberately *not* the one normalize_batch_id
# assumes ("nest-br-prod-BAX000584") — this is the external-manufacture case.
RESOLVED_ROOT = "nest-br-ext-dev-BAX000584"

STAGE = {
    "batch_id_stage": f"{RESOLVED_ROOT}-01",
    "batch_name": "ABBV-423 360 mg S.INJ 2 mL Vial",
    "project_code": "ABBV-423",
    "batch_type": "Clinical",
    "eln_url": "https://eln.abbvie.com/e?entityId=b988a640214011f18c8d00000a4818a8",
    "mfg_start": "2026-03-01",
    "mfg_stop": "2026-03-02",
    "manufactured_by": "AbbVie Ludwigshafen",
}

GENERAL = {
    "batch_id_stage": f"{RESOLVED_ROOT}-01",
    "batch_size": "500",
    "batch_size_unit": MILLIL,
    "dose_form": "Solution",
    "dose_strength": "180",
    "dose_strength_unit": MG_PER_ML,
    "density": "1.02",
    "density_unit": "http://qudt.org/vocab/unit/GM-PER-MilliL",
    "ph": "6.0",
    "packaging_system": "6R Vial",
    "unit_operation_uri": "https://ontology.abbvienet.com/dsdt/DSDT_0000360",
    "amount": "500",
    "amount_unit": MILLIL,
}

ROWS = {
    "header": [STAGE],
    "general_info": [GENERAL, dict(GENERAL, batch_id_stage=f"{RESOLVED_ROOT}-02", amount="450")],
    "composition": [
        {"component_name": "ABBV-423", "component_function": "Active",
         "amount": "180", "amount_unit": MG_PER_ML},
        {"component_name": "Water for Injection", "component_function": "Solvent",
         "amount": "", "amount_unit": ""},
    ],
    "batch_components": [
        {"batch_id_stage": f"{RESOLVED_ROOT}-01", "display_name": "Drug Substance",
         "component_ref": "urn:batch:idbsnongxp-dev-1429707", "resolved_name": None},
        {"batch_id_stage": f"{RESOLVED_ROOT}-02", "display_name": None,
         "component_ref": "urn:batch:L-0001113",
         "resolved_name": "Vial, Schott, 6R, NBB, clear glass"},
    ],
    "process_steps": [
        {"stage_id": f"{RESOLVED_ROOT}-01", "unit_operation": "Sterilization by Filtration"},
        {"stage_id": f"{RESOLVED_ROOT}-02", "unit_operation": "Filling"},
    ],
    "material_props": [
        {"parameter_ref": "urn:parameter:nominalVolume", "actual_value": "2.00",
         "actual_value_unit": ""},
        {"parameter_ref": "urn:parameter:fillVolume", "actual_value": "2.37",
         "actual_value_unit": ""},
    ],
    "quality_standard": [
        {"ref_id": "L-1", "component_name": "ABBV-423", "quality_standard": "Ph. Eur."}
    ],
}


@pytest.fixture(scope="module")
def mfg():
    module = _load_module("mfg_atr", SILO_DIR / "silo.py")
    for name in (
        "config", "mfgr_config", "ids", "workspace", "audit", "docx_common", "finalize",
        "mfgr_data", "mfgr_report", "mfgr_pdf", "mfgr_docx", "mfgr_pipeline",
    ):
        _load_module("mfg_atr", SILO_DIR / f"{name}.py", name=name)
    return module


class FakeCursor:
    """Returns the row set matching whichever query is running, keyed by a marker in the
    SQL. Also records which queries ran, so the test can prove the validation pack was
    skipped."""

    def __init__(self, seen: list[str], resolved: str | None):
        self._rows: list[dict] = []
        self._seen = seen
        self._resolved = resolved
        self._scalar: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **binds):
        self._scalar = None
        if "s" in binds and "FETCH FIRST 1 ROWS ONLY" in sql:
            self._seen.append("resolve")
            self._scalar = (f"{self._resolved}-01",) if self._resolved else None
            self._rows = []
            self.description = [("BATCH_ID",)]
            return self

        name = self._classify(sql)
        self._seen.append(name)
        self._rows = ROWS.get(name, [])
        self.description = [(k.upper(),) for k in (self._rows[0] if self._rows else {})]
        return self

    @staticmethod
    def _classify(sql: str) -> str:
        # Most specific first: general_info and header both select from the same view.
        for marker, name in (
            ("n_formulation_rows", "validation"),
            ("experimenturl_actual_value", "header"),
            ("packagingsystem_actual_value", "general_info"),
            ("material_display_name", "composition"),
            ("resolved_name", "batch_components"),
            ("clean_stage", "process_steps"),
            ("material_properties", "material_props"),
            ("qualityStandardRef", "quality_standard"),
        ):
            if marker in sql:
                return name
        return "unknown"

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def fetchone(self):
        return self._scalar


class FakeWarehouse:
    schema = "DEVSCI_DM"
    results_object = "mv_combined_results"
    results_view = "DEVSCI_DM.mv_combined_results"

    def __init__(self, resolved: str | None = RESOLVED_ROOT):
        self.connections = 0
        self.seen: list[str] = []
        self._resolved = resolved

    @contextlib.contextmanager
    def connect(self):
        self.connections += 1
        outer = self

        class Conn:
            def cursor(self_inner):
                return FakeCursor(outer.seen, outer._resolved)

        yield Conn()

    def reachable(self):
        return True


def mfgr_run(session, owner_id: str):
    """A queued run shaped the way this silo's router creates them: no files, and the
    request parameters on `meta`."""
    run = make_run(
        session, owner_id=owner_id, title="MFGR BAX000584", silo="mfg_atr", status="queued"
    )
    run.stage = None
    run.meta = {
        "report_type": "mfgr",
        "identifier": "BAX000584",
        "ids": {
            "short": "BAX000584",
            "display": "BAX000584",
            "query": "nest-br-prod-BAX000584",
            "registration": "nest",
        },
        "source": "live",
    }
    for record in list(run.files):
        session.delete(record)
    session.commit()
    session.refresh(run)
    assert run.files == [], "the fixture run must start with no files"
    return run


def context(session, run, warehouse):
    silo = _load_module("mfg_atr", SILO_DIR / "silo.py")
    return StageContext(
        session,
        run,
        storage_prefix=silo.STORAGE_PREFIX,
        storage_folders=silo.STORAGE_FOLDERS,
        warehouse=warehouse,
    )


def files_of(session, run) -> dict:
    """The run's output files, freshly loaded. `expire_on_commit=False` means the collection
    loaded before `attach_output` stays stale; the runner refreshes for the same reason."""
    session.refresh(run)
    return {f.filename: f for f in run.files}


# ── the stages, in order ──────────────────────────────────────────────────────


def test_fetch_resolves_the_registration_namespace_from_the_warehouse(client, session, mfg):
    """`normalize_batch_id` assumes 'nest-br-prod-', which is wrong for externally
    manufactured batches. The source resolves the real root first, and the resolved value is
    what every later stage must use."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    warehouse = FakeWarehouse()
    ctx = context(session, run, warehouse)

    out = mfg.fetch(run, ctx)

    assert warehouse.connections == 1, "one connection for the resolve and the pack"
    assert warehouse.seen[0] == "resolve", "the namespace is resolved before the pack runs"
    assert out["ids"]["query"] == RESOLVED_ROOT
    assert out["ids"]["short"] == "BAX000584", "the short id is untouched"


def test_fetch_keeps_the_assumed_root_when_the_warehouse_cannot_resolve_it(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse(resolved=None))

    out = mfg.fetch(run, ctx)

    assert out["ids"]["query"] == "nest-br-prod-BAX000584"


def test_fetch_skips_the_validation_query(client, session, mfg):
    """Its six COUNT(*) sub-selects are the slowest part of the pack and the report does not
    read them. This was the source's main per-request speed-up."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    warehouse = FakeWarehouse()
    ctx = context(session, run, warehouse)

    out = mfg.fetch(run, ctx)

    assert "validation" not in warehouse.seen
    assert "validation" not in out["row_counts"]
    assert out["row_counts"]["general_info"] == 2


def test_fetch_keeps_the_snapshot(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())

    out = mfg.fetch(run, ctx)

    assert out["has_data"] is True
    with ctx.storage.open_key(out["raw_key"]) as handle:
        snapshot = json.loads(handle.read().decode("utf-8"))
    assert snapshot["header"][0]["project_code"] == "ABBV-423"


def test_generate_produces_a_pdf_and_a_docx(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())

    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    out = mfg.generate(run, ctx)

    assert out["generated"] is True
    names = files_of(session, run)
    assert "MFGR_BAX000584.pdf" in names
    assert "MFGR_BAX000584.docx" in names

    pdf = ctx.storage.open_key(names["MFGR_BAX000584.pdf"].storage_key).read()
    docx = ctx.storage.open_key(names["MFGR_BAX000584.docx"].storage_key).read()
    assert pdf.startswith(b"%PDF-"), "a real reportlab PDF"
    assert docx[:2] == b"PK", "a real docx (zip)"


def test_the_generated_pdf_carries_the_editable_form_fields(client, session, mfg):
    """The edit flow binds to these names, so they are asserted on the real bytes."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    mfg.generate(run, ctx)

    raw = ctx.storage.open_key(files_of(session, run)["MFGR_BAX000584.pdf"].storage_key).read()

    for field in (b"clinical_phase", b"ds_quality", b"storage_temp", b"conclusion"):
        assert field in raw, f"{field!r} is missing from the AcroForm"


def test_the_docx_contains_the_shaped_content(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    mfg.generate(run, ctx)

    key = files_of(session, run)["MFGR_BAX000584.docx"].storage_key
    with zipfile.ZipFile(io.BytesIO(ctx.storage.open_key(key).read())) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")

    assert "BAX000584" in xml
    assert "ABBV-423" in xml, "the project code is rendered"


def test_the_archival_tree_keeps_the_source_layout(client, session, mfg):
    """Same compliance tree as ATR, keyed by the short batch id. The keys are asserted
    because an earlier version archived through `ctx.storage.put_media`, which reduces a name
    to its basename and collapsed the whole tree into the media folder."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    prefix = out["prefix"]
    assert prefix.startswith("ATR_MFG/Downloads/MFGR_"), prefix
    assert prefix.endswith("_BAX000584"), prefix

    assert ctx.storage.open_key(f"{prefix}/pdf/MFGR_BAX000584_preview.pdf").read()[:5] == b"%PDF-"
    assert ctx.storage.open_key(f"{prefix}/docx/MFGR_BAX000584_preview.docx").read()[:2] == b"PK"


def test_the_archival_tree_keeps_both_the_clean_and_the_edited_copy(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    generated = mfg.generate(run, ctx)
    queue.save_checkpoint(session, run.id, "generate", generated)
    queue.save_checkpoint(session, run.id, "await_edits", {"field_values": {}})

    mfg.finalize(run, ctx)

    prefix = generated["prefix"]
    for name in (
        "pdf/MFGR_BAX000584_preview.pdf",
        "docx/MFGR_BAX000584_preview.docx",
        "pdf/MFGR_BAX000584_edited.pdf",
        "docx/MFGR_BAX000584_edited.docx",
    ):
        assert ctx.storage.open_key(f"{prefix}/{name}").read(), name


def test_the_audit_record_names_the_root_that_was_actually_queried(client, session, mfg):
    """The bind is `batch_root`, not `batch_id` — and its value is the *resolved* namespace.
    Recording the assumed root would name a bind the query never used."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    record = json.loads(ctx.storage.open_key(out["audit_key"]).read().decode("utf-8"))
    assert record["batch_id"] == {"short": "BAX000584", "query": RESOLVED_ROOT}
    assert record["bind_values"] == {"batch_root": RESOLVED_ROOT}
    assert "DEVSCI_DM.view_batch_properties" in record["queries"]["header"]
    assert len(record["source_data_sha256"]) == 64
    assert record["app_user"]["username"] == "asha.rao", "the author, not the worker account"
    assert record["finalized"] is False


def test_the_audit_record_carries_every_query_including_validation(client, session, mfg):
    """The report path skips running it, but the audited pack is the whole pack — the record
    describes the queries the silo holds, and dropping one would understate it."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    record = json.loads(ctx.storage.open_key(out["audit_key"]).read().decode("utf-8"))
    assert set(record["queries"]) == {
        "header", "general_info", "composition", "batch_components",
        "process_steps", "material_props", "quality_standard", "validation",
    }


def test_finalize_rebuilds_from_the_snapshot_and_applies_the_values(client, session, mfg):
    """No cache: the report is rebuilt, which is what removes the original's expiry failure."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())

    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    queue.save_checkpoint(session, run.id, "generate", mfg.generate(run, ctx))
    queue.save_checkpoint(
        session,
        run.id,
        "await_edits",
        {"field_values": {"clinical_phase": "Phase 1", "storage_temp": "2-8 C",
                          "conclusion": "Batch conforms."}},
    )

    out = mfg.finalize(run, ctx)

    assert out["finalized"] is True
    assert out["pdf_bytes"] > 1000 and out["docx_bytes"] > 1000
    edited = {name for name in files_of(session, run) if "edited" in name}
    assert edited == {"MFGR_BAX000584_edited.pdf", "MFGR_BAX000584_edited.docx"}


def test_the_authors_values_reach_the_edited_docx(client, session, mfg):
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    queue.save_checkpoint(session, run.id, "generate", mfg.generate(run, ctx))
    queue.save_checkpoint(
        session, run.id, "await_edits", {"field_values": {"conclusion": "Batch conforms."}}
    )
    mfg.finalize(run, ctx)

    key = files_of(session, run)["MFGR_BAX000584_edited.docx"].storage_key
    with zipfile.ZipFile(io.BytesIO(ctx.storage.open_key(key).read())) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")

    assert "Batch conforms." in xml


def test_the_finalized_pdf_is_locked(client, session, mfg):
    """The archival copy freezes the fields and restricts permissions — a record lock."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    queue.save_checkpoint(session, run.id, "generate", mfg.generate(run, ctx))
    queue.save_checkpoint(session, run.id, "await_edits", {"field_values": {}})
    mfg.finalize(run, ctx)

    key = files_of(session, run)["MFGR_BAX000584_edited.pdf"].storage_key
    raw = ctx.storage.open_key(key).read()

    assert raw.startswith(b"%PDF-")
    assert b"/Encrypt" in raw, "the locked copy is permission-restricted"


# ── the manual field contract ─────────────────────────────────────────────────


def test_field_values_map_one_to_one_onto_the_builders_defaults(mfg):
    """Unlike ATR, MFGR's form field names are the dataclass field names."""
    pipeline = _load_module("mfg_atr", SILO_DIR / "mfgr_pipeline.py", name="mfgr_pipeline")

    defaults = pipeline.defaults_from_fields(
        {"clinical_phase": "  Phase 1  ", "ds_quality": " GMP ", "conclusion": " conforms "}
    )

    assert defaults.clinical_phase == "Phase 1"
    assert defaults.ds_quality == "GMP"
    assert defaults.conclusion == "conforms"


def test_an_omitted_field_keeps_the_dataclass_default_rather_than_being_blanked(mfg):
    """The source only passes keys that are present, so a field the form did not send is
    left alone instead of being overwritten with an empty string."""
    pipeline = _load_module("mfg_atr", SILO_DIR / "mfgr_pipeline.py", name="mfgr_pipeline")

    defaults = pipeline.defaults_from_fields({"clinical_phase": "Phase 1"})

    assert defaults.storage_temp == ""


def test_an_unknown_field_name_is_ignored(mfg):
    """A stray key must not raise — `ManualDefaults(**fv)` would."""
    pipeline = _load_module("mfg_atr", SILO_DIR / "mfgr_pipeline.py", name="mfgr_pipeline")

    assert pipeline.defaults_from_fields({"not_a_field": "x"}).clinical_phase == ""


# ── the no-data path ──────────────────────────────────────────────────────────


def test_an_empty_warehouse_result_completes_rather_than_failing(client, session, mfg):
    """This gate has no counterpart in the source, which would emit a fully blank MFGR plus
    an audit record asserting it came from this batch. Declining is the safer failure, and
    the run still completes rather than inviting a retry that cannot help."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])

    class Empty(FakeWarehouse):
        @contextlib.contextmanager
        def connect(self):
            class Cur:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql, **binds):
                    self_inner.description = [("X",)]
                    return self_inner

                def fetchall(self_inner):
                    return []

                def fetchone(self_inner):
                    return None

            class Conn:
                def cursor(self_inner):
                    return Cur()

            yield Conn()

    ctx = context(session, run, Empty())
    fetched = mfg.fetch(run, ctx)
    queue.save_checkpoint(session, run.id, "fetch", fetched)

    assert fetched["has_data"] is False

    generated = mfg.generate(run, ctx)
    queue.save_checkpoint(session, run.id, "generate", generated)
    assert generated["generated"] is False
    assert generated["reason"] == "no_data"

    assert mfg.await_edits(run, ctx) == {"skipped": True, "reason": "no_data"}


# ── through the real runner ────────────────────────────────────────────────────


def test_the_run_parks_for_the_author_then_completes(client, session, mfg, monkeypatch):
    """The whole flow driven by the platform's own runner, including the pause."""
    asha = login(client)
    run = mfgr_run(session, asha["id"])

    from api.backend.da_platform.engine import context as context_module

    monkeypatch.setattr(context_module, "get_warehouse", lambda: FakeWarehouse())
    silo = _build("mfg_atr", mfg)

    claimed = queue.claim(session, "worker-1")
    assert claimed.id == run.id
    assert runner.run_one(session, claimed, silo) == "awaiting_user"
    session.refresh(run)
    assert run.stage == "await_edits"

    queue.complete_paused_stage(
        session, run, {"field_values": {"clinical_phase": "Phase 1"}}
    )

    claimed = queue.claim(session, "worker-2")
    assert runner.run_one(session, claimed, silo) == "complete"
    session.refresh(run)
    assert run.progress_pct == 100
    assert {f.filename for f in run.files} >= {
        "MFGR_BAX000584.pdf",
        "MFGR_BAX000584.docx",
        "MFGR_BAX000584_edited.pdf",
        "MFGR_BAX000584_edited.docx",
    }
