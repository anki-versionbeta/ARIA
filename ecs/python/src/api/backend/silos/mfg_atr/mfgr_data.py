"""CMC Data Warehouse access — the MFGR Query Pack. Ported from `src/mfgr_data_access.py`.

Runs the eight queries against DEVSCI_DM tables, parameterized by a single `:batch_root`
bind (e.g. 'nest-br-prod-BAX000584'). A fixture mode loads captured JSON instead of
connecting, so all downstream logic is testable offline.

Tables consumed:
  * REQUESTS                            — optional PA / request pairing
  * VIEW_BATCH_PROPERTIES               — dedicated typed columns per unit op
  * BATCH_PROPERTIES                    — name/value fallback (display_name lookup)
  * BATCH_COMPONENTS                    — DS lot + ingredient list
  * VIEW_FORMULATION_BATCHES + _COMPONENTS — composition with mg/mL amounts
  * MV_COMBINED_RESULTS                 — dev-sample results (Table 3), optional

**Two structural changes; the SQL text is untouched by both.**

1. The source built each statement at *import* time by interpolating `DWH_SCHEMA`, read
   from the environment. A silo may not read the environment, so the statements are built
   at *call* time from the schema the platform supplies (`ctx.warehouse.schema`). Every
   character of SQL is otherwise as it was. As with ATR, this improves the audit trail:
   `record_generation` stores the queries verbatim, so it records the SQL that actually ran
   rather than a template whose meaning depended on the environment.

2. `get_connection` and its 4-attempt retry are gone. The platform owns connections — see
   `api.backend.da_platform.warehouse`, which carries the same retry intent. This module never opens
   one.

`fetch_report` deliberately skips the validation query: its six `COUNT(*)` sub-selects are
the slowest part of the pack and `build_mfgr_report` does not consume them. That was the
source's main per-request speed-up and is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_queries(schema: str) -> dict[str, str]:
    """The eight audited statements, resolved against the configured schema.

    Returned as a dict so the caller can hand it straight to the audit record, which is
    what makes the trail reproducible.
    """
    _S = schema  # noqa: N806 — the source's alias, kept so the SQL below is untouched

    # Q1 — Header (batch → project, eLN, times). Single row per stage; latest wins.
    header = f"""
SELECT vb.batch_id                                         AS batch_id_stage,
       vb.batch_name                                        AS batch_name,
       vb.project_name                                      AS project_code,
       vb.batch_type                                        AS batch_type,
       vb.experimenturl_actual_value                        AS eln_url,
       vb.manufacturingstart_actual_value                   AS mfg_start,
       vb.manufacturingstop_actual_value                    AS mfg_stop,
       vb.manufactured_by                                   AS manufactured_by
FROM   {_S}.view_batch_properties vb
WHERE  vb.batch_id LIKE :batch_root || '%'
ORDER  BY vb.batch_id
"""

    # Q2 — Table 1 General Info: batch size + unit, dose form/strength, density, pH, etc.
    # All values from VIEW_BATCH_PROPERTIES dedicated columns (survives when BATCH_PROPERTIES
    # display-name lookup fails).
    general_info = f"""
SELECT vb.batch_id                                         AS batch_id_stage,
       vb.batchsize                                        AS batch_size,
       vb.batchsizeunit                                    AS batch_size_unit,
       vb.doseform_actual_value                            AS dose_form,
       vb.dosestrength_actual_value                        AS dose_strength,
       vb.dosestrength_unit                                AS dose_strength_unit,
       vb.density_actual_value                             AS density,
       vb.density_unit                                     AS density_unit,
       vb.ph_actual_value                                  AS ph,
       vb.packagingsystem_actual_value                     AS packaging_system,
       vb.unitoperation_actual_value                       AS unit_operation_uri,
       vb.amount_actual_value                              AS amount,
       vb.amount_unit                                      AS amount_unit
FROM   {_S}.view_batch_properties vb
WHERE  vb.batch_id LIKE :batch_root || '%'
ORDER  BY vb.batch_id
"""

    # Q3 — Table 2 Composition (bulk-solution ingredients + amount per unit).
    # NB: `view_formulation_components` has NO quality_standard column; that field
    # is currently MANUAL (see MFGR_Automation_Plan.md).
    composition = f"""
SELECT vfc.material_display_name                           AS component_name,
       vfc.function                                        AS component_function,
       vfc.planned_amount                                  AS amount,
       vfc.planned_amount_unit                             AS amount_unit
FROM   {_S}.view_formulation_batches vfb
JOIN   {_S}.view_formulation_components vfc
       ON vfc.formulation_id = vfb.formulation_id
WHERE  vfb.batch_id LIKE :batch_root || '%'
  AND  vfb.formulation_id IS NOT NULL
ORDER  BY vfc.function, vfc.material_display_name
"""

    # Q4 — Packaging + DS component refs from batch_components.
    # The DS "lot / MMID" is embedded as trailing digits of `component_ref`
    # (`urn:batch:idbsnongxp-dev-...NNN` / `urn:batch:ext-br-prod-...NNN`).
    # `component_ref` is an opaque pointer (`urn:batch:<id>`); the human-readable
    # material/lot name lives in LOTS.displayname keyed by the id WITHOUT the
    # `urn:batch:` prefix — so we strip it in the join (confirmed against live data,
    # e.g. L-0001113 → "Vial, Schott, 6R, NBB, clear glass").
    batch_components = f"""
SELECT bc.batch_id                                         AS batch_id_stage,
       bc.display_name                                     AS display_name,
       bc.component_ref                                    AS component_ref,
       l.displayname                                       AS resolved_name
FROM   {_S}.batch_components bc
LEFT   JOIN {_S}.lots l
       ON l.id = REGEXP_REPLACE(bc.component_ref, '^urn:batch:', '')
WHERE  bc.batch_id LIKE :batch_root || '%'
  AND  (bc.component_ref LIKE 'urn:batch:%'
        OR LOWER(bc.display_name) LIKE '%vial%'
        OR LOWER(bc.display_name) LIKE '%syringe%'
        OR LOWER(bc.display_name) LIKE '%stopper%'
        OR LOWER(bc.display_name) LIKE '%crimp%'
        OR LOWER(bc.display_name) LIKE '%cap%')
ORDER  BY bc.batch_id
"""

    # Q6 — Unit operations (§5 Process Flow) from BATCH_PROCESS_STEPS.STAGE.
    # The DSDT unit-operation URIs on view_batch_properties are NOT translatable in the
    # DWH, but STAGE carries the human-readable label directly (per the URI-field
    # investigation). STAGE is stored as "Label - variant" (e.g. "Thawing - Thawing",
    # "Sterilization by Filtration - Sterile Filtration") — take the part before " - ".
    # stage_id (batch_id minus the trailing "-NN" substep) matches
    # view_batch_properties.batch_id, so shaping can pair each stage's label with its amount.
    process_steps = f"""
SELECT stage_id, MIN(clean_stage) AS unit_operation
FROM (
  SELECT REGEXP_REPLACE(batch_id, '-[0-9]+$', '')                 AS stage_id,
         TRIM(SUBSTR(stage, 1, INSTR(stage || ' - ', ' - ') - 1)) AS clean_stage,
         process_step_timestamp                                    AS ts
  FROM   {_S}.batch_process_steps
  WHERE  batch_id LIKE :batch_root || '%'
)
GROUP  BY stage_id
ORDER  BY MIN(ts)
"""

    # Q7 — Material properties, keyed by the batch's formulation id (Alex-confirmed
    # CMCDW source). Populated even when view_batch_properties columns are NULL:
    #   fillVolume    → Table 1 "Fill volume (incl. overfill)"  (e.g. 2.37)
    #   nominalVolume → Table 1 "Dosage volume (Liquid)" + title (e.g. 2.00)
    #   unitStrength  → Table 1 "Dosage strength [mg]" + title  (e.g. 360)
    #   pH            → Table 1 "pH"                             (e.g. 6.0)
    #   materialForm  → dosage-form hint (vial / solution_mg_ml)
    # Values are dimensionless here (units assumed mL / mg).
    material_props = f"""
SELECT mp.parameter_ref, mp.actual_value, mp.actual_value_unit
FROM   {_S}.material_properties mp
WHERE  mp.material_id IN (
         SELECT DISTINCT formulation_id
         FROM   {_S}.view_formulation_batches
         WHERE  batch_id LIKE :batch_root || '%'
           AND  formulation_id IS NOT NULL)
  AND  mp.parameter_ref IN ('urn:parameter:fillVolume',
                            'urn:parameter:nominalVolume',
                            'urn:parameter:unitStrength',
                            'urn:parameter:doseStrength',
                            'urn:parameter:pH',
                            'urn:parameter:materialForm')
"""

    # Q8 — Quality standard per component (Table 2), confirmed by Alex as
    # BATCH_COMPONENT_PARAMETERS.qualityStandardRef. Resolve the component's readable
    # name via LOTS so it can be matched back to the composition rows. Empty on some
    # pilot batches (value not yet populated) — returns 0 rows then.
    quality_standard = f"""
SELECT REGEXP_REPLACE(bc.component_ref, '^urn:batch:', '') AS ref_id,
       l.displayname                                       AS component_name,
       bcp.actual_value                                    AS quality_standard
FROM   {_S}.batch_component_parameters bcp
LEFT   JOIN {_S}.batch_components bc ON bc.id = bcp.batch_comp_id
LEFT   JOIN {_S}.lots l ON l.id = REGEXP_REPLACE(bc.component_ref, '^urn:batch:', '')
WHERE  bcp.batch_id LIKE :batch_root || '%'
  AND  bcp.parameter_ref = 'urn:parameter:qualityStandardRef'
  AND  bcp.actual_value IS NOT NULL
"""

    # Q5 — Validation counts (cheap smoke test).
    validation = f"""
SELECT :batch_root                                         AS batch_root,
       (SELECT COUNT(*) FROM {_S}.view_batch_properties
         WHERE batch_id LIKE :batch_root || '%')            AS n_stages,
       (SELECT COUNT(*) FROM {_S}.batch_properties
         WHERE batch_id LIKE :batch_root || '%')            AS n_properties,
       (SELECT COUNT(*) FROM {_S}.batch_components
         WHERE batch_id LIKE :batch_root || '%')            AS n_components,
       (SELECT COUNT(*) FROM {_S}.batch_process_steps
         WHERE batch_id LIKE :batch_root || '%')            AS n_process_steps,
       (SELECT COUNT(*) FROM {_S}.view_formulation_components vfc
          JOIN {_S}.view_formulation_batches vfb
            ON vfb.formulation_id = vfc.formulation_id
         WHERE vfb.batch_id LIKE :batch_root || '%')        AS n_formulation_rows
FROM dual
"""

    return {
        "header": header,
        "general_info": general_info,
        "composition": composition,
        "batch_components": batch_components,
        "process_steps": process_steps,
        "material_props": material_props,
        "quality_standard": quality_standard,
        "validation": validation,
    }


# The queries the report consumes. `validation`'s six COUNT(*) sub-selects are the slowest
# part of the pack and `build_mfgr_report` does not read them, so the report path skips it.
REPORT_QUERIES = (
    "header",
    "general_info",
    "composition",
    "batch_components",
    "process_steps",
    "material_props",
    "quality_standard",
)


def build_batch_id_search_query(schema: str) -> str:
    """Distinct batch-id suggestions for the UI picker.

    Strips the namespace ('nest-br-prod-') and stage suffix ('-02') so the caller gets the
    short id (BAX000584 / BA259821) that `normalize_batch_id` expects. Deliberately NOT part
    of `build_queries` — a convenience lookup, not part of the audited pack.
    """
    return f"""
SELECT DISTINCT REGEXP_SUBSTR(vb.batch_id, 'BA[A-Z]?[0-9]+') AS short_id
FROM   {schema}.view_batch_properties vb
WHERE  REGEXP_LIKE(vb.batch_id, 'BA[A-Z]?[0-9]+', 'i')
  AND  UPPER(vb.batch_id) LIKE '%' || UPPER(:prefix) || '%'
ORDER  BY 1
FETCH FIRST :max_rows ROWS ONLY
"""


def _rows(cursor) -> list[dict[str, Any]]:
    cols = [c[0].lower() for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _run(conn, sql: str, batch_root: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, batch_root=batch_root)
        return _rows(cur)


def resolve_query_root(conn, short_id: str, schema: str) -> str | None:
    """Return the actual DWH batch-id root for a short id, across ANY registration
    namespace — NEST prod/dev and **externally-manufactured** (nest-br-ext-dev-,
    nest-br-ext-prod-), plus pega/idbs/sap. `normalize_batch_id` assumes the
    'nest-br-prod-' prefix, which misses external batches (e.g. BAX001209 is really
    'nest-br-ext-dev-BAX001209-01'); resolving against the DWH fixes that.

    Returns e.g. 'nest-br-ext-dev-BAX001209' (namespace + id, no stage suffix), or None if
    the id isn't found (caller falls back to the assumed root).
    """
    if not short_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT batch_id FROM {schema}.view_batch_properties "
            "WHERE UPPER(batch_id) LIKE '%' || UPPER(:s) || '%' "
            "FETCH FIRST 1 ROWS ONLY",
            s=short_id,
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    full = str(row[0])
    i = full.upper().find(short_id.upper())
    return full[: i + len(short_id)] if i >= 0 else None


def fetch_all(conn, batch_root: str, schema: str) -> dict[str, list[dict[str, Any]]]:
    """Run every query with the same batch root (live warehouse)."""
    return {name: _run(conn, sql, batch_root) for name, sql in build_queries(schema).items()}


def fetch_report(conn, batch_root: str, schema: str) -> dict[str, list[dict[str, Any]]]:
    """Run only the queries the report consumes (excludes `validation`)."""
    queries = build_queries(schema)
    return {name: _run(conn, queries[name], batch_root) for name in REPORT_QUERIES}


def fetch_validation(conn, batch_root: str, schema: str) -> list[dict[str, Any]]:
    """Run only Q5 (counts) — a cheap live-connection smoke test."""
    return _run(conn, build_queries(schema)["validation"], batch_root)


def fetch_fixture(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load captured query output from a JSON fixture instead of connecting."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def search_batch_ids(conn, prefix: str, schema: str, max_rows: int = 25) -> list[str]:
    """Return up to `max_rows` distinct short batch ids matching `prefix`.

    Used by the batch-id picker UI. The prefix is matched case-insensitively as a substring
    of the full DWH batch id (which may include 'nest-br-prod-' and a stage suffix); the
    returned list contains only the short forms (e.g. 'BAX000584', 'BA259821').
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return []
    with conn.cursor() as cur:
        cur.execute(build_batch_id_search_query(schema), prefix=prefix, max_rows=max_rows)
        return [row[0] for row in cur.fetchall() if row[0]]


def list_batch_ids(conn, schema: str, limit: int = 200) -> list[str]:
    """Return up to `limit` distinct short batch ids from the warehouse, newest first.

    Used to populate the 'select from existing' dropdown in the picker UI. Ordering
    descending by short_id keeps the highest sequence numbers on top (which is a good proxy
    for most-recent for both NEST and manual batches).
    """
    sql = (
        f"SELECT DISTINCT REGEXP_SUBSTR(vb.batch_id, 'BA[A-Z]?[0-9]+') AS short_id "
        f"FROM {schema}.view_batch_properties vb "
        f"WHERE REGEXP_LIKE(vb.batch_id, 'BA[A-Z]?[0-9]+', 'i') "
        f"ORDER BY 1 DESC "
        f"FETCH FIRST :max_rows ROWS ONLY"
    )
    with conn.cursor() as cur:
        cur.execute(sql, max_rows=limit)
        return [row[0] for row in cur.fetchall() if row[0]]


def batch_id_exists(conn, short_id: str, schema: str) -> bool:
    """True if the warehouse has at least one row for `short_id` (namespace-agnostic)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {schema}.view_batch_properties "
            "WHERE UPPER(batch_id) LIKE '%' || UPPER(:short_id) || '%' "
            "FETCH FIRST 1 ROWS ONLY",
            short_id=short_id,
        )
        return cur.fetchone() is not None


def has_data(raw: dict[str, Any]) -> bool:
    """Did the warehouse return anything for this batch?

    **This has no counterpart in the source and is the one deliberate behaviour change in
    the MFGR port.** The app being replaced has no such gate: `POST /mfgr/generate` builds
    unconditionally, so a batch id that exists nowhere in the warehouse yields a fully blank
    MFGR — every table empty, the title line placeholders — plus an audit record asserting
    it was generated from that batch. Its guard against that is `GET /mfgr/exists`, which
    the UI is expected to call first.

    For any batch that genuinely exists this returns True (`view_batch_properties` feeds
    both `header` and `general_info`), so no real report changes. It only diverges where the
    source would emit a blank pharmaceutical manufacturing record, and there the run
    completes with an explanation instead. Declining to produce a blank GxP document is the
    safer failure, and the run still succeeds rather than inviting a pointless retry.

    ATR's equivalent gate is `_has_data` in its own router, so this also makes the two report
    types behave alike at the stage level.
    """
    return any(raw.get(name) for name in REPORT_QUERIES)
