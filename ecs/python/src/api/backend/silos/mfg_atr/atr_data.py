"""CMC Data Warehouse access — the ATR Query Pack (ATR_Query/ATR_Query.docx).

Ported from `src/data_access.py`. Runs the six queries against the configured results
object, parameterized by a single `:request_id` bind (the pack's &CMC literal). A fixture
mode loads captured JSON instead of connecting, so all downstream logic is testable
offline.

**One structural change, and the SQL text is untouched by it.** The source built each
statement at *import* time by interpolating `DWH_SCHEMA` and `RESULTS_OBJECT`, both read
from the environment. A silo may not read the environment, so the statements are built at
*call* time from values the platform supplies (`ctx.warehouse.schema` /
`.results_object`). Every character of SQL is otherwise as it was.

That change is visible in the audit trail, and for the better: `record_generation` stores
the queries verbatim, so it now records the SQL that actually ran rather than a template
whose meaning depended on the environment at the time.

The connection itself comes from `ctx.warehouse` — this module never opens one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_queries(schema: str, results_object: str) -> dict[str, str]:
    """The six audited statements, resolved against the configured objects.

    Returned as a dict so the caller can hand it straight to the audit record, which is
    what makes the trail reproducible.
    """
    mv = f"{schema}.{results_object}"

    # Q1 — Header (request + project + all batch/sample ids). Slow join; runs once.
    header = f"""
SELECT 'Header' AS atr_section,
       r.request_id                                   AS cmc_request_id,
       COALESCE(MAX(vb.project_name),
                MAX(mcr.batch_project),
                MAX(mcr.request_project))              AS project_code,
       LISTAGG(DISTINCT mcr.batch_id, ', ')
         WITHIN GROUP (ORDER BY mcr.batch_id)          AS batch_sample_ids
FROM   {schema}.requests r
JOIN   {mv} mcr ON mcr.request_id = r.request_id
LEFT   JOIN {schema}.view_batch_properties vb ON vb.batch_id = mcr.batch_id
WHERE  r.request_id = :request_id
GROUP  BY r.request_id
"""

    # Q2 — §3 Analytical Methods (one row per technique x method reference).
    methods = f"""
SELECT DISTINCT
       mcr.experiment_id                                            AS eln_unique_id,
       NVL(REGEXP_SUBSTR(mcr.result_header_name,'[^-]+$'),
           mcr.result_header_name)                                  AS test_description,
       mcr.method_id                                                AS test_method_reference
FROM   {mv} mcr
WHERE  mcr.request_id = :request_id
ORDER  BY eln_unique_id, test_method_reference
"""

    # Q3 — §4 Results (business-aligned): reported_value first; analyte = peak_name; units decoded.
    # Superset that also carries the columns needed for STABILITY ATRs:
    #   * sample_name / batch_id / detail_context,
    #   * particle_size_um  = the MFI 'ECD' row paired to its count via PARAMETER_GROUP_INDEX,
    #   * timepoint_parsed / storage_condition  parsed from SAMPLE_NAME (e.g. '12M5C').
    # Dev-samples shaping ignores the extra columns, so behaviour there is unchanged.
    results = f"""
WITH r AS (
  SELECT mcr.experiment_id,
         NVL(REGEXP_SUBSTR(mcr.result_header_name,'[^_-]+$'),
             mcr.result_header_name)                    AS technique,
         mcr.detail_param_name                          AS result_name,
         mcr.peak_name                                  AS analyte_name,
         mcr.sample_id,
         mcr.sample_name,
         mcr.batch_id,
         mcr.parameter_group_index,
         mcr.detail_context,
         -- Reported value actually lives in REPORTED_VALUE_STRING for the vast
         -- majority of rows (reported_value / _num are almost always NULL). Prefer
         -- it so the ATR shows the *reported* (rounded/sig-fig) value, not the raw
         -- actual. Fall back to actual only when no reported value exists.
         COALESCE(mcr.reported_value_string,
                  mcr.reported_value,
                  TO_CHAR(mcr.reported_value_num),
                  mcr.actual_value_string,
                  mcr.actual_value,
                  TO_CHAR(mcr.actual_value_num))         AS reported_value,
         CASE WHEN mcr.reported_value_string IS NOT NULL
                OR mcr.reported_value IS NOT NULL
                OR mcr.reported_value_num IS NOT NULL
              THEN 'reported' ELSE 'actual' END          AS value_source,
         CASE mcr.detail_param_unit
              WHEN 'http://qudt.org/vocab/unit/MilliGM-PER-MilliL' THEN 'mg/mL'
              WHEN 'http://qudt.org/vocab/unit/NTU'                THEN 'NTU'
              WHEN 'http://qudt.org/vocab/unit/MicroM'             THEN 'um'
              WHEN 'http://qudt.org/vocab/unit/KiloGM'             THEN 'kg'
              WHEN 'http://qudt.org/vocab/unit/PERCENT'            THEN '%'
              WHEN 'http://qudt.org/vocab/unit/MilliPA-SEC'        THEN 'mPa*s'
              WHEN 'http://qudt.org/vocab/unit/N'                  THEN 'N'
              WHEN 'https://ontology.abbvienet.com/dsdt/DSDT_0000133' THEN 'L'
              WHEN 'https://ontology.abbvienet.com/dsdt/DSDT_0000570' THEN 'particles'
              WHEN 'https://ontology.abbvienet.com/dsdt/DSDT_0000571' THEN 'particles'
              ELSE mcr.detail_param_unit
         END                                            AS units,
         mcr.timepoint, mcr.temperature
  FROM   {mv} mcr
  WHERE  mcr.request_id = :request_id
),
ecd AS (
  SELECT experiment_id, sample_id, parameter_group_index,
         MAX(actual_value_num) AS particle_size_um
  FROM   {mv}
  WHERE  request_id = :request_id AND detail_param_name = 'ECD'
  GROUP  BY experiment_id, sample_id, parameter_group_index
)
SELECT r.experiment_id, r.technique, r.result_name, r.analyte_name, r.sample_id,
       r.sample_name, r.batch_id, r.detail_context,
       r.reported_value, r.value_source, r.units, r.timepoint, r.temperature,
       e.particle_size_um,
       REGEXP_SUBSTR(r.sample_name,'^(Day[0-9]+|[0-9]+M)') AS timepoint_parsed,
       REGEXP_SUBSTR(r.sample_name,'[0-9]+C$')             AS storage_condition
FROM   r
LEFT   JOIN ecd e ON e.experiment_id = r.experiment_id
                 AND e.sample_id = r.sample_id
                 AND e.parameter_group_index = r.parameter_group_index
WHERE  r.result_name <> 'ECD'
ORDER  BY r.technique, r.batch_id, r.sample_name, e.particle_size_um, r.result_name
"""

    # Q4 — §1 Scope & §2 Summary/Conclusion (from the summary-experiment row).
    scope_summary = f"""
SELECT summary_exp_id, scope, summary_conclusion
FROM (
   SELECT mcr.summary_conclusion_experiment_id          AS summary_exp_id,
          mcr.scope,
          mcr.summary_conclusion,
          ROW_NUMBER() OVER (ORDER BY ROWNUM)            AS rn
   FROM   {mv} mcr
   WHERE  mcr.request_id = :request_id
     AND  mcr.summary_conclusion IS NOT NULL
)
WHERE  rn = 1
"""

    # Q5 — Validation counts.
    validation = f"""
SELECT :request_id AS request, COUNT(*) AS result_rows,
       COUNT(DISTINCT experiment_id) AS n_experiments,
       COUNT(DISTINCT sample_id) AS n_samples,
       COUNT(DISTINCT batch_id) AS n_batches
FROM   {mv}
WHERE  request_id = :request_id
"""

    # Q6 — Batch id -> human batch/lot number. For PEGA batches, LOTS.displayname holds the
    # DP/SAP lot number used in the manual report (e.g. pega-prod-BA00006111 -> 1001707466).
    batch_lots = f"""
SELECT DISTINCT mcr.batch_id, l.displayname AS lot_display
FROM   {mv} mcr
LEFT   JOIN {schema}.lots l ON l.id = mcr.batch_id
WHERE  mcr.request_id = :request_id
"""

    return {
        "header": header,
        "methods": methods,
        "results": results,
        "scope_summary": scope_summary,
        "validation": validation,
        "batch_lots": batch_lots,
    }


def build_request_ids_query(schema: str, results_object: str) -> str:
    """Distinct request ids that actually have combined-results data (for the UI picker).

    Deliberately NOT part of `build_queries` — it is a convenience lookup, not part of the
    audited per-request Query Pack.
    """
    return f"""
SELECT DISTINCT request_id
FROM   {schema}.{results_object}
WHERE  request_id IS NOT NULL
ORDER  BY request_id
"""


def _rows(cursor) -> list[dict[str, Any]]:
    cols = [c[0].lower() for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _run(conn, sql: str, request_id_query: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, request_id=request_id_query)
        return _rows(cur)


def fetch_all(
    conn, request_id_query: str, schema: str, results_object: str
) -> dict[str, list[dict[str, Any]]]:
    """Run every audited query with the same request id (live warehouse)."""
    queries = build_queries(schema, results_object)
    return {name: _run(conn, sql, request_id_query) for name, sql in queries.items()}


def fetch_validation(
    conn, request_id_query: str, schema: str, results_object: str
) -> list[dict[str, Any]]:
    """Run only Q5 (counts) — a cheap live-connection smoke test."""
    return _run(conn, build_queries(schema, results_object)["validation"], request_id_query)


def fetch_request_ids(conn, schema: str, results_object: str) -> list[str]:
    """Return every request id present in the results object (for the UI picker)."""
    with conn.cursor() as cur:
        cur.execute(build_request_ids_query(schema, results_object))
        return [row[0] for row in cur.fetchall() if row[0]]


def fetch_fixture(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load captured query output from a JSON fixture instead of connecting."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def has_data(raw: dict[str, Any]) -> bool:
    """Did the warehouse return anything for this request?

    Ported from `_has_data` in `api/routers/atr.py`. A false answer is **not** an error: it
    means the id was valid but the warehouse holds nothing for it, and the run finishes
    with an explanation instead of failing.

    The validation count is preferred, but an unreadable count falls back to the header and
    results rows rather than concluding "no data" — a wrong negative looks to the user like
    a missing request.
    """
    val = raw.get("validation") or []
    if val:
        try:
            return int(val[0].get("result_rows") or 0) > 0
        except (TypeError, ValueError):
            pass
    return bool(raw.get("header") or raw.get("results"))
