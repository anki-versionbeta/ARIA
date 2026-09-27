# Ported verbatim from src/mfgr_report.py.
#
# Pure shaping over row dicts - no database, filesystem or environment access, so the
# only change is where the constants come from: this silo splits config.py into
# config.py (ATR) and mfgr_config.py (MFGR).
"""MFGR shaping — turn the five Query-Pack result sets into an `MfgrReport`.

Pure functions over the raw query-row dicts. Every rule is unit-testable against a JSON
fixture (no oracledb dependency here).

Encoded rules (MFGR_Automation_Plan.md + Data Mapping analysis):
  * VIEW_BATCH_PROPERTIES has 1 row per unit-operation stage — pick the header row (max
    manufactured window) for the top-level fields; keep per-stage rows for §5 yield.
  * Decode ontology URIs (unit + unit-operation) via UNIT_MAP / UNIT_OP_MAP; fall back to
    the raw URI when unknown and surface as a review flag.
  * Composition rows come from VIEW_FORMULATION_COMPONENTS; quality standard is currently
    MANUAL (no column in the view — see plan).
  * §5 yield per stage = amount_actual_value(stage_n) / batchsize(stage_1) — flagged as
    "review" while DSDT_0000024 / operation URIs remain undecoded.
  * DS lot / MMID is extracted from BATCH_COMPONENTS.component_ref trailing digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .mfgr_config import UNIT_MAP, UNIT_OP_MAP, normalize_batch_id, parse_batch_name

NA = "N/A"

_DS_REF_RE = re.compile(r"urn:batch:[^:]+[-:](\d{4,})\b", re.IGNORECASE)
# Accept both NEST-registered (BAX######) and manually-registered (BA######) tails.
_BAX_TAIL_RE = re.compile(r"\bBA[A-Z]?\d{4,}\b", re.IGNORECASE)


def decode_unit(raw: str | None) -> tuple[str, bool]:
    """('http://qudt.org/vocab/unit/MilliL', True) — second element is `confident`."""
    if not raw:
        return "", True
    key = str(raw).strip()
    if key in UNIT_MAP:
        return UNIT_MAP[key], True
    return key, False


def decode_unit_op(raw: str | None) -> tuple[str, bool]:
    if not raw:
        return "", True
    key = str(raw).strip()
    if key in UNIT_OP_MAP:
        return UNIT_OP_MAP[key], True
    return key, False


def _extract_lot(component_ref: str | None) -> str:
    """'urn:batch:idbsnongxp-dev-...1429707' → '1429707'; empty if not matched."""
    if not component_ref:
        return ""
    m = _DS_REF_RE.search(str(component_ref))
    return m.group(1) if m else ""


def _extract_bax(component_ref: str | None) -> str:
    if not component_ref:
        return ""
    m = _BAX_TAIL_RE.search(str(component_ref))
    return m.group(0).upper() if m else ""


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return s


def _fmt_amount(value: Any, unit_raw: str | None) -> tuple[str, bool]:
    """'500' + 'http://qudt.org/vocab/unit/MilliL' → ('500 mL', True)."""
    val = _fmt(value)
    if not val:
        return "", True
    unit, confident = decode_unit(unit_raw)
    return (f"{val} {unit}".strip(), confident)


# --- shaped data classes ----------------------------------------------------
@dataclass
class HeaderInfo:
    batch_id_display: str
    project_code: str
    batch_type: str
    eln_url: str
    mfg_start: str
    mfg_stop: str
    manufactured_by: str
    # parsed from NEST batch_name (see config.parse_batch_name)
    batch_name: str = ""
    dose_mg: str = ""
    dose_form_code: str = ""      # e.g. "S.INJ"
    dose_form_label: str = ""     # e.g. "Solution for Injection"
    withdraw_volume_ml: str = ""
    container: str = ""

    @property
    def title_line(self) -> str:
        """'ABBV-423 360 mg S.INJ 2 mL Vial' — the recurring page-header line."""
        parts = []
        if self.dose_mg: parts.append(f"{self.dose_mg} mg")
        if self.dose_form_code: parts.append(self.dose_form_code)
        if self.withdraw_volume_ml: parts.append(f"{self.withdraw_volume_ml} mL")
        if self.container: parts.append(f"in {self.container}")
        return " ".join(parts)

    @property
    def eln_experiment_id(self) -> str:
        """Unique eLN experiment ID parsed from the eLN URL (the ``entityId``), e.g.
        'b988a640214011f18c8d00000a4818a8'. §10 References must show this id, not just
        the URL (per business feedback). Empty when no id is present."""
        m = re.search(r"entityId=([^&\s]+)", self.eln_url or "")
        return m.group(1) if m else ""


@dataclass
class GeneralInfo:
    batch_size: str
    dose_form: str
    dose_strength: str
    density: str
    ph: str
    packaging_system: str
    concentration: str = ""      # active-ingredient bulk-solution conc, "180 mg/mL"
    fill_volume: str = ""        # incl. overfill, "2.37" (mL) — from MATERIAL_PROPERTIES


@dataclass
class CompositionRow:
    component_name: str
    component_function: str
    quality_standard: str        # from BATCH_COMPONENT_PARAMETERS when populated
    amount_per_unit: str         # bulk-solution concentration, "1.0 mg/mL"
    amount_per_unit_mg: str = "" # amount per unit = conc x nominal volume, "360 mg"
    is_ingredient: bool = True   # False for packaging / bulk-solution intermediate rows


@dataclass
class Stage:
    batch_id_stage: str
    stage_index: int
    unit_operation: str          # decoded label or raw URI
    amount: str                  # decoded amount + unit
    confident: bool              # false when either URI failed to decode


@dataclass
class DsComponent:
    lot: str                     # trailing digits of component_ref
    display_name: str
    component_ref: str
    bax_tail: str


@dataclass
class ReviewFlag:
    severity: str                # "warn" | "info"
    area: str                    # "general_info" | "stages" | "composition" | "components"
    message: str


@dataclass
class MfgrReport:
    batch_id_short: str
    batch_id_display: str
    batch_id_query: str
    header: HeaderInfo
    general_info: GeneralInfo
    composition: list[CompositionRow] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    ds_components: list[DsComponent] = field(default_factory=list)
    packaging: list[DsComponent] = field(default_factory=list)
    yield_percent: str = ""
    flags: list[ReviewFlag] = field(default_factory=list)
    registration: str = "nest"    # "nest" (BAX######) or "manual" (BA#######)

    @property
    def ds_composition_narrative(self) -> str:
        """Auto-derived DS composition sentence used in §3 Topic.

        Real MFGRs (L-RDLU-000658, L-RDLU-001296) render this as a comma-separated
        `<amount+unit> <component>` list built from the composition table, with the
        Water-for-Injection row omitted. The RepBatch mapping doc flagged this row
        as MANUAL — it is derivable and moves to AUTO here.
        """
        parts: list[str] = []
        for row in self.composition:
            name = (row.component_name or "").strip()
            if not name or not row.is_ingredient:
                continue  # skip packaging + bulk-solution intermediate rows
            if name.lower().startswith("water"):
                continue  # Water for Injection is stated separately as "Ad x.xx mL"
            if row.amount_per_unit:
                parts.append(f"{row.amount_per_unit} {name}")
            else:
                parts.append(name)
        return ", ".join(parts)


# --- builders ---------------------------------------------------------------
def _pick_header_row(rows: list[dict]) -> dict:
    """Choose one representative row and back-fill its NULL columns from siblings.

    Different per-stage rows in ``view_batch_properties`` populate different
    columns. The mfg window (start/stop, manufactured_by) lives on the fill
    stage, while formulation attributes (batch_name, packaging_system, density,
    dose_form/strength, ph) live on the initial formulation stage. If we
    return only one raw row, half the values are NULL.

    Strategy: pick the mfg-window row as the base, then overlay any key that's
    empty on the base with the first non-empty value found across all rows.
    Falls back to the first row when no mfg dates are present.
    """
    if not rows:
        return {}
    base = None
    for r in rows:
        if _fmt(r.get("mfg_start")) or _fmt(r.get("mfg_stop")):
            base = r
            break
    if base is None:
        base = rows[0]
    merged = dict(base)
    for r in rows:
        if r is base:
            continue
        for k, v in r.items():
            if not _fmt(merged.get(k)) and _fmt(v):
                merged[k] = v
    return merged


def _build_header(ids: dict[str, str], header_rows: list[dict]) -> HeaderInfo:
    h = _pick_header_row(header_rows)  # merged row — see _pick_header_row docstring
    batch_name = _fmt(h.get("batch_name") or h.get("batch_id_stage") or "")
    parsed = parse_batch_name(batch_name or ids["display"])
    return HeaderInfo(
        batch_id_display=ids["display"],
        project_code=_fmt(h.get("project_code")),
        batch_type=_fmt(h.get("batch_type")),
        eln_url=_fmt(h.get("eln_url")),
        mfg_start=_fmt(h.get("mfg_start")),
        mfg_stop=_fmt(h.get("mfg_stop")),
        manufactured_by=_fmt(h.get("manufactured_by")),
        batch_name=batch_name,
        dose_mg=parsed["dose_mg"],
        dose_form_code=parsed["dose_form_code"],
        dose_form_label=parsed["dose_form_label"],
        withdraw_volume_ml=parsed["withdraw_volume_ml"],
        container=parsed["container"],
    )


def _build_general_info(gi_rows: list[dict], flags: list[ReviewFlag]) -> GeneralInfo:
    r = _pick_header_row(gi_rows)
    batch_size, confident_sz = _fmt_amount(r.get("batch_size"), r.get("batch_size_unit"))
    if r.get("batch_size") and not confident_sz:
        flags.append(ReviewFlag("warn", "general_info",
            f"Batch-size unit URI is not decoded: {r.get('batch_size_unit')!r} — "
            "add to UNIT_MAP when ontology confirmed."))
    dose_strength, confident_ds = _fmt_amount(r.get("dose_strength"), r.get("dose_strength_unit"))
    if r.get("dose_strength") and not confident_ds:
        flags.append(ReviewFlag("warn", "general_info",
            f"Dose-strength unit URI is not decoded: {r.get('dose_strength_unit')!r}."))
    density, confident_dn = _fmt_amount(r.get("density"), r.get("density_unit"))
    if r.get("density") and not confident_dn:
        flags.append(ReviewFlag("warn", "general_info",
            f"Density unit URI is not decoded: {r.get('density_unit')!r}."))
    return GeneralInfo(
        batch_size=batch_size,
        dose_form=_fmt(r.get("dose_form")),
        dose_strength=dose_strength,
        density=density,
        ph=_fmt(r.get("ph")),
        packaging_system=_fmt(r.get("packaging_system")),
    )


def _build_stages(gi_rows: list[dict], flags: list[ReviewFlag],
                  stage_ops: dict[str, str] | None = None) -> list[Stage]:
    """Build §5 process-flow stages.

    Unit operation is taken from BATCH_PROCESS_STEPS.STAGE (``stage_ops``, keyed by
    stage id) when available — readable and needs no URI translation. Falls back to
    decoding the DSDT ``unit_operation_uri`` (and flags it) only when the process
    step is missing for that stage.
    """
    stage_ops = stage_ops or {}
    stages: list[Stage] = []
    for idx, r in enumerate(gi_rows, start=1):
        stage_id = _fmt(r.get("batch_id_stage"))
        bps_label = stage_ops.get(stage_id)
        if bps_label:
            op_label, confident_op = bps_label, True
        else:
            op_label, confident_op = decode_unit_op(r.get("unit_operation_uri"))
        amount, confident_amt = _fmt_amount(r.get("amount"), r.get("amount_unit"))
        confident = confident_op and confident_amt
        stages.append(Stage(
            batch_id_stage=stage_id,
            stage_index=idx,
            unit_operation=op_label,
            amount=amount,
            confident=confident,
        ))
        if not bps_label and r.get("unit_operation_uri") and not confident_op:
            flags.append(ReviewFlag("warn", "stages",
                f"Stage {idx}: unit-operation URI not decoded ({r.get('unit_operation_uri')!r}) "
                "and no BATCH_PROCESS_STEPS.STAGE label found."))
        if r.get("amount") and not confident_amt:
            flags.append(ReviewFlag("warn", "stages",
                f"Stage {idx}: amount unit URI not decoded ({r.get('amount_unit')!r})."))
    return stages


def _compute_yield(gi_rows: list[dict], stages: list[Stage]) -> str:
    """Yield% = final-stage amount / first-stage amount (or batch_size), when numeric."""
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    if not gi_rows:
        return ""
    first_amt = _num(gi_rows[0].get("amount")) or _num(gi_rows[0].get("batch_size"))
    last_amt = _num(gi_rows[-1].get("amount"))
    if not first_amt or not last_amt or first_amt <= 0:
        return ""
    return f"{(last_amt / first_amt) * 100:.1f}%"


def _build_composition(comp_rows: list[dict],
                       quality_map: dict[str, str] | None = None) -> list[CompositionRow]:
    """Composition rows, deduplicated across per-stage duplicates.

    ``view_formulation_batches`` returns 1 row per (component × batch stage) — the
    ``batch_id LIKE :root||'%'`` filter matches every stage (-01, -02, -03). Same
    formulation → each ingredient repeats N-stages times. Dedupe on the composite
    key (name, function, amount, unit) and preserve first-seen order.

    ``quality_map`` (component-name → quality standard, from BATCH_COMPONENT_PARAMETERS
    qualityStandardRef) fills the Quality Standard column when the DWH has a value;
    it is empty on batches where the field isn't populated yet.
    """
    quality_map = quality_map or {}
    out: list[CompositionRow] = []
    seen: set[tuple[str, str, str, str]] = set()
    for r in comp_rows:
        name = _fmt(r.get("component_name"))
        func = _fmt(r.get("component_function"))
        amt_raw = _fmt(r.get("amount"))
        unit_raw = _fmt(r.get("amount_unit"))
        key = (name.lower(), func.lower(), amt_raw, unit_raw)
        if key in seen:
            continue
        seen.add(key)
        amt, _confident = _fmt_amount(r.get("amount"), r.get("amount_unit"))
        # A bulk-solution ingredient (active/excipient) — not packaging, not an
        # intermediate "bulk solution" row. Only ingredients belong in Table 2.
        flow = func.lower()
        is_ingredient = ("bulk solution" not in flow
                         and not any(k in flow for k in _PACKAGING_HINTS))
        out.append(CompositionRow(
            component_name=name,
            component_function=func,
            quality_standard=quality_map.get(name.lower(), ""),  # AUTO when populated
            amount_per_unit=amt,
            is_ingredient=is_ingredient,
        ))
    return out


def _amount_per_unit_mg(conc_str: str, nominal_volume: str) -> str:
    """'180 mg/mL' + '2.00' -> '360 mg'. Empty when either part is missing/non-numeric."""
    if not conc_str or not nominal_volume:
        return ""
    m = re.match(r"\s*([\d.]+)", conc_str)
    try:
        vol = float(nominal_volume)
    except (TypeError, ValueError):
        return ""
    if not m:
        return ""
    val = float(m.group(1)) * vol
    return f"{val:g} mg"


_PACKAGING_HINTS = ("vial", "syringe", "stopper", "crimp", "cap")
# component_ref namespaces that identify the drug substance (vs excipient/consumable lots)
_DS_REF_HINTS = ("ext-br-prod", "idbsnongxp")


def _build_components(bc_rows: list[dict]) -> tuple[list[DsComponent], list[DsComponent]]:
    """Split batch_components into DS lots and packaging.

    The readable material name comes from ``display_name`` when present, else the
    LOTS-resolved ``resolved_name`` (component_ref → LOTS.displayname). Classification:
      * packaging — name matches a container/closure hint (vial/stopper/crimp/cap/syringe)
      * DS        — component_ref is a drug-substance namespace (ext-br-prod / idbsnongxp)
                    or the name says so; deduped by lot (DS repeats once per stage)
      * otherwise — excipient/consumable lots already covered in composition → skipped
    """
    ds: list[DsComponent] = []
    pkg: list[DsComponent] = []
    seen_ds: set[str] = set()
    seen_pkg: set[str] = set()
    for r in bc_rows:
        ref = _fmt(r.get("component_ref"))
        name = _fmt(r.get("display_name")) or _fmt(r.get("resolved_name"))
        low = name.lower()
        # nest-br-prod refs are process intermediates (prior-stage bulk), not materials.
        is_intermediate = "nest-br-prod" in ref.lower()
        obj = DsComponent(
            lot=_extract_lot(ref),
            display_name=name,
            component_ref=ref,
            bax_tail=_extract_bax(ref),
        )
        if not is_intermediate and any(k in low for k in _PACKAGING_HINTS):
            key = f"{low}|{ref}"
            if key not in seen_pkg:            # dedupe per-stage repeats
                seen_pkg.add(key)
                pkg.append(obj)
        elif (any(h in ref.lower() for h in _DS_REF_HINTS)
              or "drug substance" in low or "ds lot" in low):
            dedupe_key = obj.lot or ref
            if dedupe_key not in seen_ds:
                seen_ds.add(dedupe_key)
                ds.append(obj)
    return ds, pkg


_CONTAINER_FROM_PKG_RE = re.compile(r"\b(PFS|Vial|Cartridge|Syringe)\b", re.IGNORECASE)


def _backfill_header_from_general_info(header: HeaderInfo, gi: GeneralInfo, gi_rows: list[dict]) -> None:
    """Fill missing title-line pieces from general_info when batch_name didn't parse.

    NEST batch_name is the intended source ("ABBV-XXX 100 mg S.INJ 5 mL Vial") but
    live DWH data often has batch_name null or in a shape parse_batch_name can't
    read. Derive what we can from Table-1 fields so the recurring page header line
    stops showing <Dose>/<S.INJ>/<Volume>/<PFS or Vial> placeholders.
    """
    # dose_mg — dose_strength numeric part (e.g. "1 mg/mL" → "1")
    if not header.dose_mg and gi.dose_strength:
        m = re.match(r"\s*(\d+(?:\.\d+)?)", gi.dose_strength)
        if m:
            header.dose_mg = m.group(1)
    # dose_form_code — from gi.dose_form or per-stage row
    if not header.dose_form_code and gi.dose_form:
        df = gi.dose_form.strip().lower()
        if "sol" in df and "powder" not in df:
            header.dose_form_code = "S.INJ"
            header.dose_form_label = "Solution for Injection"
        elif "powder" in df or "lyo" in df:
            header.dose_form_code = "P.S.INJ"
            header.dose_form_label = "Powder for Solution for Injection"
    # container — from packaging_system string
    if not header.container and gi.packaging_system:
        m = _CONTAINER_FROM_PKG_RE.search(gi.packaging_system)
        if m:
            raw = m.group(1)
            header.container = raw.upper() if raw.upper() == "PFS" else raw.capitalize()
    # withdraw_volume_ml intentionally NOT back-filled from stage amounts: those
    # represent batch/fill sizes (500 mL etc.), not the per-unit withdraw volume.


def _backfill_header_from_composition(header: HeaderInfo, composition: list[CompositionRow],
                                      ds_components: list[DsComponent]) -> None:
    """Second-stage backfill using composition + component data.

    When view_batch_properties has nulls for dose_form/dose_strength, the
    composition table still tells us something: an ACTIVE ingredient in mg/mL
    means the product is a Solution for Injection with that concentration.
    Container hints (Vial / PFS / Syringe) can appear in DS/packaging display
    names.
    """
    # Look at the active ingredient row in composition.
    for row in composition:
        func = (row.component_function or "").lower()
        amt = (row.amount_per_unit or "").strip()
        if "active" not in func:
            continue
        # Concentration format "100 mg/mL" → dose_form=Solution, dose_strength=100 mg/mL
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*mg/mL\b", amt, re.IGNORECASE)
        if m and not header.dose_form_code:
            header.dose_form_code = "S.INJ"
            header.dose_form_label = "Solution for Injection"
        if m and not header.dose_mg:
            # Note: this fills the concentration when per-unit dose is unknown —
            # reviewer should confirm against the batch record.
            header.dose_mg = m.group(1)
        break
    # Container inference from DS/packaging component display names.
    if not header.container:
        joined = " ".join((c.display_name or "") for c in ds_components).lower()
        for hint, canonical in (("pfs", "PFS"), ("syringe", "Syringe"),
                                ("vial", "Vial"), ("cartridge", "Cartridge")):
            if hint in joined:
                header.container = canonical
                break


def _material_props(mp_rows: list[dict]) -> dict[str, tuple[str, str]]:
    """{'fillVolume': (value, unit), 'unitStrength': ..., 'doseStrength': ...}.

    Keyed by the short parameter name (last URI segment); first non-empty value wins.
    """
    out: dict[str, tuple[str, str]] = {}
    for r in mp_rows:
        key = _fmt(r.get("parameter_ref")).rsplit(":", 1)[-1]
        val = _fmt(r.get("actual_value"))
        if key and val and key not in out:
            out[key] = (val, _fmt(r.get("actual_value_unit")))
    return out


def _quality_map(qs_rows: list[dict]) -> dict[str, str]:
    """component-name (lowercased) → quality standard, from qualityStandardRef rows."""
    m: dict[str, str] = {}
    for r in qs_rows:
        name = _fmt(r.get("component_name")).lower()
        val = _fmt(r.get("quality_standard"))
        if name and val:
            m[name] = val
    return m


def build_mfgr_report(batch_id: str, raw: dict[str, list[dict]]) -> MfgrReport:
    ids = normalize_batch_id(batch_id)
    flags: list[ReviewFlag] = []

    header = _build_header(ids, raw.get("header") or [])
    gi_rows = raw.get("general_info") or []
    general_info = _build_general_info(gi_rows, flags)
    _backfill_header_from_general_info(header, general_info, gi_rows)
    # Unit operations from BATCH_PROCESS_STEPS.STAGE (readable; no URI translation).
    stage_ops = {_fmt(r.get("stage_id")): _fmt(r.get("unit_operation"))
                 for r in (raw.get("process_steps") or []) if _fmt(r.get("stage_id"))}
    stages = _build_stages(gi_rows, flags, stage_ops)
    y = _compute_yield(gi_rows, stages)
    composition = _build_composition(raw.get("composition") or [],
                                     _quality_map(raw.get("quality_standard") or []))
    if not composition:
        flags.append(ReviewFlag("info", "composition",
            "No formulation rows found — composition (Table 2) will be blank / manual."))
    ds, pkg = _build_components(raw.get("batch_components") or [])
    if not ds:
        flags.append(ReviewFlag("info", "components",
            "No DS component_refs matched urn:batch:* — DS Lot / MMID will be blank."))
    # Second-pass header backfill — composition table often carries the concentration
    # even when view_batch_properties has null dose_form/dose_strength.
    _backfill_header_from_composition(header, composition, ds + pkg)

    # MATERIAL_PROPERTIES (Alex-confirmed) — populated even when view_batch_properties
    # columns are NULL. Fills dose strength, dose/fill volumes, pH.
    mp = _material_props(raw.get("material_props") or [])
    nominal = mp.get("nominalVolume", ("", ""))[0]
    if nominal:
        header.withdraw_volume_ml = nominal          # nominal dose volume → title + Table 1
    elif mp.get("fillVolume") and not header.withdraw_volume_ml:
        header.withdraw_volume_ml = mp["fillVolume"][0]
    if mp.get("fillVolume"):
        general_info.fill_volume = mp["fillVolume"][0]   # incl. overfill → Table 1
    if mp.get("pH") and not general_info.ph:
        general_info.ph = mp["pH"][0]
    if mp.get("unitStrength"):
        header.dose_mg = mp["unitStrength"][0]       # per-unit dose → title + Table 1

    # Bulk-solution concentration (Table 1) = the active ingredient's mg/mL.
    for row in composition:
        if "active" in (row.component_function or "").lower() and row.amount_per_unit:
            general_info.concentration = row.amount_per_unit
            break

    # Amount per unit (mg) per ingredient = concentration x nominal dose volume.
    for row in composition:
        if row.is_ingredient and row.amount_per_unit:
            row.amount_per_unit_mg = _amount_per_unit_mg(row.amount_per_unit, nominal)

    registration = ids.get("registration", "nest")
    if registration == "manual":
        flags.append(ReviewFlag("info", "general_info",
            f"Batch {ids['display']} is manually-registered (BA prefix, non-NEST). "
            "Query uses bare id — confirm with client if DWH namespace differs."))

    return MfgrReport(
        batch_id_short=ids["short"],
        batch_id_display=ids["display"],
        batch_id_query=ids["query"],
        header=header,
        general_info=general_info,
        composition=composition,
        stages=stages,
        ds_components=ds,
        packaging=pkg,
        yield_percent=y,
        flags=flags,
        registration=registration,
    )
