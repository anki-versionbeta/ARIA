"""The MFGR seam: what `silo.py`'s stages call. Ported from `api/routers/mfgr.py`.

The HTTP layer this replaces did all four steps inside two requests and bridged them with
an in-memory TTL cache keyed by a process id. Here they are stages, and the bridge is a
checkpoint plus the raw snapshot — so `POST /finalize`'s "This report run has expired —
regenerate it, then download" dead end is gone.

What each function corresponds to in the source:

    fetch_raw   ->  the `resolve_query_root` + `fetch_report` half of `POST /generate`
    generate    ->  the render + audit + S3 half of `POST /generate`
    finalize    ->  `POST /finalize`, with the report rebuilt rather than read from cache

The archival copies keep the source's layout exactly — `<prefix>/MFGR_<pid>_<short>/pdf/`
and `/docx/`, with `_preview` for the clean render and `_edited` for the author's version.
That tree is a compliance artefact and reviewers navigate it, so it is preserved even
though the platform also attaches both files to the run.

**Two things MFGR does that ATR does not**, both preserved:

* **The batch's registration namespace is resolved against the warehouse first.**
  `normalize_batch_id` assumes `nest-br-prod-`, which is wrong for externally-manufactured
  batches (BAX001209 is really `nest-br-ext-dev-BAX001209-01`). The source calls
  `resolve_query_root` on the same connection and falls back to the assumed root. Both the
  query and the audit record's bind value use the resolved root.
* **The validation query is skipped** on the report path — `fetch_report`, not `fetch_all`.
  Its six `COUNT(*)` sub-selects were the slowest part of the pack.
"""

from __future__ import annotations

import logging
from dataclasses import fields as dc_fields

from . import archive, audit, mfgr_data, workspace
from .finalize import finalize_pdf
from .ids import new_process_id, run_prefix
from .mfgr_docx import generate_mfgr_docx
from .mfgr_pdf import ManualDefaults, generate_mfgr_pdf
from .mfgr_report import build_mfgr_report

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"

# The archival PDF's owner password. `finalize.py`'s default is ATR's; MFGR's own value is
# passed explicitly so the two silos' record locks stay as the source had them.
_OWNER_PASSWORD = REDACTED

# MFGR's AcroForm field names match `ManualDefaults` 1:1 — unlike ATR, which remaps.
_MANUAL_FIELDS = frozenset(f.name for f in dc_fields(ManualDefaults))


def fetch_raw(ctx, meta: dict, progress=None) -> tuple[dict, dict]:
    """Run the query pack, or load a captured fixture. Returns `(raw, ids)`.

    The returned ids carry the *resolved* query root, which is what the queries actually
    bound and therefore what the audit record must name.
    """
    ids = dict(meta["ids"])
    if meta.get("source") == "fixture":
        path = meta.get("fixture_path")
        if not path:
            raise ValueError("a fixture source needs fixture_path")
        logger.info("Loading MFGR fixture %s", path)
        return mfgr_data.fetch_fixture(path), ids

    warehouse = ctx.warehouse
    # Resolved here rather than at import time: the schema comes from the platform, because
    # a silo may not read the environment. The SQL text is unchanged.
    schema = warehouse.schema
    with warehouse.connect() as conn:
        # One connection for both, as the source did: resolving the namespace and then
        # querying it must not race a warehouse that changed in between.
        resolved = mfgr_data.resolve_query_root(conn, ids["short"], schema)
        if resolved and resolved != ids.get("query"):
            logger.info(
                "batch %s resolves to %r, not the assumed %r",
                ids["short"],
                resolved,
                ids.get("query"),
            )
        ids["query"] = resolved or ids["query"]
        if progress:
            progress(f"Reading {ids['display']} from the warehouse", pct=20)
        return mfgr_data.fetch_report(conn, ids["query"], schema), ids


def has_data(raw: dict) -> bool:
    return mfgr_data.has_data(raw)


def list_batch_ids(conn, schema: str, limit: int = 200) -> list[str]:
    """Every batch id with data, for the picker. Not part of the audited pack.

    The schema is passed in rather than looked up here: the caller already holds the
    warehouse it opened `conn` on, and resolving a second one would let the connection and
    the schema disagree.
    """
    return mfgr_data.list_batch_ids(conn, schema, limit=limit)


def batch_exists(conn, ids: dict, schema: str) -> bool:
    """Does this batch have data? The source checks separately so the user finds out before
    waiting for a full generation."""
    return mfgr_data.batch_id_exists(conn, ids["short"], schema)


def _load_snapshot(ctx, fetched: dict) -> dict:
    import json

    with ctx.storage.open_key(fetched["raw_key"]) as handle:
        return json.loads(handle.read().decode("utf-8"))


def _render(report, short: str, defaults: ManualDefaults | None, generated_at: str, variant: str):
    """Render both formats and return their bytes.

    The builders write to a path and return it, so a scratch tree is created for them and
    the bytes read back. `variant` is "preview" (clean) or "edited" (with the author's
    values), matching the archival filenames.
    """
    suffix = "_edited" if variant == "edited" else ""
    pdf_name = f"MFGR_{short}{suffix}.pdf"
    docx_name = f"MFGR_{short}{suffix}.docx"

    with workspace.scratch() as ws:
        generate_mfgr_pdf(
            report, ws.path(pdf_name), defaults=defaults, generated_at=generated_at
        )
        generate_mfgr_docx(
            report, ws.path(docx_name), defaults=defaults, generated_at=generated_at
        )
        if variant == "edited":
            # The archival copy freezes the completed fields as read-only and restricts
            # permissions. The source is candid that this is a record lock rather than a
            # true appearance flatten.
            locked = f"MFGR_{short}_locked.pdf"
            finalize_pdf(ws.path(pdf_name), ws.path(locked), owner_password=_OWNER_PASSWORD)
            return ws.read(locked), ws.read(docx_name), pdf_name, docx_name
        return ws.read(pdf_name), ws.read(docx_name), pdf_name, docx_name


def _archive(ctx, run, prefix: str, short: str, variant: str, pdf: bytes, docx: bytes) -> None:
    """Keep the compliance tree the reviewers navigate.

    `attach_output` separately makes both files downloadable from the document page; this
    tree is in addition to that, as in the source. See `archive.py` for why it does not go
    through `ctx.storage`.
    """
    archive.put(f"{prefix}/pdf/MFGR_{short}_{variant}.pdf", pdf)
    archive.put(f"{prefix}/docx/MFGR_{short}_{variant}.docx", docx)


def generate(run, ctx, fetched: dict) -> dict:
    """Build the report, write the audit record, render and attach the clean copies."""
    raw = _load_snapshot(ctx, fetched)
    ids = fetched["ids"]
    short = ids["short"]

    report = build_mfgr_report(short, raw)
    generated_at = audit.now_iso()

    process_id = new_process_id()
    prefix = run_prefix("MFGR", process_id, short)

    # The audit record names the output and carries the exact SQL that ran, so it is
    # written before the render — as in the source, where a render failure still leaves
    # the generation recorded.
    #
    # The source passes `batch_ids={"short": short, "query": root}` — only those two keys,
    # not the whole normalised dict. That shape is part of the artefact, so it is kept.
    audit_key = audit.record_generation(
        run,
        ctx,
        ids={"short": short, "query": ids["query"]},
        id_field="batch_id",
        bind_field="batch_root",
        queries=mfgr_data.build_queries(ctx.warehouse.schema),
        raw=raw,
        flags=report.flags,
        output_path=f"{prefix}/pdf/MFGR_{short}_preview.pdf",
        generated_at=generated_at,
        app_user=_app_user(run),
    )

    ctx.progress("Rendering the report", pct=55)
    pdf, docx, pdf_name, docx_name = _render(report, short, None, generated_at, "preview")

    _archive(ctx, run, prefix, short, "preview", pdf, docx)
    ctx.storage.attach_output(run, pdf_name, pdf, _PDF_MIME)
    ctx.storage.attach_output(run, docx_name, docx, _DOCX_MIME)

    logger.info(
        "run %s: generated MFGR %s (%d flags, pdf=%d bytes)",
        run.id,
        short,
        len(report.flags),
        len(pdf),
    )
    return {
        "generated": True,
        "process_id": process_id,
        "prefix": prefix,
        "generated_at": generated_at,
        "audit_key": audit_key,
        "flags": [
            {"severity": f.severity, "area": f.area, "message": f.message}
            for f in report.flags
        ],
        "registration": report.registration,
    }


def finalize(run, ctx, fetched: dict, field_values: dict) -> dict:
    """Re-bake with the author's values, lock the PDF, and attach both.

    The report is rebuilt from the stored snapshot rather than read from a cache, which is
    what removes the original's expiry failure. `build_mfgr_report` is a pure function of
    (short id, raw), so the rebuild is exact.
    """
    raw = _load_snapshot(ctx, fetched)
    generated = ctx.checkpoint("generate") or {}
    ids = fetched["ids"]
    short = ids["short"]

    report = build_mfgr_report(short, raw)
    defaults = defaults_from_fields(field_values)
    generated_at = generated.get("generated_at") or audit.now_iso()
    prefix = generated.get("prefix") or run_prefix("MFGR", new_process_id(), short)

    pdf, docx, pdf_name, docx_name = _render(report, short, defaults, generated_at, "edited")

    _archive(ctx, run, prefix, short, "edited", pdf, docx)
    ctx.storage.attach_output(run, pdf_name, pdf, _PDF_MIME)
    ctx.storage.attach_output(run, docx_name, docx, _DOCX_MIME)

    logger.info("run %s: finalized MFGR %s with %d value(s)", run.id, short, len(field_values))
    return {
        "finalized": True,
        "prefix": prefix,
        "pdf_bytes": len(pdf),
        "docx_bytes": len(docx),
    }


def defaults_from_fields(fv: dict) -> ManualDefaults:
    """Map AcroForm field values onto the builders' `ManualDefaults`.

    Ported from `_defaults_from_fields`. MFGR's form field names match the dataclass field
    names exactly, so this is a filter rather than a mapping — and note it only passes keys
    that are *present*, so an omitted field keeps the dataclass default instead of being
    blanked. ATR's equivalent differs on both counts.
    """
    return ManualDefaults(
        **{
            name: (fv.get(name) or "").strip()
            for name in _MANUAL_FIELDS
            if name in fv
        }
    )


def _app_user(run) -> dict[str, str] | None:
    """The authenticated author, for the audit record's `app_user`."""
    owner = getattr(run, "owner", None)
    if owner is None:
        return None
    return {
        "username": getattr(owner, "username", "") or "",
        "full_name": getattr(owner, "display_name", "") or "",
        "email": getattr(owner, "email", "") or "",
    }
