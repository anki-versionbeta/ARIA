"""ATR / MFGR — the silo contract.

One silo, two report types. ATR is keyed by a CMC request id and MFGR by a batch id; they
share this stage skeleton and almost nothing else, so every stage dispatches on
`report_type` to a per-type module. Do not try to unify the queries, the report objects or
the field sets — they are different documents.

Four endpoints become four stages:

    POST /api/{atr,mfgr}/generate   ->  fetch, generate
    <the author edits the report>   ->  await_edits
    POST /api/{atr,mfgr}/finalize   ->  finalize

**There is no uploaded document.** `ACCEPTS = []` says so. A run starts from an identifier
the user picks from a live list, and `router.py` creates it — the platform's upload
endpoint is never involved. The parameters live on the run's `meta` column.

**The report is not cached between stages.** The app being replaced holds the built report
in a TTL cache and answers "This report run has expired — regenerate it, then download"
when it lapses. Instead, `fetch` stores the raw query snapshot and `finalize` rebuilds the
report from it. Both builders are pure functions of `(identifier, raw)`, so the rebuild is
deterministic, the expiry failure disappears, and the audit snapshot and the rebuild read
the same bytes — which is what the ALCOA+ trail claims.

The pipeline modules are imported inside the stage bodies, not at module scope, so a silo
whose heavy module fails to import stays discoverable and its router keeps working rather
than stranding every parked run.
"""

from __future__ import annotations

import logging

from .location import STORAGE_FOLDERS, STORAGE_PREFIX  # noqa: F401 — read by the registry

LABEL = "ATR / MFGR — Analytical & Manufacturing Reports"
# Nothing is uploaded: the input is an identifier. The registry reads an empty list as
# "no restriction", which is why router.py owns creation rather than the platform's
# upload endpoint.
ACCEPTS: list[str] = []
STAGES = ["fetch", "generate", "await_edits", "finalize"]

REPORT_TYPES = ("atr", "mfgr")

logger = logging.getLogger(__name__)

# The captured query snapshot, stored so `finalize` can rebuild the report without a
# second trip to the warehouse — and so the audit trail and the rebuild agree.
RAW_SNAPSHOT_MEDIA = "raw.json"

EDIT_PROMPT = "Review the report and complete the editable fields"


def _report_type(run) -> str:
    """Which report this run builds, from the parameters `router.py` recorded."""
    meta = run.meta or {}
    report_type = str(meta.get("report_type") or "").lower()
    if report_type not in REPORT_TYPES:
        raise ValueError(
            f"run {run.id} has no usable report_type in its metadata (got {report_type!r})"
        )
    return report_type


def _pipeline(report_type: str):
    """The per-type module pair. Imported here so an import failure cannot make the silo
    undiscoverable."""
    if report_type == "atr":
        from . import atr_pipeline as pipeline
    else:
        from . import mfgr_pipeline as pipeline
    return pipeline


def fetch(run, ctx):
    """Query the warehouse and keep the snapshot.

    The first half of `POST /generate`. ATR additionally applies its `_has_data` gate,
    which is *not* an error — a request with no rows in the warehouse is a completed run
    carrying an explanation, and a failure would only invite a pointless retry.
    """
    import json

    report_type = _report_type(run)
    pipeline = _pipeline(report_type)
    meta = run.meta or {}

    ctx.progress(f"Reading {meta['ids']['display']} from the warehouse", pct=10)
    # `fetch_raw` returns the identifier alongside the rows because fetching can *refine*
    # it: MFGR resolves the batch's real registration namespace from the warehouse, on the
    # same connection, and everything downstream — including the audit record's bind value —
    # must use the root that was actually queried rather than the one that was assumed.
    raw, ids = pipeline.fetch_raw(ctx, meta, progress=ctx.progress)

    row_counts = {name: len(rows or []) for name, rows in raw.items()}
    has_data = pipeline.has_data(raw)
    logger.info(
        "run %s: fetched %s rows=%s has_data=%s query=%s",
        run.id,
        ids["display"],
        row_counts,
        has_data,
        ids.get("query"),
    )

    raw_key = ctx.storage.put_media(
        run, RAW_SNAPSHOT_MEDIA, json.dumps(raw, default=str).encode("utf-8")
    )

    ctx.progress(
        f"Read {sum(row_counts.values())} row(s)" if has_data else "No data found",
        pct=30,
    )
    return {
        "report_type": report_type,
        "ids": ids,
        "source": "live",
        "raw_key": raw_key,
        "row_counts": row_counts,
        "has_data": has_data,
    }


def generate(run, ctx):
    """Build the report, write the audit record, render the clean PDF and DOCX.

    The second half of `POST /generate`.
    """
    report_type = _report_type(run)
    pipeline = _pipeline(report_type)
    fetched = ctx.checkpoint("fetch") or {}

    if not fetched.get("has_data"):
        # ATR's `_has_data` gate: success with nothing generated, not a failure.
        display = fetched.get("ids", {}).get("display", "this request")
        message = (
            f"No data found in the database for {display}. Verify the id and try again. "
            "No report or audit record was generated."
        )
        ctx.progress(message, pct=100)
        return {"generated": False, "reason": "no_data", "message": message}

    ctx.progress("Building the report", pct=45)
    return pipeline.generate(run, ctx, fetched)


def await_edits(run, ctx):
    """Park until the author completes the editable fields.

    This is what the in-memory TTL cache used to bridge. The worker is released and
    `router.py` records the author's values as this stage's checkpoint.
    """
    generated = ctx.checkpoint("generate") or {}
    if not generated.get("generated", True):
        # Nothing was produced, so there is nothing to review — let the run finish.
        logger.info("run %s: no report to review, skipping the edit pause", run.id)
        return {"skipped": True, "reason": "no_data"}

    ctx.progress(EDIT_PROMPT, pct=60)
    return ctx.pause_for_user(EDIT_PROMPT)


def finalize(run, ctx):
    """Re-bake the report with the author's values and archive it.

    Rebuilds the report from the stored snapshot rather than reading a cache, which is
    what removes the original's "this run has expired" failure.
    """
    report_type = _report_type(run)
    pipeline = _pipeline(report_type)
    fetched = ctx.checkpoint("fetch") or {}
    chosen = ctx.checkpoint("await_edits") or {}

    if chosen.get("skipped") or not fetched.get("has_data"):
        ctx.progress("Nothing to finalize", pct=100)
        return {"finalized": False, "reason": "no_data"}

    ctx.progress("Applying your values", pct=80)
    result = pipeline.finalize(run, ctx, fetched, chosen.get("field_values") or {})
    ctx.progress("Report ready", pct=100)
    return result
