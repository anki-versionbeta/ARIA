"""ATR end to end: fetch -> generate -> edit -> finalize, through the real stages.

No database and no network. The warehouse connection is injected, so the four stages run
against a fake cursor and produce genuine PDF and DOCX bytes via reportlab and python-docx.

The point of this file is the seam: that the ported builders, the audit trail and the
platform's checkpoint/pause machinery actually fit together — which the unit tests for each
piece cannot show on their own.
"""

from __future__ import annotations

import contextlib
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
    not (SILO_DIR / "atr_pipeline.py").is_file(), reason="the ATR pipeline is not present"
)

# One header row, two methods, four results across two samples — enough to exercise the
# pivot, the N/A fill, the batch matrix and a review flag.
ROWS = {
    "header": [{"project_code": "ABBV-000", "batch_sample_ids": "BA00006111"}],
    "methods": [
        {"eln_unique_id": "EXP-1", "test_description": "SoloVPE", "test_method_reference": "P-1 V8.0"},
        {"eln_unique_id": "EXP-2", "test_description": "Compendial", "test_method_reference": "P-500488"},
    ],
    "results": [
        {"experiment_id": "EXP-1", "technique": "SoloVPE", "result_name": "Protein content",
         "analyte_name": None, "sample_id": "S1", "sample_name": "Day0", "batch_id": "B1",
         "detail_context": None, "reported_value": "12.345", "value_source": "reported",
         "units": "mg/mL", "particle_size_um": None, "timepoint_parsed": None,
         "storage_condition": None},
        {"experiment_id": "EXP-1", "technique": "SoloVPE", "result_name": "Protein content",
         "analyte_name": None, "sample_id": "S2", "sample_name": "Day1", "batch_id": "B1",
         "detail_context": None, "reported_value": "12.9", "value_source": "actual",
         "units": "mg/mL", "particle_size_um": None, "timepoint_parsed": None,
         "storage_condition": None},
        {"experiment_id": "EXP-2", "technique": "Compendial", "result_name": "pH value",
         "analyte_name": None, "sample_id": "S1", "sample_name": "Day0", "batch_id": "B1",
         "detail_context": None, "reported_value": "6.8", "value_source": "reported",
         "units": "", "particle_size_um": None, "timepoint_parsed": None,
         "storage_condition": None},
        {"experiment_id": "EXP-2", "technique": "Compendial", "result_name": "pH value",
         "analyte_name": None, "sample_id": "S2", "sample_name": "Day1", "batch_id": "B1",
         "detail_context": None, "reported_value": None, "value_source": "reported",
         "units": "", "particle_size_um": None, "timepoint_parsed": None,
         "storage_condition": None},
    ],
    "scope_summary": [{"scope": "Assess release testing.", "summary_conclusion": "All results conform."}],
    "validation": [{"result_rows": 4, "n_experiments": 2, "n_samples": 2, "n_batches": 1}],
    "batch_lots": [{"batch_id": "B1", "lot_display": "1001707466"}],
}


@pytest.fixture(scope="module")
def mfg():
    module = _load_module("mfg_atr", SILO_DIR / "silo.py")
    for name in (
        "config", "mfgr_config", "ids", "workspace", "atr_data", "audit",
        "atr_report", "docx_common", "atr_pdf", "atr_docx", "finalize", "atr_pipeline",
    ):
        _load_module("mfg_atr", SILO_DIR / f"{name}.py", name=name)
    return module


class FakeCursor:
    """Returns the row set matching whichever query is running, keyed by a marker in the SQL."""

    def __init__(self):
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **binds):
        if "atr_section" in sql:
            self._rows = ROWS["header"]
        elif "test_method_reference" in sql:
            self._rows = ROWS["methods"]
        elif "particle_size_um" in sql:
            self._rows = ROWS["results"]
        elif "summary_conclusion" in sql:
            self._rows = ROWS["scope_summary"]
        elif "result_rows" in sql:
            self._rows = ROWS["validation"]
        else:
            self._rows = ROWS["batch_lots"]
        self.description = [(k.upper(),) for k in (self._rows[0] if self._rows else {})]
        return self

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]


class FakeWarehouse:
    schema = "DEVSCI_DM"
    results_object = "mv_combined_results"
    results_view = "DEVSCI_DM.mv_combined_results"

    def __init__(self):
        self.connections = 0

    @contextlib.contextmanager
    def connect(self):
        self.connections += 1

        class Conn:
            def cursor(self_inner):
                return FakeCursor()

        yield Conn()

    def reachable(self):
        return True


def atr_run(session, owner_id: str):
    """A queued run shaped the way this silo's router creates them: no files, and the
    request parameters on `meta`.

    `status="queued"` matters — conftest's `make_run` attaches an *output* file as well
    when the status is "complete", and this silo produces its own outputs.
    """
    run = make_run(
        session, owner_id=owner_id, title="ATR CMC-10352", silo="mfg_atr", status="queued"
    )
    run.stage = None
    run.meta = {
        "report_type": "atr",
        "identifier": "CMC-10352",
        "ids": {"short": "10352", "display": "CMC-10352", "query": "PEGA-PROD-CMC-10352"},
        "source": "live",
    }
    # make_run always attaches an input document; this silo has none to attach.
    for record in list(run.files):
        session.delete(record)
    session.commit()
    # `files` is a selectin relationship and was already loaded, so it would otherwise
    # still report the deleted row and every filename assertion below would see it.
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
    """The run's output files, freshly loaded, keyed by filename.

    The platform's sessionmaker sets `expire_on_commit=False`, so the commit inside
    `attach_output` leaves an already-loaded `files` collection stale. The runner refreshes
    for the same reason — see the note in `engine/queue.py`.
    """
    session.refresh(run)
    return {f.filename: f for f in run.files}


# ── the stages, in order ──────────────────────────────────────────────────────


def test_fetch_queries_the_warehouse_and_keeps_the_snapshot(client, session, mfg):
    asha = login(client)
    run = atr_run(session, asha["id"])
    warehouse = FakeWarehouse()
    ctx = context(session, run, warehouse)

    out = mfg.fetch(run, ctx)

    assert warehouse.connections == 1, "one connection per run, as the source did"
    assert out["has_data"] is True
    assert out["row_counts"]["results"] == 4
    # The snapshot is what finalize rebuilds from, so it must round-trip.
    with ctx.storage.open_key(out["raw_key"]) as handle:
        assert json.loads(handle.read().decode("utf-8"))["validation"][0]["result_rows"] == 4


def test_generate_produces_a_pdf_and_a_docx(client, session, mfg):
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())

    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    out = mfg.generate(run, ctx)

    assert out["generated"] is True
    names = files_of(session, run)
    assert "ATR_CMC-10352.pdf" in names
    assert "ATR_CMC-10352.docx" in names

    pdf = ctx.storage.open_key(names["ATR_CMC-10352.pdf"].storage_key).read()
    docx = ctx.storage.open_key(names["ATR_CMC-10352.docx"].storage_key).read()
    assert pdf.startswith(b"%PDF-"), "a real reportlab PDF"
    assert docx[:2] == b"PK", "a real docx (zip)"


def test_the_generated_pdf_carries_the_five_editable_form_fields(client, session, mfg):
    """The whole edit flow binds to these names, so they are asserted on the real bytes."""
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    mfg.generate(run, ctx)

    key = files_of(session, run)["ATR_CMC-10352.pdf"].storage_key
    raw = ctx.storage.open_key(key).read()

    for field in (b"regulatory", b"hqc_spec_id", b"hqc_assessment", b"hqc_remarks", b"hqc_remarks_na"):
        assert field in raw, f"{field!r} is missing from the AcroForm"


def test_the_docx_contains_the_shaped_content(client, session, mfg):
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    mfg.generate(run, ctx)

    key = files_of(session, run)["ATR_CMC-10352.docx"].storage_key
    import io

    with zipfile.ZipFile(io.BytesIO(ctx.storage.open_key(key).read())) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")

    assert "All results conform." in xml, "the summary conclusion is rendered"
    assert "CMC-10352" in xml


def test_the_archival_tree_keeps_the_source_layout(client, session, mfg):
    """The `<prefix>/pdf/` + `/docx/` tree is a compliance artefact reviewers navigate, so
    the *keys* are asserted, not just that some bytes were written.

    This is what an earlier version got wrong: it archived through `ctx.storage.put_media`,
    whose `run_key` reduces a name to its basename, so the whole tree collapsed to
    `ATR_MFG/extracts/<run_id>_ATR_CMC-10352_preview.pdf`. Tests that only checked
    `run.files` could not see it.
    """
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    prefix = out["prefix"]
    assert prefix.startswith("ATR_MFG/Downloads/ATR_"), prefix
    assert prefix.endswith("_CMC-10352"), prefix

    pdf = ctx.storage.open_key(f"{prefix}/pdf/ATR_CMC-10352_preview.pdf").read()
    docx = ctx.storage.open_key(f"{prefix}/docx/ATR_CMC-10352_preview.docx").read()
    assert pdf.startswith(b"%PDF-")
    assert docx[:2] == b"PK"


def test_the_archival_tree_keeps_both_the_clean_and_the_edited_copy(client, session, mfg):
    """`_preview` is the clean report and `_edited` is what the author signed off. Both are
    retained, in the same run folder."""
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    generated = mfg.generate(run, ctx)
    queue.save_checkpoint(session, run.id, "generate", generated)
    queue.save_checkpoint(session, run.id, "await_edits", {"field_values": {}})

    mfg.finalize(run, ctx)

    prefix = generated["prefix"]
    for name in (
        "pdf/ATR_CMC-10352_preview.pdf",
        "docx/ATR_CMC-10352_preview.docx",
        "pdf/ATR_CMC-10352_edited.pdf",
        "docx/ATR_CMC-10352_edited.docx",
    ):
        assert ctx.storage.open_key(f"{prefix}/{name}").read(), name


def test_an_archive_key_outside_the_silo_prefix_is_refused(mfg):
    """The bypass of `ctx.storage` means the key is unchecked by the platform, so it is
    checked here — a caller's mistake must not write into another silo's area."""
    archive = _load_module("mfg_atr", SILO_DIR / "archive.py", name="archive")

    with pytest.raises(ValueError, match="outside this silo's prefix"):
        archive.put("BOP/Downloads/whatever.pdf", b"x")


def test_the_audit_record_is_written_with_the_resolved_sql(client, session, mfg):
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    record = json.loads(ctx.storage.open_key(out["audit_key"]).read().decode("utf-8"))
    assert record["request_id"]["display"] == "CMC-10352"
    assert record["bind_values"] == {"request_id": "PEGA-PROD-CMC-10352"}
    assert "DEVSCI_DM.mv_combined_results" in record["queries"]["results"]
    assert len(record["source_data_sha256"]) == 64
    assert record["app_user"]["username"] == "asha.rao", "the author, not the worker account"
    assert record["finalized"] is False


def test_a_missing_value_reaches_the_run_as_a_review_flag(client, session, mfg):
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))

    out = mfg.generate(run, ctx)

    assert any("Missing value" in f["message"] for f in out["flags"])


def test_finalize_rebuilds_from_the_snapshot_and_applies_the_values(client, session, mfg):
    """No cache: the report is rebuilt, which is what removes the original's expiry failure."""
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())

    fetched = mfg.fetch(run, ctx)
    queue.save_checkpoint(session, run.id, "fetch", fetched)
    queue.save_checkpoint(session, run.id, "generate", mfg.generate(run, ctx))
    queue.save_checkpoint(
        session,
        run.id,
        "await_edits",
        {"field_values": {"regulatory": "Yes", "hqc_assessment": "meets",
                          "hqc_spec_id": "SPEC-1", "hqc_remarks_na": "on"}},
    )

    out = mfg.finalize(run, ctx)

    assert out["finalized"] is True
    assert out["pdf_bytes"] > 1000 and out["docx_bytes"] > 1000
    edited = {name for name in files_of(session, run) if "edited" in name}
    assert edited == {"ATR_CMC-10352_edited.pdf", "ATR_CMC-10352_edited.docx"}


def test_the_finalized_pdf_is_locked(client, session, mfg):
    """The archival copy freezes the fields and restricts permissions — a record lock."""
    asha = login(client)
    run = atr_run(session, asha["id"])
    ctx = context(session, run, FakeWarehouse())
    queue.save_checkpoint(session, run.id, "fetch", mfg.fetch(run, ctx))
    queue.save_checkpoint(session, run.id, "generate", mfg.generate(run, ctx))
    queue.save_checkpoint(session, run.id, "await_edits", {"field_values": {}})
    mfg.finalize(run, ctx)

    key = files_of(session, run)["ATR_CMC-10352_edited.pdf"].storage_key
    raw = ctx.storage.open_key(key).read()

    assert raw.startswith(b"%PDF-")
    assert b"/Encrypt" in raw, "the locked copy is permission-restricted"


def test_field_values_map_onto_the_builders_defaults(mfg):
    """The form field names differ from the dataclass names, and that mapping is the
    contract the front end binds to."""
    pipeline = _load_module("mfg_atr", SILO_DIR / "atr_pipeline.py", name="atr_pipeline")

    defaults = pipeline.defaults_from_fields(
        {
            "regulatory": " Yes ",
            "hqc_spec_id": " SPEC-1 ",
            "hqc_assessment": "not_meets",
            "hqc_remarks": " see section 4 ",
            "hqc_remarks_na": "checked",
        }
    )

    assert defaults.regulatory == "Yes"
    assert defaults.spec_id == "SPEC-1"
    assert defaults.hqc == "not_meets"
    assert defaults.remarks == "see section 4"
    assert defaults.remarks_na is True


@pytest.mark.parametrize(
    "value,expected",
    [("yes", True), ("on", True), ("true", True), ("1", True), ("checked", True),
     ("", False), ("no", False), ("off", False)],
)
def test_the_na_checkbox_accepts_the_source_truthy_values(mfg, value, expected):
    pipeline = _load_module("mfg_atr", SILO_DIR / "atr_pipeline.py", name="atr_pipeline")
    assert pipeline.defaults_from_fields({"hqc_remarks_na": value}).remarks_na is expected


# ── the no-data path ──────────────────────────────────────────────────────────


def test_an_empty_warehouse_result_completes_rather_than_failing(client, session, mfg):
    """A valid id with no rows is not an error — a failed run would invite a retry that
    cannot help."""
    asha = login(client)
    run = atr_run(session, asha["id"])

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
    assert "No data found" in generated["message"]

    # Nothing to review, so the pause is skipped and the run can finish.
    assert mfg.await_edits(run, ctx) == {"skipped": True, "reason": "no_data"}


# ── through the real runner ────────────────────────────────────────────────────


def test_the_run_parks_for_the_author_then_completes(client, session, mfg, monkeypatch):
    """The whole flow driven by the platform's own runner, including the pause."""
    asha = login(client)
    run = atr_run(session, asha["id"])

    from api.backend.da_platform.engine import context as context_module

    monkeypatch.setattr(context_module, "get_warehouse", lambda: FakeWarehouse())
    silo = _build("mfg_atr", mfg)

    claimed = queue.claim(session, "worker-1")
    assert claimed.id == run.id
    assert runner.run_one(session, claimed, silo) == "awaiting_user"
    session.refresh(run)
    assert run.stage == "await_edits"

    queue.complete_paused_stage(session, run, {"field_values": {"regulatory": "No"}})

    claimed = queue.claim(session, "worker-2")
    assert runner.run_one(session, claimed, silo) == "complete"
    session.refresh(run)
    assert run.progress_pct == 100
    assert {f.filename for f in run.files} >= {
        "ATR_CMC-10352.pdf",
        "ATR_CMC-10352.docx",
        "ATR_CMC-10352_edited.pdf",
        "ATR_CMC-10352_edited.docx",
    }
