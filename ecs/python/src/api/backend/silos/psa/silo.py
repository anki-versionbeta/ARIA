"""Product Similarity Assessment — the silo contract.

This is the file ARIA's registry looks for (`da_platform/silo_registry.py`: `CONTRACT_FILE = "silo.py"`).
Discovery is by folder scan, so registering PSA adds a folder and edits no platform file.

**There is no uploaded document.** `ACCEPTS = []` says so. A run starts from a program code and a
Smartsheet row that the user picks from a live list, and `router.py` creates it. `mfg_atr` has the same
shape and states the same thing.

    fetch      one Smartsheet read — sheet JSON *and* the presentation's product photo, as run media
    report     the QPP11-04-001-G004-F01 assessment .docx, from that snapshot and photo

**A job is the REPORT, and nothing else** (decided 2026-08-22, user). Clicking "Generate PSA report" is
what creates a history entry; the cap-colour recommendation stays the screen's instant, unrecorded
preview. Two stages, straight through, no pause.

This REVERSES the four-stage design of 2026-08-21, which added a `confirm` stage that parked the run so
the assessor could confirm a cap colour, recording who chose what. That worked — it was executed end to
end — but it made the report a two-step interaction and put a second "start a job" control on the screen
next to the report button. The user removed it in favour of one button that does what its label says. The
consequence is accepted and worth stating plainly: **nothing now records which cap colour a person chose,
or that they chose it.** The recommendation is advisory again. `git log` has the pause if it comes back.

**Every stage after `fetch` rebuilds from the stored bytes and touches no network.** That is the whole
design: see `snapshot.py` for why, and note the two properties it buys — a run's recommendation and its
report necessarily describe the same catalogue, and the snapshot is the audit record of what Smartsheet
said when the assessment was made.

⚠️ `ACCEPTS = []` means "no upload RESTRICTION", not "no upload": `Silo.accepts_filename()` returns True
for an empty list (`silo_registry.py:68-72`) and the platform's upload endpoint gates on nothing else
(`api/uploads.py:59`), so a file POSTed to `/silos/psa/documents` WOULD create a run. `_params()` therefore
fails such a run loudly instead of half-running it — the run needs a program code, and a file upload
cannot supply one.

Heavy modules are imported inside the stage bodies, not at module scope, so a silo whose pipeline fails
to import stays discoverable and its router keeps working — `mfg_atr` does the same, for the same reason.
`psa/aria.py` is imported the same way for a second reason: it imports `da_platform`, so it cannot be
imported at all outside ARIA.
"""

from __future__ import annotations

import json
import logging

from .location import STORAGE_FOLDERS, STORAGE_PREFIX  # noqa: F401 — read by the registry

LABEL = "Product Similarity Assessment"
# Nothing is uploaded: the input is a program code plus a Smartsheet row.
ACCEPTS: list[str] = []
STAGES = ["fetch", "report"]

logger = logging.getLogger(__name__)

REPORT_MEDIA = "psa_assessment.docx"
PHOTO_MEDIA_PREFIX = "product_photo"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Progress ladder for the whole job, so the percentages read as ONE monotonic sequence rather than each
# stage counting to 100 on its own. Found by executing the run: `recommend` used to finish at 100%, so a
# job parked for a human decision with the report still to come reported itself complete.
PCT = {
    "fetch_start": 10, "fetch_done": 35,
    "report_start": 55, "report_generating": 80, "report_done": 100,
}
"""One monotonic ladder for the whole job rather than each stage counting to 100 on its own — the first
real execution showed a job reporting itself complete while it still had work left."""


def _params(run) -> dict:
    """The run's parameters, as `router.py` recorded them on `run.meta`.

    `source_row` is the Smartsheet row — the key that is stable across database rebuilds, unlike
    `product_id`, which is reassigned every time psa.db is recreated.

    Raises on a run with no program code rather than guessing one. That is the case a file uploaded to
    the platform's generic endpoint produces, and a run that cannot say which product it is about has
    nothing to assess.
    """
    meta = run.meta or {}
    program = str(meta.get("program") or "").strip()
    if not program:
        raise ValueError(
            f"run {run.id} has no program code in its metadata. PSA starts from a product + "
            "presentation chosen from the Smartsheet, so it cannot be started by uploading a file."
        )
    # `vendor` stays None when the run did not name one, and None means EVERY supplier
    # (`cap_recommend.ALL_VENDORS`). It used to fall back to a default of "Datwyler", which silently
    # restricted the whole job to one catalogue — found on the first real execution: the run's checkpoint
    # read `vendors_considered: ['Datwyler']` while the pause message it displayed said "across every
    # supplier". A default here is exactly wrong: at assessment time the cap is often not yet tooled, so
    # "no supplier named" means the supplier is still open, not that it is the first one alphabetically.
    vendor = str(meta.get("vendor")).strip() if meta.get("vendor") else None
    return {
        "program": program,
        "source_row": meta.get("source_row"),
        "vendor": vendor or None,
    }


def _snapshot_bytes(run, ctx) -> bytes:
    """The snapshot this run captured. Fails loudly if `fetch` did not record one."""
    fetched = ctx.checkpoint("fetch") or {}
    key = fetched.get("snapshot_key")
    if not key:
        raise ValueError(f"run {run.id} has no snapshot: the fetch stage recorded no snapshot_key")
    with ctx.storage.open_key(key) as handle:
        return handle.read()


def fetch(run, ctx):
    """Read the live Smartsheet once and store the raw response — and the product photo — as run media.

    The ONLY stage that touches the network, which is the property everything else depends on. It captures
    both artefacts here for the same reason: the report needs the photo, and a second trip at report time
    could return a different image than the one the assessment was based on.

    The photo is fetched for THIS presentation only (see `ingest_smartsheet_api.capture_row_image`): a run
    assesses one row, and downloading the other ~36 to embed one would be waste. A row with no photo is
    normal — the report renders a blank photo cell and `verify.py` accepts it — so a missing image is
    recorded, not raised.
    """
    from . import aria, ingest_smartsheet_api as ingest_api, snapshot

    aria.install()
    params = _params(run)
    ctx.progress(f"Reading the Smartsheet for {params['program']}", pct=PCT["fetch_start"])
    try:
        raw = snapshot.capture_bytes()
    except SystemExit as exc:
        # `ingest_smartsheet_api._fetch_sheet` reports a 401 / HTTP error / unreachable host by raising
        # SystemExit — reasonable for the CLI it was written for, dangerous here. SystemExit derives from
        # BaseException, NOT Exception, so a worker that wraps a stage in `except Exception` to mark the
        # run failed would not catch it: the exception would unwind the worker itself and take every OTHER
        # silo's queued runs down with it. An expired PSA token must fail PSA's run, nothing more.
        raise RuntimeError(f"Smartsheet capture failed: {exc}") from exc
    facts = snapshot.describe(raw)
    key = ctx.storage.put_media(run, snapshot.SNAPSHOT_MEDIA, raw)

    photo_key, photo_bytes = None, 0
    source_row = params["source_row"]
    if source_row is not None:
        sheet = json.loads(raw.decode("utf-8"))["sheet"]
        captured = ingest_api.capture_row_image(sheet, int(source_row))
        if captured:
            name, data = captured
            ext = (name.rsplit(".", 1)[-1] if "." in name else "png").lower()
            photo_key = ctx.storage.put_media(run, f"{PHOTO_MEDIA_PREFIX}.{ext}", data)
            photo_bytes = len(data)

    logger.info("run %s: captured %s rows (%s bytes) from sheet %s; photo=%s bytes",
                run.id, facts["rows"], facts["bytes"], facts["sheet_id"], photo_bytes)
    ctx.progress(f"Captured {facts['rows']} sheet row(s)"
                 + (f" and a {photo_bytes}-byte product photo" if photo_key else " (no product photo)"),
                 pct=PCT["fetch_done"])
    return {**params, **facts, "snapshot_key": key,
            "photo_key": photo_key, "photo_bytes": photo_bytes}


def report(run, ctx):
    """Generate the PSA assessment document and attach it as the run's deliverable.

    Rebuilt from the same snapshot the recommendation used, so the two cannot disagree. The product photo
    comes from the run's stored media rather than from disk, so the embedded image is the one captured when
    the assessment was made. Parts D and E are an auto-drafted, banner-marked draft for SME review.

    """
    from . import aria, engines, snapshot

    aria.install()
    params = _params(run)
    raw = _snapshot_bytes(run, ctx)

    ctx.progress("Rebuilding the catalogue from the snapshot", pct=PCT["report_start"])
    snapshot.rebuild(raw)

    photo = None
    photo_key = (ctx.checkpoint("fetch") or {}).get("photo_key")
    if photo_key:
        with ctx.storage.open_key(photo_key) as handle:
            photo = handle.read()

    ctx.progress(f"Generating the assessment for {params['program']}", pct=PCT["report_generating"])
    result = dict(engines.generate_report(params["program"], params["source_row"],
                                          refresh_first=False, photo=photo))

    output_path = result.pop("output_path", None)
    if result.get("output_exists") and output_path:
        with open(output_path, "rb") as fh:
            record = ctx.storage.attach_output(run, REPORT_MEDIA, fh.read(), content_type=DOCX_MIME)
        result["output_key"] = record.storage_key
        result["output_size"] = record.size_bytes

    verified = result.get("verify_ok")
    logger.info("run %s: report status=%s verify_ok=%s", run.id, result.get("status"), verified)
    # A failed verify still leaves a downloadable report whose run log explains what failed, so this is
    # reported rather than raised.
    ctx.progress("Assessment generated" if verified else "Assessment generated with check failures",
                 pct=PCT["report_done"])
    return result
