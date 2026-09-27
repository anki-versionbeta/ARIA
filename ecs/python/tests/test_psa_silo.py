"""The PSA silo contract: what `fetch` hands to `report` (`silos/psa/silo.py`).

`tests/test_psa_plumbing.py` proves the silo is *discoverable* and that a run can be created
from a program code. It never calls a stage. This file calls both, with every module they
delegate to replaced.

That substitution is the point, not a convenience. `silo.py` owns exactly four decisions and
nothing else:

  * that `fetch` is the ONLY stage which touches the network, and that it captures the sheet
    and the photo together — the property every other stage's determinism rests on;
  * what goes into each checkpoint, because a checkpoint is the contract between stages and a
    resume on another worker has nothing else;
  * that a Smartsheet `SystemExit` becomes a `RuntimeError` before it can unwind the worker;
  * that the progress ladder is read from `PCT` rather than counted per stage.

`snapshot`, `engines`, `ingest_smartsheet_api` and `aria` are separately tested, so letting the
real ones run here would mean a failure could be theirs. With them stubbed, a failure is the
wiring — which is the only thing this file is for.

The storage, checkpoint and progress seams are real: a genuine `StageContext` over the test
session and object store. Faking those would fake the thing most likely to drift, and the
snapshot round trip through `put_media` / `open_key` is half of what `report` depends on.
"""

from __future__ import annotations

import json

import pytest

from api.backend.da_platform.engine.context import StageContext
from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from api.backend.da_platform.storage import get_object_store
from tests.conftest import login, make_run

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)

SHEET = {
    "name": "NBE (Vials)",
    "columns": [{"id": 1, "title": "Program"}, {"id": 2, "title": "Vial"}],
    "rows": [{"rowNumber": 20, "cells": []}, {"rowNumber": 21, "cells": []}],
}


def snapshot_bytes(sheet=None, sheet_id="SHEET-1") -> bytes:
    """A snapshot in the exact shape `snapshot.capture_bytes` produces."""
    return json.dumps(
        {"sheet_id": sheet_id, "sheet": sheet if sheet is not None else SHEET},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture(scope="module")
def silo():
    return _load_module("psa", SILO_DIR / "silo.py")


@pytest.fixture
def delegates(silo, monkeypatch):
    """Every module a stage imports, with a recording stub in place of each.

    Returns the modules so a test can override one reply, plus a `calls` list recording the
    order the stages reached them in — which is the ordering half of the contract.

    The stages import these *inside* the function body, so the patch has to land on the
    already-imported module object in `sys.modules` rather than on a name in `silo.py`.
    """
    from da_silos.psa import aria, engines, ingest_smartsheet_api, snapshot

    calls: list[str] = []

    def record(name, result):
        def call(*args, **kwargs):
            calls.append(name)
            if callable(result):
                return result(*args, **kwargs)
            return result
        return call

    monkeypatch.setattr(aria, "install", record("install", None))
    monkeypatch.setattr(snapshot, "capture_bytes", record("capture_bytes", snapshot_bytes))
    monkeypatch.setattr(
        snapshot, "describe",
        record("describe", {"sheet_id": "SHEET-1", "sheet_name": "NBE (Vials)",
                            "columns": 2, "rows": 2, "bytes": 128}),
    )
    monkeypatch.setattr(snapshot, "rebuild", record("rebuild", {"products": 2}))
    monkeypatch.setattr(
        ingest_smartsheet_api, "capture_row_image", record("capture_row_image", None)
    )
    monkeypatch.setattr(
        engines, "generate_report",
        record("generate_report", {"status": "ok", "verify_ok": True,
                                   "output_exists": False}),
    )
    return {"calls": calls, "aria": aria, "snapshot": snapshot,
            "engines": engines, "ingest_api": ingest_smartsheet_api}


def psa_run(session, owner_id: str, **meta):
    """A PSA run with no input file, as `runs.py` creates one."""
    from api.backend.da_platform.db.models import RunFile

    run = make_run(session, owner_id=owner_id, title="PSA AGN-151586", silo="psa",
                   status="queued")
    session.query(RunFile).filter(RunFile.run_id == run.id).delete()
    run.meta = {"program": "AGN-151586", "source_row": 20, "vendor": None, **meta}
    session.commit()
    session.refresh(run)
    return run


def context_for(session, run, silo) -> StageContext:
    """A real StageContext over the test session and object store."""
    return StageContext(
        session, run, store=get_object_store(),
        storage_prefix=silo.STORAGE_PREFIX, storage_folders=silo.STORAGE_FOLDERS,
    )


@pytest.fixture
def ctx(session, silo, client):
    """A context over a fresh PSA run. `client` is required so a user exists to own it."""
    asha = login(client)
    run = psa_run(session, asha["id"])
    return run, context_for(session, run, silo)


# ── fetch: the only stage that touches the network ────────────────────────────


def test_fetch_installs_the_host_configuration_first(silo, delegates, ctx):
    """A stage and an endpoint do not share a process — the API serves the router while a
    separate worker runs the stages. Nothing else configures the worker, so a stage that
    read a path before `aria.install()` would answer from the repo-relative defaults."""
    run, context = ctx

    silo.fetch(run, context)

    assert delegates["calls"][0] == "install"


def test_fetch_captures_the_sheet_and_records_where_it_went(silo, delegates, ctx):
    run, context = ctx

    result = silo.fetch(run, context)

    assert "capture_bytes" in delegates["calls"]
    assert result["snapshot_key"], "the key is how every later stage finds the bytes"
    assert result["rows"] == 2
    assert result["sheet_id"] == "SHEET-1"


def test_fetch_carries_the_parameters_into_the_checkpoint(silo, delegates, ctx):
    """`report` reads the program from the checkpoint rather than re-deriving it, so the
    two stages cannot disagree about which product the run is about."""
    run, context = ctx

    result = silo.fetch(run, context)

    assert result["program"] == "AGN-151586"
    assert result["source_row"] == 20
    assert result["vendor"] is None


def test_the_stored_snapshot_can_be_read_back_verbatim(silo, delegates, ctx):
    """The round trip through the object store is half of what `report` depends on: a key
    that stores but does not resolve would fail only at report time."""
    run, context = ctx

    result = silo.fetch(run, context)

    with context.storage.open_key(result["snapshot_key"]) as handle:
        assert handle.read() == snapshot_bytes()


def test_fetch_reports_progress_before_the_slow_read(silo, delegates, ctx):
    """The message has to appear while the read is happening, not after it — a sheet read
    is the slowest thing in the run, and a bar that only moves afterwards is no bar."""
    run, context = ctx
    seen: list[tuple[str, int | None]] = []
    context.progress = lambda message, pct=None: seen.append((message, pct))

    silo.fetch(run, context)

    assert seen[0] == (f"Reading the Smartsheet for AGN-151586", silo.PCT["fetch_start"])


def test_fetch_ends_on_the_ladders_fetch_step(silo, delegates, ctx):
    run, context = ctx
    seen: list[int | None] = []
    context.progress = lambda message, pct=None: seen.append(pct)

    silo.fetch(run, context)

    assert seen[-1] == silo.PCT["fetch_done"]


def test_fetch_counts_the_captured_rows_in_its_progress_line(silo, delegates, ctx):
    run, context = ctx
    seen: list[str] = []
    context.progress = lambda message, pct=None: seen.append(message)

    silo.fetch(run, context)

    assert "2 sheet row(s)" in seen[-1]


# ── the product photo, captured with the sheet ────────────────────────────────


def test_the_photo_is_captured_for_this_presentation_only(silo, delegates, ctx, monkeypatch):
    """A run assesses one row, and downloading the other ~36 to embed one would be waste."""
    run, context = ctx
    seen: list[int] = []

    def capture(sheet, row):
        seen.append(row)
        return ("photo.png", b"PNG-BYTES")

    monkeypatch.setattr(delegates["ingest_api"], "capture_row_image", capture)

    result = silo.fetch(run, context)

    assert seen == [20], "exactly the subject row"
    assert result["photo_bytes"] == len(b"PNG-BYTES")
    with context.storage.open_key(result["photo_key"]) as handle:
        assert handle.read() == b"PNG-BYTES"


def test_the_photos_extension_follows_the_captured_filename(silo, delegates, ctx, monkeypatch):
    """Smartsheet attachments are not all PNG, and a .jpg stored under a .png name would
    embed as a broken image rather than fail."""
    run, context = ctx
    monkeypatch.setattr(
        delegates["ingest_api"], "capture_row_image", lambda s, r: ("shot.JPG", b"JPG")
    )

    result = silo.fetch(run, context)

    assert result["photo_key"].endswith(".jpg"), "lowercased, from the filename"


def test_a_photoless_filename_falls_back_to_png(silo, delegates, ctx, monkeypatch):
    run, context = ctx
    monkeypatch.setattr(
        delegates["ingest_api"], "capture_row_image", lambda s, r: ("noextension", b"X")
    )

    assert silo.fetch(run, context)["photo_key"].endswith(".png")


def test_a_row_with_no_photo_is_recorded_rather_than_raised(silo, delegates, ctx):
    """A row with no photo is normal — the report renders a blank photo cell and
    `verify.py` accepts it, so a missing image must not fail the run."""
    run, context = ctx

    result = silo.fetch(run, context)

    assert result["photo_key"] is None
    assert result["photo_bytes"] == 0


def test_the_progress_line_says_when_there_is_no_photo(silo, delegates, ctx):
    """Whoever reads the run log should not have to wonder whether the photo failed."""
    run, context = ctx
    seen: list[str] = []
    context.progress = lambda message, pct=None: seen.append(message)

    silo.fetch(run, context)

    assert "no product photo" in seen[-1]


def test_no_photo_is_fetched_when_the_run_names_no_row(silo, delegates, ctx, session):
    """Without a row there is nothing to look up, and asking anyway would be a wasted
    authenticated download per run."""
    run, context = ctx
    run.meta = {**run.meta, "source_row": None}
    session.commit()

    result = silo.fetch(run, context)

    assert "capture_row_image" not in delegates["calls"]
    assert result["photo_key"] is None


def test_a_string_row_is_coerced_before_the_photo_lookup(silo, delegates, ctx, session,
                                                         monkeypatch):
    """The screen may send the row as a string; the lookup indexes by int."""
    run, context = ctx
    run.meta = {**run.meta, "source_row": "20"}
    session.commit()
    seen: list[int] = []
    monkeypatch.setattr(
        delegates["ingest_api"], "capture_row_image",
        lambda sheet, row: seen.append(row) or ("p.png", b"X"),
    )

    silo.fetch(run, context)

    assert seen == [20] and isinstance(seen[0], int)


# ── the SystemExit conversion, which protects every other silo ────────────────


def test_a_smartsheet_systemexit_becomes_a_runtime_error(silo, delegates, ctx, monkeypatch):
    """`ingest_smartsheet_api._fetch_sheet` reports a 401 / HTTP error / unreachable host by
    raising SystemExit — reasonable for the CLI it was written for, dangerous here.
    SystemExit derives from BaseException, NOT Exception, so a worker that wraps a stage in
    `except Exception` to mark the run failed would not catch it: the exception would unwind
    the worker itself and take every OTHER silo's queued runs down with it."""
    run, context = ctx

    def boom(*args, **kwargs):
        raise SystemExit("Smartsheet API: 401 Unauthorized")

    monkeypatch.setattr(delegates["snapshot"], "capture_bytes", boom)

    with pytest.raises(RuntimeError, match="Smartsheet capture failed"):
        silo.fetch(run, context)


def test_the_converted_error_keeps_the_original_message(silo, delegates, ctx, monkeypatch):
    """The original text is the only thing that says WHICH failure it was — an expired
    token and an unreachable host need different responses from whoever reads the log."""
    run, context = ctx

    def boom(*args, **kwargs):
        raise SystemExit("Smartsheet API: 401 Unauthorized — the token is missing")

    monkeypatch.setattr(delegates["snapshot"], "capture_bytes", boom)

    with pytest.raises(RuntimeError, match="401 Unauthorized"):
        silo.fetch(run, context)


def test_the_conversion_does_not_swallow_a_genuine_exception(silo, delegates, ctx,
                                                             monkeypatch):
    """Only SystemExit is special. Anything else must propagate unchanged, or a real bug
    would be relabelled as a capture failure and looked for in the wrong place."""
    run, context = ctx

    def boom(*args, **kwargs):
        raise KeyError("columns")

    monkeypatch.setattr(delegates["snapshot"], "capture_bytes", boom)

    with pytest.raises(KeyError):
        silo.fetch(run, context)


# ── report: rebuilt from the stored bytes, no network ─────────────────────────


def fetched(session, run, silo, context, delegates):
    """Run `fetch` and checkpoint it, so `report` has something to read."""
    from api.backend.da_platform.engine import queue

    result = silo.fetch(run, context)
    queue.save_checkpoint(session, run.id, "fetch", result)
    delegates["calls"].clear()
    return result


def test_report_rebuilds_the_catalogue_from_the_snapshot(silo, delegates, ctx, session):
    """Rebuilt from the same bytes the fetch captured, so the report and the recommendation
    of one run cannot describe different catalogues."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)

    silo.report(run, context)

    assert "rebuild" in delegates["calls"]
    assert delegates["calls"].index("rebuild") < delegates["calls"].index("generate_report")


def test_report_passes_the_stored_bytes_to_the_rebuild(silo, delegates, ctx, session,
                                                       monkeypatch):
    """Not a fresh read — the whole design is that only `fetch` touches the network."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    seen: list[bytes] = []
    monkeypatch.setattr(
        delegates["snapshot"], "rebuild", lambda raw, **kw: seen.append(raw) or {}
    )

    silo.report(run, context)

    assert seen == [snapshot_bytes()]


def test_report_never_captures_the_sheet_again(silo, delegates, ctx, session):
    """A second trip could return a different sheet than the assessment was based on."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)

    silo.report(run, context)

    assert "capture_bytes" not in delegates["calls"]


def test_report_asks_the_engine_not_to_refresh(silo, delegates, ctx, session, monkeypatch):
    """`refresh_first=False` is what keeps the stage offline; the default is True, so this
    is the argument that matters."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    seen: dict = {}
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: seen.update(kw) or {"status": "ok", "output_exists": False},
    )

    silo.report(run, context)

    assert seen["refresh_first"] is False


def test_report_embeds_the_photo_captured_at_fetch_time(silo, delegates, ctx, session,
                                                        monkeypatch):
    """From the run's stored media rather than from disk, so the embedded image is the one
    captured when the assessment was made."""
    run, context = ctx
    monkeypatch.setattr(
        delegates["ingest_api"], "capture_row_image", lambda s, r: ("p.png", b"PNG-BYTES")
    )
    fetched(session, run, silo, context, delegates)
    seen: dict = {}
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: seen.update(kw) or {"status": "ok", "output_exists": False},
    )

    silo.report(run, context)

    assert seen["photo"] == b"PNG-BYTES"


def test_report_passes_no_photo_when_the_row_had_none(silo, delegates, ctx, session,
                                                      monkeypatch):
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    seen: dict = {}
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: seen.update(kw) or {"status": "ok", "output_exists": False},
    )

    silo.report(run, context)

    assert seen["photo"] is None


def test_report_attaches_the_generated_document_as_the_deliverable(silo, delegates, ctx,
                                                                   session, monkeypatch,
                                                                   tmp_path):
    """The .docx is what the run is FOR, and it has to arrive as a platform output file so
    the shared download endpoint can serve it."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    document = tmp_path / "psa.docx"
    document.write_bytes(b"DOCX-BYTES")
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: {"status": "ok", "verify_ok": True, "output_exists": True,
                          "output_path": str(document)},
    )

    result = silo.report(run, context)

    assert result["output_size"] == len(b"DOCX-BYTES")
    # `files` is a selectin relationship loaded when the run was fetched, so the row
    # `attach_output` committed is not in the cached collection until it is expired.
    session.refresh(run)
    outputs = [f for f in run.files if f.kind == "output"]
    assert [f.filename for f in outputs] == [silo.REPORT_MEDIA]


def test_the_absolute_path_never_reaches_the_checkpoint(silo, delegates, ctx, session,
                                                        monkeypatch, tmp_path):
    """A worker's temp path means nothing to the screen and would leak the layout of the
    container; the storage key replaces it."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    document = tmp_path / "psa.docx"
    document.write_bytes(b"X")
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: {"status": "ok", "output_exists": True,
                          "output_path": str(document)},
    )

    result = silo.report(run, context)

    assert "output_path" not in result
    assert result["output_key"]


def test_nothing_is_attached_when_the_engine_produced_no_file(silo, delegates, ctx, session):
    """A validation answer is a legitimate outcome, and attaching a phantom output would
    give the screen a download that does not exist."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)

    result = silo.report(run, context)

    assert "output_key" not in result
    session.refresh(run)
    assert [f for f in run.files if f.kind == "output"] == []


def test_a_failed_verify_still_completes_the_stage(silo, delegates, ctx, session,
                                                   monkeypatch):
    """A failed verify still leaves a downloadable report whose run log explains what
    failed, so this is reported rather than raised."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: {"status": "ok", "verify_ok": False, "output_exists": False},
    )

    result = silo.report(run, context)

    assert result["verify_ok"] is False


def test_the_progress_line_distinguishes_a_failed_verify(silo, delegates, ctx, session,
                                                          monkeypatch):
    """"Assessment generated" on a report that failed its checks would be a lie the run log
    never corrects."""
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    monkeypatch.setattr(
        delegates["engines"], "generate_report",
        lambda *a, **kw: {"status": "ok", "verify_ok": False, "output_exists": False},
    )
    seen: list[str] = []
    context.progress = lambda message, pct=None: seen.append(message)

    silo.report(run, context)

    assert seen[-1] == "Assessment generated with check failures"


def test_report_finishes_the_ladder_at_one_hundred(silo, delegates, ctx, session):
    run, context = ctx
    fetched(session, run, silo, context, delegates)
    seen: list[int | None] = []
    context.progress = lambda message, pct=None: seen.append(pct)

    silo.report(run, context)

    assert seen[-1] == 100


def test_report_fails_loudly_when_fetch_recorded_no_snapshot(silo, delegates, ctx):
    """A resume with a missing snapshot key must say so, not rebuild from nothing and
    produce an assessment of an empty catalogue."""
    run, context = ctx

    with pytest.raises(ValueError, match="no snapshot"):
        silo.report(run, context)
