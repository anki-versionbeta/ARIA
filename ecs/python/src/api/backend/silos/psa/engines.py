"""Shared state and the data layer between `router.py` and the pipeline.

Everything here is front-end agnostic and returns plain dicts — which is what allowed the UI to be
replaced without touching the engines. It carries no configuration of its own: paths and credentials are
resolved per call from `config.config()`, so importing this module has no side effects and no ordering
requirement.

Ports to `da-backend/silos/psa/engines.py` unchanged.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading

from . import build_db, cap_export, cap_queries, cap_recommend, paths, scope, workflow
from . import ingest_smartsheet_api as ingest_api

# Every recommend / report / refresh rebuilds the SHARED psa.db from scratch (build_db drops and
# recreates every table). Concurrent requests would read a half-built database, so they queue.
# This only works because the endpoints are sync `def`: Starlette runs those in a threadpool, so
# blocking here blocks one worker, not the event loop.
RUN_LOCK = threading.Lock()


def db_exists() -> bool:
    """True when psa.db exists and holds something. A 0-byte file is not a database."""
    try:
        db = paths.db_path()
        return os.path.exists(db) and os.path.getsize(db) > 0
    except OSError:
        return False


@contextlib.contextmanager
def connect():
    """A short-lived READ-ONLY connection to psa.db (rebuilt per run, so never cache one).

    `mode=ro` matters beyond hygiene: a plain `sqlite3.connect()` CREATES the file, so a single read
    against an unbuilt checkout would leave a 0-byte psa.db behind — and `os.path.exists(psa.db)` is
    exactly what tests/test_cap_colors.py and tests/test_risk.py gate their smoke tests on, so that
    empty file makes them crash with "no such table: product" instead of skipping.

    Raises sqlite3.OperationalError when the file is absent; every caller here either guards with
    db_exists() or answers with an empty/error result.
    """
    con = sqlite3.connect(f"file:{paths.db_path()}?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def smartsheet_live() -> bool:
    """True when the live Smartsheet API is configured (else the pipeline uses the stale .xlsx)."""
    return ingest_api.available()


def refresh(skip_images: bool = True) -> tuple[bool, str]:
    """Rebuild psa.db from the live Smartsheet. Returns (ok, message); never raises.

    `ingest_smartsheet_api.main()` raises SystemExit when a credential is missing — a BaseException,
    so `except Exception` would let it escape and kill the worker. It is caught explicitly.
    """
    try:
        with RUN_LOCK:
            build_db.main()
            if not ingest_api.available():
                return False, ("Live Smartsheet is not configured — set SMARTSHEET_ACCESS_TOKEN and "
                               "SMARTSHEET_SHEET_ID. The database was rebuilt empty.")
            ingest_api.main(skip_images=skip_images)
            return True, "Product list refreshed from the LIVE Smartsheet."
    except SystemExit as exc:
        return False, f"Refresh failed: {exc}"
    except Exception as exc:
        return False, f"Refresh failed: {exc}"


# ---------------------------------------------------------------- pickers
def pres_label(vial, batch_type, strength) -> str:
    """Presentation label: Vial | Batch type | Strength.

    The list number is deliberately absent — it was dropped from the label on 2026-08-17.

    Returns "" when the row carries none of the three, so the caller's `or f"row {sr}"` fallback can
    name the sheet row. A "?" placeholder here would be truthy and defeat it: a nearly-blank row (one a
    Smartsheet form submission can leave behind) rendered as a bare "?" in the picker, which named
    nothing the analyst could go and look at.
    """
    parts = [
        (vial or "").strip(),
        (batch_type or "").strip(),
        (str(strength).strip() if strength else ""),
    ]
    if not any(parts):
        return ""
    return "  |  ".join([parts[0] or "?"] + [p for p in parts[1:] if p])


def programs() -> list[dict]:
    """In-scope products for the picker: live Smartsheet first, else the psa.db snapshot."""
    try:
        if ingest_api.available():
            live = ingest_api.live_programs()
            if live:
                return [{"program_no": p, "program_name": n or "",
                         "label": f"{p} ({n})" if n else p} for p, n in live]
        with connect() as con:
            rows = con.execute(
                "SELECT DISTINCT program_no, program_name FROM product p WHERE program_no IS NOT NULL "
                "AND TRIM(program_no)<>'' AND UPPER(program_no)<>'N/A' "
                f"AND {scope.scope_sql('p')} ORDER BY program_no").fetchall()
        return [{"program_no": p, "program_name": n or "",
                 "label": f"{p} ({n})" if n else p} for p, n in rows]
    except Exception:
        return []


def presentations(program: str, live: bool = True) -> list[dict]:
    """In-scope presentations of one program, keyed by `source_row` (stable across DB rebuilds).

    `live=False` forbids the Smartsheet read and answers from `psa.db` alone. **A caller working from a
    captured snapshot must pass it.** The picker endpoint wants the live sheet — that is the point of the
    picker — but a stage that has promised not to touch the network must not reach for a label and quietly
    break that promise. It did: `recommendation()` called this unconditionally, purely to build a display
    string, so a snapshot-only run still issued one REST read. Beyond contradicting the guarantee, it made
    the label come from a *different* sheet state than the recommendation it labels, and a Smartsheet
    outage could fail a stage that has no business needing Smartsheet.
    """
    try:
        if live and ingest_api.available():
            live = ingest_api.live_presentations(program)
            if live:
                return [{"source_row": r["source_row"],
                         "label": pres_label(r["vial"], r["batch_type"], r.get("strength"))
                         or f"row {r['source_row']}",
                         "vial_size": r["vial"] or "", "batch_type": r["batch_type"] or "",
                         "strength": str(r.get("strength") or ""),
                         "list_number": str(r.get("list_number") or "")} for r in live]
        with connect() as con:
            rows = con.execute(
                "SELECT source_row, vial_container_size, batch_type, strength, list_number "
                f"FROM product p WHERE program_no=? AND {scope.scope_sql('p')} ORDER BY source_row",
                (program,)).fetchall()
        return [{"source_row": sr, "label": pres_label(v, b, s) or f"row {sr}",
                 "vial_size": v or "", "batch_type": b or "", "strength": str(s or ""),
                 "list_number": str(ln or "")} for sr, v, b, s, ln in rows]
    except Exception:
        return []


def program_label(program: str) -> str:
    """'CODE (Name)' from psa.db, falling back to the bare code."""
    try:
        with connect() as con:
            row = con.execute(
                "SELECT program_name FROM product WHERE program_no=? AND program_name IS NOT NULL "
                "LIMIT 1", (program,)).fetchone()
        return f"{program} ({row[0]})" if row and row[0] else program
    except Exception:
        return program


# ---------------------------------------------------------------- tables
# These own the "database not built yet" case so router.py never has to hold a connection.
def products_by_color(vendor: str, color: str) -> list[dict]:
    """In-scope products using one exact palette colour. [] when psa.db is not built."""
    if not db_exists():
        return []
    try:
        with connect() as con:
            return cap_queries.products_by_color(con, vendor, color)
    except Exception:
        return []


def site_product_counts() -> list[dict]:
    """In-scope product count per manufacturing site. [] when psa.db is not built."""
    if not db_exists():
        return []
    try:
        with connect() as con:
            return cap_queries.site_product_counts(con)
    except Exception:
        return []


# ---------------------------------------------------------------- recommendation
def recommendation(program: str, source_row, vendor: str | None = None,
                   refresh_first: bool = True) -> tuple[dict | None, str, str]:
    """Cap-colour recommendation for ONE presentation, computed program-wide.

    Program-wide is required, not an optimisation: `recommend_program_presentations` reserves each
    presentation's already-selected colour and hands out a distinct #1 pick per presentation, which is
    what sets `first_unique`. Calling `recommend()` for a single presentation would lose that.

    Returns (result_or_None_or_error_dict, program_label, presentation_label). Exception-tolerant like
    the rest of the cap layer: on a database that has not been built yet the query raises
    ("no such table: product"), and a 500 is something the screen cannot render — so it comes back as
    an `error` the UI can show.

    `refresh_first=False` skips the live re-ingest and reads whatever is already in psa.db. That is what
    the `recommend` stage passes: under ARIA the database has just been rebuilt from the run's stored
    snapshot, and re-reading Smartsheet there would defeat the point — the recommendation and the report
    of one run must describe the same catalogue.
    """
    label = f"row {source_row}"
    try:
        if refresh_first and ingest_api.available():
            refresh(skip_images=True)
        if not db_exists():
            return ({"error": "The analysis database has not been built yet — refresh from "
                              "Smartsheet first."}, program_label(program), label)
        with RUN_LOCK, connect() as con:
            results, sr2pid = cap_recommend.recommend_program_presentations(
                con, program, vendor=vendor)
        # A JSON client may send the source_row as 12 or "12" while the map is keyed the other way, so
        # match on the string form rather than trusting the incoming type.
        pid = next((p for sr, p in sr2pid.items() if str(sr) == str(source_row)), None)
        res = results.get(pid) if pid is not None else None
        # `live=refresh_first`: when this call is snapshot-only, the label comes from the same rebuilt
        # database as the recommendation, not from a fresh look at the sheet.
        label = next((p["label"] for p in presentations(program, live=refresh_first)
                      if str(p["source_row"]) == str(source_row)), label)
        return res, program_label(program), label
    except SystemExit as exc:      # ingest_api.main() on a missing credential — a BaseException
        return {"error": f"Smartsheet refresh failed: {exc}"}, program_label(program), label
    except Exception as exc:
        return ({"error": f"Recommendation unavailable: {exc}"}, program_label(program), label)


def generate_report(program: str, presentation, refresh_first: bool = True, photo=None) -> dict:
    """Generate the PSA report for a product + presentation, with no uploaded documents.

    `process_documents([])` is the automated path: an empty doc list makes the extractors no-op, so
    Parts A & B come from the Smartsheet row for the chosen presentation.

    `refresh_first=False` reuses the database as it stands instead of rebuilding it from the live sheet —
    what the `report` stage passes, having already rebuilt from the run's snapshot. `photo` supplies the
    product image as bytes for the same reason: under ARIA there is no ingested image file on disk, so the
    stage reads the photo back from the run's media and passes it here.

    A failure is reported as `status="message"` rather than raised: the screen renders that status, and
    a 500 would lose the reason. `SystemExit` is caught explicitly because the Smartsheet ingest raises
    it on a missing credential, and being a BaseException it would otherwise escape.
    """
    try:
        with RUN_LOCK:
            return workflow.process_documents([], program_override=program,
                                              presentation=presentation,
                                              rebuild=refresh_first, photo=photo)
    except SystemExit as exc:
        return {"status": "message",
                "message": f"Report generation failed — Smartsheet is not configured: {exc}"}
    except Exception as exc:
        return {"status": "message", "message": f"Report generation failed: {exc}"}


def export_recommendation(program: str, source_row, vendor: str) -> tuple[str | None, str]:
    """Write the on-screen recommendation to a .docx. Returns (path_or_None, message)."""
    res, prog_label, label = recommendation(program, source_row, vendor)
    if res is None:
        return None, ("That presentation is not in the analysis database yet — refresh, then "
                      "recommend, then export.")
    if res.get("error"):
        # recommendation() reports failures as an error dict; cap_export.build would choke on one.
        return None, res["error"]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{program}_{source_row}")
    os.makedirs(paths.output_dir(), exist_ok=True)
    path = os.path.join(paths.output_dir(), f"cap_recommendation_{safe}.docx")
    cap_export.build(path, res, prog_label, label)
    return path, "Recommendation exported."
