"""Shared fixtures for the PSA tests: a populated catalogue, built offline.

Every PSA test that needs more than a pure function needs the same two things — a temp
workspace so nothing is written to the source tree, and a `psa.db` with enough products in it
to have comparators. Building that by hand in each file would be both repetitive and a source
of drift, so it lives here.

**Nothing here touches the network.** `sheet()` returns a dict in exactly the shape the
Smartsheet API replies with, and `snapshot.rebuild()` loads it through the real ingest path — so
the catalogue these tests run against is built by the same code production uses, from bytes in
the same canonical form a real run would have captured.

The columns carry a real `index`, because the loader reads each attribute by POSITION
(`ingest_smartsheet.COLS`) rather than by title. A fixture with only `id` and `title` rebuilds
into an empty catalogue and every assertion downstream passes vacuously.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

# The reference program the extractors were tuned against, and its two site-mates.
SUBJECT = "AGN-151586"
SUBJECT_ROW = 20


def load_psa():
    """Import the silo package and return the modules these fixtures need.

    Loading `silo.py` first is what registers the synthetic `da_silos.psa` package, so the
    ordinary `from da_silos.psa import ...` below resolves.
    """
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import config, paths, snapshot

    return config, paths, snapshot


def cell(cols, name, value):
    return {"columnId": 100 + cols[name], "value": value}


def row(cols, n, *, program, vial="2R", cap="Blue 6043", site="AP16",
        batch_type="Commercial", modality="Liquid", form="Liquid", colour="Colourless",
        status="Commercial", shape="Vial", marking="None"):
    """One presentation, with every column the report reads populated."""
    return {
        "rowNumber": SUBJECT_ROW + n,
        "id": 900 + n,
        "cells": [
            cell(cols, "program_no", program),
            cell(cols, "program_name", f"Product {n}"),
            cell(cols, "route_of_admin", "Intravenous"),
            cell(cols, "strength", "100 mg"),
            cell(cols, "target_fill_volume_ml", "2.0"),
            cell(cols, "vial_container_size", vial),
            cell(cols, "product_color", colour),
            cell(cols, "modality", modality),
            cell(cols, "contact", "a.person@abbvie.com"),
            cell(cols, "status", status),
            cell(cols, "list_number", f"NDC-{n:03d}"),
            cell(cols, "batch_type", batch_type),
            cell(cols, "_cap_color", cap),
            cell(cols, "_cap_vendor", "Datwyler"),
            cell(cols, "_dp_mfr", site),
            cell(cols, "_dp_pkging", site),
            cell(cols, "launch_year", "2027"),
            cell(cols, "form", form),
            cell(cols, "shape", shape),
            cell(cols, "marking", marking),
        ],
    }


def sheet(rows=None):
    """A sheet in the shape `ingest_smartsheet_api.capture` returns.

    The default catalogue is deliberately shaped to exercise the risk engine: three products at
    one site (so there are comparators), one of them near-identical to the subject and one
    clearly different, plus a product at a second site and a clinical row that scope must drop.
    """
    from da_silos.psa.ingest_smartsheet import COLS

    columns = [{"id": 100 + index, "index": index, "title": title}
               for title, index in sorted(COLS.items(), key=lambda kv: kv[1])]
    if rows is None:
        rows = [
            row(COLS, 0, program=SUBJECT),                                   # the subject
            row(COLS, 1, program="ABBV-400", cap="Red 6070"),                # same vial, diff cap
            row(COLS, 2, program="ABBV-401", vial="10R", cap="White 6003",
                colour="Yellow", shape="Cartridge"),                          # clearly different
            row(COLS, 3, program="ABBV-402", site="LU"),                     # another site
            row(COLS, 4, program="ABBV-403", batch_type="Clinical"),         # out of scope
            row(COLS, 5, program="ABBV-404", modality="Lyo Powder",
                form="Lyophilised powder"),                                   # other family
        ]
    return {"name": "NBE (Vials)", "columns": columns, "rows": rows}


def snapshot_bytes(sheet_doc=None, sheet_id="SHEET-1") -> bytes:
    """Bytes in exactly the form `snapshot.capture_bytes` produces (sorted keys, UTF-8)."""
    return json.dumps(
        {"sheet_id": sheet_id, "sheet": sheet() if sheet_doc is None else sheet_doc},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture
def psa_config(tmp_path):
    """A PSA configuration rooted in a temp directory, restored afterwards.

    `set_config` writes a module-level global, so a test that leaves it set would send the next
    test's paths somewhere unexpected — hence the restore.
    """
    config, _paths, _snapshot = load_psa()
    before = config._INJECTED
    cfg = dataclasses.replace(
        config.defaults(),
        work_dir=str(tmp_path),
        db_path=str(tmp_path / "Database" / "psa.db"),
        output_dir=str(tmp_path / "Output_Files"),
        uploads_dir=str(tmp_path / "uploads"),
        storage_dir=str(tmp_path / "store"),
        smartsheet_token=REDACTED
        smartsheet_sheet_id="SHEET-1",
    )
    config.set_config(cfg)
    yield cfg
    config._INJECTED = before


@pytest.fixture
def catalogue(psa_config):
    """A populated `psa.db`, built from the fixture sheet through the real ingest path.

    Returns the row counts `snapshot.rebuild` reports, so a test can assert against what was
    actually loaded rather than against what the fixture intended.
    """
    _config, _paths, snapshot = load_psa()
    return snapshot.rebuild(snapshot_bytes())


@pytest.fixture
def con(catalogue):
    """A read-write connection to the populated catalogue."""
    _config, paths, _snapshot = load_psa()
    connection = sqlite3.connect(paths.db_path())
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture
def empty_db(psa_config):
    """A schema-only catalogue: every table present, no rows.

    For the "no data" paths, which are a different branch from "no database".
    """
    _config, paths, _snapshot = load_psa()
    from da_silos.psa import build_db

    build_db.main()
    connection = sqlite3.connect(paths.db_path())
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def subject_product_id(con, program=SUBJECT):
    """This run's `product_id` for a program. Not stable across rebuilds, which is why the
    Smartsheet row is the key everywhere a caller supplies one."""
    got = con.execute(
        "SELECT product_id FROM product WHERE program_no=? ORDER BY source_row", (program,)
    ).fetchone()
    return got[0] if got else None


def docx_text(path: str) -> str:
    """Every paragraph and table cell of a .docx, joined — for asserting on a produced report."""
    from docx import Document

    document = Document(path)
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for table_row in table.rows:
            parts += [c.text for c in table_row.cells]
    return "\n".join(parts)


def generated_report(program=SUBJECT, presentation=SUBJECT_ROW, **kwargs):
    """Run the real report path offline and return its result dict.

    `refresh_first=False` is what keeps it offline: the catalogue is already built, so the
    engine reuses it instead of re-reading Smartsheet.
    """
    from da_silos.psa import engines

    return engines.generate_report(program, presentation, refresh_first=False, **kwargs)
