"""The ATR seam: what `silo.py`'s stages call. Ported from `api/routers/atr.py`.

The HTTP layer this replaces did all four steps inside two requests and bridged them with
an in-memory TTL cache. Here they are stages, and the bridge is a checkpoint plus the raw
snapshot — so the "this report run has expired, regenerate it" dead end is gone.

What each function corresponds to in the source:

    fetch_raw   ->  `_load_raw`      (fixture or live, same branch)
    has_data    ->  `_has_data`
    generate    ->  the render + audit + S3 half of `POST /generate`
    finalize    ->  `POST /finalize`, with the report rebuilt rather than read from cache

The archival copies keep the source's layout exactly — `<prefix>/ATR_<pid>_<display>/pdf/`
and `/docx/`, with `_preview` for the clean render and `_edited` for the author's version.
That tree is a compliance artefact and reviewers navigate it, so it is preserved even
though the platform also attaches both files to the run.
"""

from __future__ import annotations

import logging

from . import archive, atr_data, audit, workspace
from .atr_pdf import ManualDefaults, generate_atr_pdf
from .atr_docx import generate_atr_docx
from .atr_report import build_atr_report
from .finalize import finalize_pdf
from .ids import new_process_id, run_prefix

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"


def fetch_raw(ctx, meta: dict, progress=None) -> tuple[dict, dict]:
    """Run the query pack against the warehouse. Returns `(raw, ids)`.

    `_load_raw` in the source, minus its offline fixture branch — the warehouse is now the
    only data source. `atr_data.fetch_fixture` still exists for offline tests, but no run
    reaches it.

    ATR returns the identifier unchanged: `normalize_request_id` is authoritative, because
    every ATR request id lives under the one `PEGA-PROD-CMC-` namespace. MFGR has to resolve
    its namespace against the warehouse, which is why the contract returns ids at all.
    """
    ids = meta["ids"]
    warehouse = ctx.warehouse
    # Resolved here rather than at import time: the object names come from the platform,
    # because a silo may not read the environment. The SQL text is unchanged.
    schema, results_object = warehouse.schema, warehouse.results_object
    with warehouse.connect() as conn:
        return atr_data.fetch_all(conn, ids["query"], schema, results_object), ids


def has_data(raw: dict) -> bool:
    return atr_data.has_data(raw)


def list_request_ids(conn, schema: str, results_object: str) -> list[str]:
    """Every request id with results, for the picker. Not part of the audited pack.

    The object names are passed in rather than looked up here: the caller already holds the
    warehouse it opened `conn` on, and resolving a second one would let the connection and
    the object names disagree.
    """
    return atr_data.fetch_request_ids(conn, schema, results_object)


def _load_snapshot(ctx, fetched: dict) -> dict:
    import json

    with ctx.storage.open_key(fetched["raw_key"]) as handle:
        return json.loads(handle.read().decode("utf-8"))


def _render(report, display: str, defaults: ManualDefaults | None, generated_at: str, variant: str):
    """Render both formats and return their bytes.

    The builders write to a path and return it, so a scratch tree is created for them and
    the bytes read back. `variant` is "preview" (clean) or "edited" (with the author's
    values), matching the archival filenames.
    """
    suffix = "_edited" if variant == "edited" else ""
    pdf_name = f"ATR_{display}{suffix}.pdf"
    docx_name = f"ATR_{display}{suffix}.docx"

    with workspace.scratch() as ws:
        generate_atr_pdf(
            report, ws.path(pdf_name), defaults=defaults, generated_at=generated_at
        )
        generate_atr_docx(
            report, ws.path(docx_name), defaults=defaults, generated_at=generated_at
        )
        if variant == "edited":
            # The archival copy freezes the completed fields as read-only and restricts
            # permissions. The source is candid that this is a record lock rather than a
            # true appearance flatten.
            locked = f"ATR_{display}_locked.pdf"
            finalize_pdf(ws.path(pdf_name), ws.path(locked))
            return ws.read(locked), ws.read(docx_name), pdf_name, docx_name
        return ws.read(pdf_name), ws.read(docx_name), pdf_name, docx_name


def _archive(ctx, run, prefix: str, display: str, variant: str, pdf: bytes, docx: bytes) -> None:
    """Keep the compliance tree the reviewers navigate.

    `attach_output` separately makes both files downloadable from the document page; this
    tree is in addition to that, as in the source. See `archive.py` for why it does not go
    through `ctx.storage`.
    """
    archive.put(f"{prefix}/pdf/ATR_{display}_{variant}.pdf", pdf)
    archive.put(f"{prefix}/docx/ATR_{display}_{variant}.docx", docx)


def generate(run, ctx, fetched: dict) -> dict:
    """Build the report, write the audit record, render and attach the clean copies."""
    raw = _load_snapshot(ctx, fetched)
    ids = fetched["ids"]
    display = ids["display"]

    report = build_atr_report(display, raw)
    generated_at = audit.now_iso()

    process_id = new_process_id()
    prefix = run_prefix("ATR", process_id, display)

    # The audit record names the output and carries the exact SQL that ran, so it is
    # written before the render — as in the source, where a render failure still leaves
    # the generation recorded.
    schema, results_object = ctx.warehouse.schema, ctx.warehouse.results_object
    audit_key = audit.record_generation(
        run,
        ctx,
        ids=ids,
        id_field="request_id",
        bind_field="request_id",
        queries=atr_data.build_queries(schema, results_object),
        raw=raw,
        flags=report.flags,
        output_path=f"{prefix}/pdf/ATR_{display}_preview.pdf",
        generated_at=generated_at,
        app_user=_app_user(run),
    )

    ctx.progress("Rendering the report", pct=55)
    pdf, docx, pdf_name, docx_name = _render(report, display, None, generated_at, "preview")

    _archive(ctx, run, prefix, display, "preview", pdf, docx)
    ctx.storage.attach_output(run, pdf_name, pdf, _PDF_MIME)
    ctx.storage.attach_output(run, docx_name, docx, _DOCX_MIME)

    logger.info(
        "run %s: generated ATR %s (%d flags, pdf=%d bytes)",
        run.id,
        display,
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
        "is_stability": report.is_stability,
    }


def finalize(run, ctx, fetched: dict, field_values: dict) -> dict:
    """Re-bake with the author's values, lock the PDF, and attach both.

    The report is rebuilt from the stored snapshot rather than read from a cache, which is
    what removes the original's expiry failure. `build_atr_report` is a pure function of
    (identifier, raw), so the rebuild is exact.
    """
    raw = _load_snapshot(ctx, fetched)
    generated = ctx.checkpoint("generate") or {}
    ids = fetched["ids"]
    display = ids["display"]

    report = build_atr_report(display, raw)
    defaults = defaults_from_fields(field_values)
    generated_at = generated.get("generated_at") or audit.now_iso()
    prefix = generated.get("prefix") or run_prefix("ATR", new_process_id(), display)

    pdf, docx, pdf_name, docx_name = _render(report, display, defaults, generated_at, "edited")

    _archive(ctx, run, prefix, display, "edited", pdf, docx)
    ctx.storage.attach_output(run, pdf_name, pdf, _PDF_MIME)
    ctx.storage.attach_output(run, docx_name, docx, _DOCX_MIME)

    logger.info("run %s: finalized ATR %s with %d value(s)", run.id, display, len(field_values))
    return {
        "finalized": True,
        "prefix": prefix,
        "pdf_bytes": len(pdf),
        "docx_bytes": len(docx),
    }


def defaults_from_fields(fv: dict) -> ManualDefaults:
    """Map AcroForm field values onto the builders' `ManualDefaults`.

    Ported from `_defaults_from_fields`. The names deliberately differ from the form field
    names (`spec_id` <- `hqc_spec_id`, `hqc` <- `hqc_assessment`) — that mapping is the
    contract the front end binds to, so it is not "tidied".
    """
    na = str(fv.get("hqc_remarks_na", "")).strip().lower()
    return ManualDefaults(
        regulatory=(fv.get("regulatory") or "").strip(),
        spec_id=(fv.get("hqc_spec_id") or "").strip(),
        hqc=(fv.get("hqc_assessment") or "").strip(),
        remarks=(fv.get("hqc_remarks") or "").strip(),
        remarks_na=na in ("yes", "on", "true", "1", "checked"),
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
