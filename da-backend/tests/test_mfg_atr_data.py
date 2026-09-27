"""The ATR query pack, the data path and the GxP audit record.

The SQL is exercised against a fake connection: a unit test that needed Oracle would be
testing the network. What is pinned here is the SQL *text* (it is recorded verbatim in the
audit trail, so it is part of the compliance artefact), the row shaping, the no-data gate,
and the audit record's shape.

The real captured fixture is warehouse data and is deliberately **not** committed. Set
`ATR_FIXTURE` to `tests/fixtures/cmc10352.json` in the source repo to exercise it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from da_platform.settings import REPO_ROOT
from da_platform.silo_registry import _load_module

SILO_DIR = REPO_ROOT / "silos" / "mfg_atr"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "atr_data.py").is_file(), reason="the ATR pipeline is not present"
)


@pytest.fixture(scope="module")
def atr():
    _load_module("mfg_atr", SILO_DIR / "silo.py")
    return {
        "data": _load_module("mfg_atr", SILO_DIR / "atr_data.py", name="atr_data"),
        "audit": _load_module("mfg_atr", SILO_DIR / "audit.py", name="audit"),
    }


class FakeCursor:
    """Enough of an oracledb cursor for the row shaper: description + fetchall."""

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, **binds):
        self._connection.executed.append((sql, binds))
        self.description = [("REQUEST_ID",), ("ROW_COUNT",)]
        return self

    def fetchall(self):
        return [("PEGA-PROD-CMC-10352", 42)]


class FakeConnection:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def cursor(self):
        return FakeCursor(self)


# A raw snapshot with the shape the builders consume. Synthetic on purpose: the captured
# fixture is real warehouse data.
RAW = {
    "header": [
        {
            "atr_section": "Header",
            "cmc_request_id": "PEGA-PROD-CMC-10352",
            "project_code": "ABBV-000",
            "batch_sample_ids": "BA00006111",
        }
    ],
    "methods": [
        {
            "eln_unique_id": "EXP-1",
            "test_description": "Appearance",
            "test_method_reference": "M-001",
        }
    ],
    "results": [
        {
            "experiment_id": "EXP-1",
            "technique": "HPLC",
            "result_name": "Purity",
            "analyte_name": "Main",
            "sample_id": "S1",
            "sample_name": "Day0",
            "batch_id": "BA00006111",
            "reported_value": "99.1",
            "value_source": "reported",
            "units": "%",
        }
    ],
    "scope_summary": [
        {"summary_exp_id": "EXP-9", "scope": "Scope text", "summary_conclusion": "Conforms"}
    ],
    "validation": [
        {
            "request": "PEGA-PROD-CMC-10352",
            "result_rows": 1,
            "n_experiments": 1,
            "n_samples": 1,
            "n_batches": 1,
        }
    ],
    "batch_lots": [{"batch_id": "BA00006111", "lot_display": "1001707466"}],
}


# ── the query pack ────────────────────────────────────────────────────────────


def test_the_pack_has_the_six_audited_queries(atr):
    queries = atr["data"].build_queries("DEVSCI_DM", "mv_combined_results")

    assert sorted(queries) == [
        "batch_lots",
        "header",
        "methods",
        "results",
        "scope_summary",
        "validation",
    ]


def test_the_object_names_are_resolved_into_the_sql(atr):
    """The source built these at import time from the environment. A silo cannot read the
    environment, so they are resolved at call time — the SQL text is unchanged."""
    queries = atr["data"].build_queries("DEVSCI_DM", "mv_combined_results")

    assert "DEVSCI_DM.mv_combined_results" in queries["results"]
    assert "DEVSCI_DM.requests" in queries["header"]
    assert "{" not in queries["results"], "no unsubstituted placeholder may reach Oracle"


def test_a_different_schema_reaches_the_sql(atr):
    """Swapping the DEV materialised view for the PROD view is a configuration change."""
    queries = atr["data"].build_queries("PROD_DM", "combined_results_vw")

    assert "PROD_DM.combined_results_vw" in queries["results"]
    assert "DEVSCI_DM" not in queries["results"]


def test_every_query_binds_the_request_id(atr):
    """One bind, recorded in the audit trail as `bind_values`. A query that interpolated
    the id would be both an injection risk and an audit lie."""
    queries = atr["data"].build_queries("DEVSCI_DM", "mv_combined_results")

    for name, sql in queries.items():
        assert ":request_id" in sql, name


def test_the_request_id_picker_is_not_part_of_the_audited_pack(atr):
    """The source is explicit: it is a convenience lookup, not part of the per-request
    Query Pack, so it must not appear in the audited queries."""
    queries = atr["data"].build_queries("DEVSCI_DM", "mv_combined_results")
    picker = atr["data"].build_request_ids_query("DEVSCI_DM", "mv_combined_results")

    assert picker not in queries.values()
    assert ":request_id" not in picker


# ── running them ──────────────────────────────────────────────────────────────


def test_fetch_all_runs_every_query_with_the_query_form_of_the_id(atr):
    conn = FakeConnection()

    raw = atr["data"].fetch_all(conn, "PEGA-PROD-CMC-10352", "DEVSCI_DM", "mv_combined_results")

    assert sorted(raw) == [
        "batch_lots",
        "header",
        "methods",
        "results",
        "scope_summary",
        "validation",
    ]
    assert len(conn.executed) == 6
    for _sql, binds in conn.executed:
        assert binds == {"request_id": "PEGA-PROD-CMC-10352"}


def test_rows_come_back_as_dicts_with_lowercased_columns(atr):
    """Oracle reports column names uppercase; every downstream builder reads lowercase."""
    conn = FakeConnection()

    raw = atr["data"].fetch_all(conn, "PEGA-PROD-CMC-10352", "DEVSCI_DM", "mv_combined_results")

    assert raw["header"] == [{"request_id": "PEGA-PROD-CMC-10352", "row_count": 42}]


def test_a_fixture_is_loaded_straight_from_json(atr, tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(RAW), encoding="utf-8")

    assert atr["data"].fetch_fixture(path) == RAW


# ── the no-data gate ──────────────────────────────────────────────────────────


def test_has_data_trusts_the_validation_count(atr):
    assert atr["data"].has_data(RAW) is True


def test_no_rows_means_no_data(atr):
    empty = dict(RAW, validation=[{"result_rows": 0}], header=[], results=[])

    assert atr["data"].has_data(empty) is False


def test_a_null_count_is_taken_at_face_value(atr):
    """Subtle, and faithful to the source: `result_rows: None` becomes `None or 0` → 0,
    which raises nothing, so the header/results fallback is **never reached** and the answer
    is "no data" even though header rows exist.

    Pinned because it is surprising — a reader would reasonably expect the fallback here.
    """
    raw = {"validation": [{"result_rows": None}], "header": [{"a": 1}]}

    assert atr["data"].has_data(raw) is False


def test_an_unparseable_count_falls_back_to_header_or_results(atr):
    """The fallback exists for a count that cannot be read at all, which is what the
    try/except is guarding — a wrong "no data" answer looks like a missing request."""
    assert atr["data"].has_data({"validation": [{"result_rows": "many"}], "header": [{"a": 1}]}) is True
    assert atr["data"].has_data({"validation": [{}], "results": [{"a": 1}]}) is False


def test_no_validation_row_falls_back_to_header_or_results(atr):
    assert atr["data"].has_data({"validation": [], "results": [{"a": 1}]}) is True
    assert atr["data"].has_data({}) is False


def test_a_fixture_missing_a_query_key_does_not_break_the_gate(atr):
    """The repo's own captured ATR fixture predates the `batch_lots` query and has only 5
    of the 6 keys, so nothing may assume every key is present."""
    without_lots = {k: v for k, v in RAW.items() if k != "batch_lots"}

    assert atr["data"].has_data(without_lots) is True


# ── the GxP audit record ──────────────────────────────────────────────────────


def test_the_audit_record_keeps_every_field(atr):
    """ALCOA+: who, when, the exact SQL, the binds, row counts, a hash of the source data,
    the review flags and the output. Field names are part of the artefact."""
    queries = atr["data"].build_queries("DEVSCI_DM", "mv_combined_results")
    record = atr["audit"].build_record(
        ids={"short": "10352", "display": "CMC-10352", "query": "PEGA-PROD-CMC-10352"},
        id_field="request_id",
        bind_field="request_id",
        queries=queries,
        raw=RAW,
        flags=[],
        output_path="ATR_CMC-10352.pdf",
        generated_at="2026-08-07T00:00:00+00:00",
        app_user={"username": "asha.rao", "full_name": "Asha Rao", "email": "a@b.c"},
    )

    assert sorted(record) == sorted(
        [
            "timestamp_utc",
            "os_user",
            "app_user",
            "host",
            "request_id",
            "queries",
            "bind_values",
            "row_counts",
            "source_data_sha256",
            "review_flags",
            "output_path",
            "finalized",
        ]
    )
    assert record["bind_values"] == {"request_id": "PEGA-PROD-CMC-10352"}
    assert record["row_counts"]["results"] == 1
    assert record["finalized"] is False
    assert record["app_user"]["username"] == "asha.rao"


def test_the_audit_records_the_sql_that_actually_ran(atr):
    """Not a template. Resolving the object names at call time makes this strictly more
    truthful than the original, which stored whatever the environment happened to be."""
    queries = atr["data"].build_queries("PROD_DM", "combined_results_vw")
    record = atr["audit"].build_record(
        ids={"query": "PEGA-PROD-CMC-1"},
        id_field="request_id",
        bind_field="request_id",
        queries=queries,
        raw=RAW,
        flags=[],
        output_path="x.pdf",
        generated_at="2026-08-07T00:00:00+00:00",
    )

    assert "PROD_DM.combined_results_vw" in record["queries"]["results"]


def test_the_source_data_hash_is_stable_and_content_sensitive(atr):
    """The hash is what lets a reviewer prove the report came from this data."""
    build = atr["audit"].build_record
    common = dict(
        ids={"query": "q"},
        id_field="request_id",
        bind_field="request_id",
        queries={},
        flags=[],
        output_path="x.pdf",
        generated_at="2026-08-07T00:00:00+00:00",
    )

    first = build(raw=RAW, **common)["source_data_sha256"]
    again = build(raw=json.loads(json.dumps(RAW)), **common)["source_data_sha256"]
    changed = build(raw=dict(RAW, results=[]), **common)["source_data_sha256"]

    assert first == again, "the same data must hash the same"
    assert first != changed
    assert len(first) == 64


def test_mfgr_uses_its_own_id_field(atr):
    """MFGR's record carries `batch_id` and binds `batch_root`; ATR uses `request_id` for both.
    Both names are visible in the compliance artefact, so the caller supplies each."""
    record = atr["audit"].build_record(
        ids={"short": "BAX000584", "query": "nest-br-prod-BAX000584"},
        id_field="batch_id",
        bind_field="batch_root",
        queries={},
        raw=RAW,
        flags=[],
        output_path="MFGR_BAX000584.pdf",
        generated_at="2026-08-07T00:00:00+00:00",
    )

    assert "batch_id" in record
    assert "request_id" not in record
    # The bind name is NOT the record key for MFGR: its queries bind `:batch_root`. A
    # hardcoded "request_id" here would make the record name a bind the query never used.
    assert record["bind_values"] == {"batch_root": "nest-br-prod-BAX000584"}


def test_flags_survive_as_data(atr):
    """Review flags are part of the record, and they arrive as dataclasses."""
    from dataclasses import dataclass

    @dataclass
    class Flag:
        severity: str
        area: str
        message: str

    record = atr["audit"].build_record(
        ids={"query": "q"},
        id_field="request_id",
        bind_field="request_id",
        queries={},
        raw=RAW,
        flags=[Flag("warning", "results", "Unknown unit URI")],
        output_path="x.pdf",
        generated_at="2026-08-07T00:00:00+00:00",
    )

    assert record["review_flags"] == [
        {"severity": "warning", "area": "results", "message": "Unknown unit URI"}
    ]


# ── the real captured fixture, when available ─────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("ATR_FIXTURE"),
    reason="set ATR_FIXTURE to the captured cmc10352.json to exercise real warehouse data",
)
def test_the_captured_fixture_is_usable(atr):
    raw = atr["data"].fetch_fixture(Path(os.environ["ATR_FIXTURE"]))

    assert atr["data"].has_data(raw) is True
    # Documented, not asserted away: this fixture predates the batch_lots query.
    missing = {"header", "methods", "results", "scope_summary", "validation"} - set(raw)
    assert not missing, f"the captured fixture is missing {missing}"
