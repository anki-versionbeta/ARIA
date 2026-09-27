"""The snapshot, the derived database it rebuilds, and the shipped assets both read.

`snapshot.py` is the answer to PSA's oldest architectural problem. `build_db.main()` deletes and
recreates the whole database, and every recommend/report/refresh used to do that against the live
sheet — so two requests raced over one file and two runs of "the same" report could legitimately
differ because the sheet moved underneath them.

The fix is capture-once: a run reads the sheet a single time and every later stage rebuilds from
those bytes. Two properties follow, and they are what this file tests:

  * **the bytes are canonical**, so "did the catalogue change?" is a byte comparison. That is what
    `sort_keys=True` buys, and it only holds if key order in the API's reply cannot leak through;
  * **the rebuild is deterministic**, so a run's recommendation and its report necessarily describe
    the same catalogue.

`build_db.py` is both the DDL runner and the seed for `template_field_map`, which is the
data-driven map that drives `populate_template.py`. A missing seed row does not fail — it silently
leaves a cell blank in a GxP form — so the seed is pinned here.

`asset_store.py` is read-only access to the three files that ship with the silo. Its path guard is
tested as a security property: `_resolve` is the only thing standing between a relative path and
anything else on the worker's disk.
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

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)

def sheet(rows=2):
    """A sheet in the shape the Smartsheet API returns.

    Columns carry an `index`, because the loader sorts on it and reads each attribute by
    position (`ingest_smartsheet.COLS`) rather than by title — so a fixture with only `id`
    and `title` rebuilds into nothing and every determinism test would pass vacuously.
    """
    from da_silos.psa.ingest_smartsheet import COLS

    columns = [{"id": 100 + index, "index": index, "title": title}
               for title, index in sorted(COLS.items(), key=lambda kv: kv[1])]
    return {
        "name": "NBE (Vials)",
        "columns": columns,
        "rows": [
            {"rowNumber": 20 + n, "id": 900 + n, "cells": [
                {"columnId": 100 + COLS["program_no"], "value": f"AGN-15158{n}"},
                {"columnId": 100 + COLS["program_name"], "value": f"Product {n}"},
                {"columnId": 100 + COLS["vial_container_size"], "value": "2R"},
                {"columnId": 100 + COLS["batch_type"], "value": "Commercial"},
                {"columnId": 100 + COLS["status"], "value": "Commercial"},
                {"columnId": 100 + COLS["form"], "value": "Liquid"},
                {"columnId": 100 + COLS["modality"], "value": "Liquid"},
                {"columnId": 100 + COLS["_dp_mfr"], "value": "AP01"},
            ]}
            for n in range(rows)
        ],
    }


def raw_bytes(sheet_doc=None, sheet_id="SHEET-1") -> bytes:
    """Bytes in exactly the shape `snapshot.capture_bytes` produces."""
    return json.dumps(
        {"sheet_id": sheet_id, "sheet": sheet() if sheet_doc is None else sheet_doc},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import asset_store, build_db, config, paths, snapshot

    return {"snapshot": snapshot, "build_db": build_db, "asset_store": asset_store,
            "config": config, "paths": paths}


@pytest.fixture(autouse=True)
def restore_config(psa):
    before = psa["config"]._INJECTED
    yield
    psa["config"]._INJECTED = before


@pytest.fixture
def workspace(psa, tmp_path):
    """A configuration rooted in a temp directory, so `build_db` writes nowhere real."""
    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(),
        work_dir=str(tmp_path),
        db_path=str(tmp_path / "Database" / "psa.db"),
        output_dir=str(tmp_path / "Output_Files"),
        uploads_dir=str(tmp_path / "uploads"),
        storage_dir=str(tmp_path / "store"),
    ))
    return tmp_path


# ── capturing: the bytes have to be canonical ─────────────────────────────────


def test_a_capture_records_the_sheet_and_its_id(psa, monkeypatch):
    """The id travels with the bytes because a snapshot without it cannot say which sheet it
    is a snapshot OF — which is the first thing an auditor asks."""
    from da_silos.psa import ingest_smartsheet_api

    monkeypatch.setattr(
        ingest_smartsheet_api, "capture", lambda sid=None: (sheet(), "SHEET-1")
    )

    doc = json.loads(psa["snapshot"].capture_bytes().decode("utf-8"))

    assert doc["sheet_id"] == "SHEET-1"
    assert doc["sheet"]["name"] == "NBE (Vials)"


def test_the_captured_bytes_are_key_ordered(psa, monkeypatch):
    """`sort_keys=True` is what makes the snapshot comparable between runs. Without it, the
    same sheet content could produce different bytes depending on the order the API replied
    in, and "did the catalogue change?" would stop being a byte comparison."""
    from da_silos.psa import ingest_smartsheet_api

    ordered = sheet()
    shuffled = {"rows": ordered["rows"], "name": ordered["name"],
                "columns": ordered["columns"]}
    monkeypatch.setattr(ingest_smartsheet_api, "capture", lambda sid=None: (shuffled, "S"))

    first = psa["snapshot"].capture_bytes()

    monkeypatch.setattr(ingest_smartsheet_api, "capture", lambda sid=None: (ordered, "S"))
    second = psa["snapshot"].capture_bytes()

    assert first == second, "key order in the reply must not change the bytes"


def test_the_capture_is_utf8_with_real_characters(psa, monkeypatch):
    """`ensure_ascii=False` keeps the sheet's text readable in the stored audit record —
    a reviewer should not have to decode \\u escapes to read a product name."""
    from da_silos.psa import ingest_smartsheet_api

    accented = {**sheet(), "name": "Vials — Européen"}
    monkeypatch.setattr(ingest_smartsheet_api, "capture", lambda sid=None: (accented, "S"))

    raw = psa["snapshot"].capture_bytes()

    assert "Européen" in raw.decode("utf-8")


def test_the_snapshot_is_named_for_what_it_is(psa):
    """It names an object in the store, so renaming it orphans every run already made."""
    assert psa["snapshot"].SNAPSHOT_MEDIA == "smartsheet.json"


# ── describing a snapshot without rebuilding it ───────────────────────────────


def test_describing_a_snapshot_counts_its_rows(psa):
    """Cheap facts for the progress line and the checkpoint, so a reviewer can see the shape
    of what was captured without opening the database."""
    from da_silos.psa.ingest_smartsheet import COLS

    facts = psa["snapshot"].describe(raw_bytes())

    assert facts["rows"] == 2
    assert facts["columns"] == len(COLS)
    assert facts["sheet_id"] == "SHEET-1"
    assert facts["sheet_name"] == "NBE (Vials)"


def test_the_described_size_is_the_stored_size(psa):
    """It is the number a reviewer compares against the object in the store."""
    raw = raw_bytes()

    assert psa["snapshot"].describe(raw)["bytes"] == len(raw)


def test_describing_an_empty_sheet_does_not_fail(psa):
    """An empty sheet is a real answer from a misconfigured sheet id, and the run should
    report zero rows rather than crash before it can say anything."""
    facts = psa["snapshot"].describe(raw_bytes({}))

    assert facts["rows"] == 0
    assert facts["columns"] == 0


def test_describing_a_snapshot_with_no_sheet_at_all_does_not_fail(psa):
    raw = json.dumps({"sheet_id": "S"}).encode("utf-8")

    assert psa["snapshot"].describe(raw)["rows"] == 0


def test_describing_does_not_touch_the_database(psa, workspace):
    """The point of `describe` over `rebuild`: the progress line must not cost a rebuild."""
    psa["snapshot"].describe(raw_bytes())

    assert not os.path.exists(psa["paths"].db_path())


# ── rebuilding: deterministic, and offline ────────────────────────────────────


def test_a_rebuild_loads_the_snapshot_into_a_fresh_database(psa, workspace):
    """The row counts are what a stage puts in its checkpoint, so a person reading the run
    can see what the snapshot actually contained."""
    result = psa["snapshot"].rebuild(raw_bytes())

    assert os.path.exists(psa["paths"].db_path())
    assert result["products"] == 2

    con = sqlite3.connect(psa["paths"].db_path())
    programs = {r[0] for r in con.execute("SELECT program_no FROM product")}
    con.close()
    assert programs == {"AGN-151580", "AGN-151581"}, (
        "the rows must really be loaded, or every determinism test below passes vacuously"
    )


def test_a_rebuild_reads_no_network(psa, workspace, monkeypatch):
    """The whole design rests on this: only `fetch` touches Smartsheet. A rebuild that
    reached out would make a resumed run depend on the sheet not having moved."""
    from da_silos.psa import ingest_smartsheet_api

    def forbidden(*args, **kwargs):
        raise AssertionError("the rebuild must not read Smartsheet")

    monkeypatch.setattr(ingest_smartsheet_api, "capture", forbidden)
    monkeypatch.setattr(ingest_smartsheet_api, "_fetch_sheet", forbidden, raising=False)

    psa["snapshot"].rebuild(raw_bytes())


def test_the_same_bytes_rebuild_the_same_catalogue(psa, workspace):
    """Determinism, stated as the property the audit trail claims: a run's recommendation and
    its report are guaranteed to describe the same catalogue."""
    first = psa["snapshot"].rebuild(raw_bytes())
    second = psa["snapshot"].rebuild(raw_bytes())

    assert first == second


def test_a_rebuild_replaces_the_previous_database(psa, workspace):
    """`build_db.main()` drops and recreates, so a stale row from an earlier snapshot cannot
    survive into a later assessment."""
    psa["snapshot"].rebuild(raw_bytes())
    con = sqlite3.connect(psa["paths"].db_path())
    con.execute(
        "INSERT INTO product (product_id, program_no) VALUES (9999, 'STALE')"
    )
    con.commit()
    con.close()

    psa["snapshot"].rebuild(raw_bytes())

    con = sqlite3.connect(psa["paths"].db_path())
    remaining = con.execute(
        "SELECT count(*) FROM product WHERE program_no='STALE'"
    ).fetchone()[0]
    con.close()
    assert remaining == 0


def test_rebuilding_malformed_bytes_fails_rather_than_building_an_empty_catalogue(psa,
                                                                                 workspace):
    """A truncated snapshot must not produce an assessment of nothing, which would read as
    "no similar products" — the opposite of "the snapshot is unreadable"."""
    with pytest.raises(Exception):
        psa["snapshot"].rebuild(b"not json at all")


# ── the derived database and its seed ─────────────────────────────────────────


def test_building_the_database_creates_every_table(psa, workspace):
    """From the shipped DDL, so a table the engine reads cannot be missing in production."""
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()

    for table in ("product", "site", "vendor", "product_site", "cap_color",
                  "template_field_map", "validation_rule", "field_provenance"):
        assert table in names, table


def test_the_template_field_map_is_seeded(psa, workspace):
    """It is the data-driven map that drives `populate_template.py`. A missing row does not
    fail — it silently leaves a cell blank in a GxP form."""
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    count = con.execute("SELECT count(*) FROM template_field_map").fetchone()[0]
    con.close()

    assert count == len(psa["build_db"].TEMPLATE_FIELDS)


def test_the_seeded_map_covers_every_part_the_template_fills(psa, workspace):
    """Part C is signature-only and Part E is rendered dynamically per site, so A, B, D1 and
    D2 are the parts this static map must carry."""
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    parts = {r[0] for r in con.execute("SELECT DISTINCT part FROM template_field_map")}
    con.close()

    assert {"A", "B", "C", "D1", "D2"} <= parts


def test_the_validation_rules_are_seeded(psa, workspace):
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    count = con.execute("SELECT count(*) FROM validation_rule").fetchone()[0]
    con.close()

    assert count == len(psa["build_db"].VALIDATION_RULES)


def test_the_required_fields_are_the_ones_a_form_cannot_omit(psa):
    """Pinned because they are what turns a blank cell into a reported validation failure
    rather than a silently incomplete assessment."""
    required = {name for name, rule, _, _ in psa["build_db"].VALIDATION_RULES
                if rule == "required"}

    assert required == {"list_number", "form", "vial_container_size", "product_color"}


def test_building_the_database_twice_starts_clean(psa, workspace):
    """Every run rebuilds, so the seed must not accumulate duplicate rows."""
    psa["build_db"].main()
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    count = con.execute("SELECT count(*) FROM template_field_map").fetchone()[0]
    con.close()

    assert count == len(psa["build_db"].TEMPLATE_FIELDS)


def test_the_build_creates_its_parent_directory(psa, workspace):
    """It is the first step of every run, so it is the natural place to guarantee the
    writable directories exist: a fresh container has no Database/ directory, and sqlite
    will not create one for you."""
    assert not os.path.exists(os.path.dirname(psa["paths"].db_path()))

    psa["build_db"].main()

    assert os.path.isdir(os.path.dirname(psa["paths"].db_path()))


def test_the_schema_passes_its_own_foreign_key_check(psa, workspace):
    """`verify.py` runs this against a populated database; here it proves the shipped DDL is
    self-consistent before any data is loaded."""
    psa["build_db"].main()

    con = sqlite3.connect(psa["paths"].db_path())
    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()

    assert violations == []


# ── the shipped assets ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["cap_palette.csv", "schema.sql", "PSA_template.docx"])
def test_every_documented_asset_ships(psa, name):
    """All three are load-bearing: the palette is the catalogue, the schema is the database,
    and the template is the form itself. A missing one fails at report time, not at start-up."""
    assert psa["asset_store"].exists(name), name
    assert psa["asset_store"].read(name)


def test_an_asset_can_be_read_as_text(psa):
    assert "CREATE TABLE" in psa["asset_store"].read_text("schema.sql")


def test_the_template_is_a_real_docx(psa):
    """A .docx is a zip; 'PK' is its magic number. A truncated template would otherwise
    fail deep inside python-docx at report time."""
    assert psa["asset_store"].read("PSA_template.docx")[:2] == b"PK"


def test_a_missing_asset_reports_absent_rather_than_raising(psa):
    """`exists` is a question, and callers use it to choose a fallback."""
    assert psa["asset_store"].exists("no_such_asset.csv") is False


def test_reading_a_missing_asset_raises(psa):
    """Because by then the caller has committed to needing it, and a silent empty result
    would be a blank palette or an empty schema."""
    with pytest.raises(OSError):
        psa["asset_store"].read("no_such_asset.csv")


@pytest.mark.parametrize("escape", [
    "../config.py", "../../__init__.py", "subdir/../../config.py",
])
def test_a_path_that_escapes_the_assets_directory_is_refused(psa, escape):
    """The only thing between a relative path and the rest of the worker's disk. `exists`
    answers False rather than raising, because a caller asking a question should not have to
    catch — but `read` raises, because by then it is an attempt."""
    assert psa["asset_store"].exists(escape) is False
    with pytest.raises(ValueError, match="escapes the assets directory"):
        psa["asset_store"].read(escape)


def test_an_absolute_path_is_refused(psa):
    """`os.path.join` discards everything before an absolute component, so this is the case
    a naive join silently allows."""
    with pytest.raises(ValueError, match="escapes the assets directory"):
        psa["asset_store"].read(os.path.abspath(__file__))


def test_an_asset_is_cached_after_the_first_read(psa):
    """These files are immutable for the life of a deployment, and the gold template is read
    on every run."""
    psa["asset_store"].read("schema.sql")

    assert "schema.sql" in psa["asset_store"]._CACHE


def test_the_cache_can_be_bypassed(psa):
    """For the rare caller that wants to see an edit made while the process is running."""
    first = psa["asset_store"].read("schema.sql")
    second = psa["asset_store"].read("schema.sql", cache=False)

    assert first == second


def test_a_bypassed_read_is_not_cached(psa):
    psa["asset_store"]._CACHE.pop("cap_palette.csv", None)

    psa["asset_store"].read("cap_palette.csv", cache=False)

    assert "cap_palette.csv" not in psa["asset_store"]._CACHE


def test_the_asset_store_offers_no_way_to_write(psa):
    """Modelled on the platform's `SiloAssets`, which exposes `read`/`read_text`/`exists` and
    no `put` — a silo reads its own assets and never rewrites them. That constraint is why
    the palette override is written through an object store instead."""
    assert not hasattr(psa["asset_store"], "put")
    assert not hasattr(psa["asset_store"], "write")
