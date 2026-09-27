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

from .mfgr_config import (
    COUNT_OP_LABELS,
    HEURISTIC_STAGE_NAMES,
    UNIT_MAP,
    UNIT_OP_MAP,
    normalize_batch_id,
    parse_batch_name,
)

NA = "N/A"

_DS_REF_RE = re.compile(r"urn:batch:[^:]+[-:](\d{4,})\b", re.IGNORECASE)
# Accept both NEST-registered (BAX######) and manually-registered (BA######) tails.
_BAX_TAIL_RE = re.compile(r"\bBA[A-Z]?\d{4,}\b", re.IGNORECASE)
# Pull YYYY-MM-DD from any leading date-like prefix. DWH values come back as
# "2025-09-05T08:33:12", "2025-09-05 08:33:12", or occasionally a datetime object
# stringified with a space — .split("T")[0] silently misses the space form.
# Prod BAX001675 returned US locale: "6/29/2026 8:51:00 AM" — hence the second regex.
_DATE_ONLY_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_US_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})")


def _date_only(s: Any) -> str:
    """Return the leading YYYY-MM-DD from a timestamp string. Empty when absent.

    Accepts ISO (`2025-09-05T08:33:12`, `2025-09-05 08:33:12`) and US locale
    (`6/29/2026 8:51:00 AM`). Anything else — including plain datetime objects
    stringified oddly — returns empty so the abstract shows a manual-entry blank
    rather than leaking a raw timestamp.
    """
    if not s:
        return ""
    text = str(s)
    m = _DATE_ONLY_RE.match(text)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    m = _DATE_US_RE.match(text)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return f"{y}-{mo:02d}-{d:02d}"
    return ""


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


def _fmt_amount(value: Any, unit_raw: str | None,
                *, op_label: str | None = None) -> tuple[str, bool]:
    """'500' + 'http://qudt.org/vocab/unit/MilliL' → ('500 mL', True).

    When ``op_label`` is one of the count operations (Filling, Visual Inspection,
    Crimping) and the URI decodes to a mass unit like "g", override with "units"
    and return not-confident so the mismatch surfaces as a review flag. This is a
    defensive workaround for the BAX001675 case where the DWH tags fill amounts
    with the mass URI.
    """
    val = _fmt(value)
    if not val:
        return "", True
    unit, confident = decode_unit(unit_raw)
    if op_label and op_label.strip().lower() in COUNT_OP_LABELS:
        if unit.lower() in {"g", "mg", "kg", "l", "ml"}:
            return (f"{val} units", False)
    return (f"{val} {unit}".strip(), confident)


_NUM_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")


def _split_amount(s: str) -> tuple[float | None, str]:
    """'1714 units' → (1714.0, 'units'). Empty unit when only a number.

    Returns (None, '') when the string doesn't start with a number.
    """
    if not s:
        return None, ""
    m = _NUM_RE.match(s)
    if not m:
        return None, ""
    try:
        val = float(m.group(1))
    except ValueError:
        return None, ""
    unit = s[m.end():].strip()
    return val, unit


# Unit → family. Only same-family divisions produce a meaningful yield %.
_UNIT_FAMILY = {
    "": "count", "units": "count", "unit": "count", "pfs": "count",
    "pcs": "count", "vials": "count", "vial": "count",
    "l": "volume", "ml": "volume",
    "g": "mass", "mg": "mass", "kg": "mass", "µg": "mass", "ug": "mass",
}


def _unit_family(unit: str) -> str:
    return _UNIT_FAMILY.get((unit or "").lower(), "other")


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

    @property
    def mfg_start_date(self) -> str:
        """Just the YYYY-MM-DD portion of mfg_start. Tim's feedback: time-of-day
        pulled from the first stage isn't necessarily meaningful — date is enough."""
        return _date_only(self.mfg_start)


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
    # Post-visual-inspection unit count (e.g. "1682 units"). Populated when a
    # Visual Inspection stage is identifiable AND its amount decodes to a count.
    # Tim's BAX001675 feedback: a raw PFS count is the meaningful yield figure;
    # the % ratio only makes sense when units line up.
    post_vi_count: str = ""
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
        uri_raw = r.get("unit_operation_uri")
        if bps_label:
            op_label, confident_op = bps_label, True
        else:
            op_label, confident_op = decode_unit_op(uri_raw)
        # Fallback: when neither BPS nor UNIT_OP_MAP resolved a name, use the
        # stage-index heuristic. Still not-confident so a warn flag is emitted.
        used_heuristic = False
        if not bps_label and not confident_op and idx in HEURISTIC_STAGE_NAMES:
            op_label = HEURISTIC_STAGE_NAMES[idx]
            used_heuristic = True
        amount, confident_amt = _fmt_amount(
            r.get("amount"), r.get("amount_unit"), op_label=op_label)
        confident = confident_op and confident_amt
        stages.append(Stage(
            batch_id_stage=stage_id,
            stage_index=idx,
            unit_operation=op_label,
            amount=amount,
            confident=confident,
        ))
        if not bps_label and uri_raw and not confident_op:
            note = " (heuristic label applied)" if used_heuristic else ""
            flags.append(ReviewFlag("warn", "stages",
                f"Stage {idx}: unit-operation URI not decoded ({uri_raw!r}) "
                f"and no BATCH_PROCESS_STEPS.STAGE label found{note}."))
        if r.get("amount") and not confident_amt:
            flags.append(ReviewFlag("warn", "stages",
                f"Stage {idx}: amount unit URI not decoded / mismatched "
                f"({r.get('amount_unit')!r} — displayed unit corrected for "
                f"{op_label!r} stage)."))
    return stages


def _compute_yield(stages: list[Stage], flags: list[ReviewFlag]) -> str:
    """Yield% = last-stage amount / first-stage amount, ONLY when unit families match.

    Historically this divided raw numbers regardless of unit — a PFS count over an
    mL volume produced 171.4% for BAX001675 (Tim's V1 feedback). Now:
      * Both endpoints must parse to a number.
      * Both endpoints must belong to the same unit family (count vs volume vs mass).
      * Otherwise return "" and emit a review flag so the % isn't silently wrong.
    """
    if not stages:
        return ""
    first_val, first_unit = _split_amount(stages[0].amount)
    last_val, last_unit = _split_amount(stages[-1].amount)
    if first_val is None or last_val is None or first_val <= 0:
        return ""
    fam_first = _unit_family(first_unit)
    fam_last = _unit_family(last_unit)
    if fam_first != fam_last:
        flags.append(ReviewFlag("warn", "stages",
            f"Yield% suppressed: first-stage unit {first_unit!r} ({fam_first}) does "
            f"not match last-stage unit {last_unit!r} ({fam_last}). "
            f"Cannot divide count by volume. See post-VI unit count instead."))
        return ""
    return f"{(last_val / first_val) * 100:.1f}%"


def _compute_post_vi_count(stages: list[Stage]) -> str:
    """Return the amount of the Visual Inspection stage (or last stage as fallback).

    Only surfaced when the amount decodes to a count unit — a mass or volume there
    is meaningless as a "unit count".
    """
    if not stages:
        return ""
    vi = None
    for s in stages:
        if "visual inspection" in (s.unit_operation or "").lower():
            vi = s
            break
    target = vi or stages[-1]
    val, unit = _split_amount(target.amount)
    if val is None:
        return ""
    if _unit_family(unit) != "count":
        return ""
    return target.amount


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
# component_ref namespaces that identify the drug substance (vs excipient/consumable lots).
# Extended after Tim's BAX001675 review — some batches use namespaces outside the
# original two, so authors were seeing empty "DS Lot / MMID" for real DS refs.
_DS_REF_HINTS = (
    "ext-br-prod", "idbsnongxp",
    "ext-ds", "ds-batch", "drug-substance",
)
_DS_NAME_HINTS = ("drug substance", "ds lot", "active ingredient",
                  "active substance", "active pharmaceutical ingredient")
# NEST-registered external drug substance batches use a BA prefix (not BAX).
_DS_BA_TAIL_RE = re.compile(r"nest-br-prod-BA\d", re.IGNORECASE)


def _build_components(bc_rows: list[dict],
                      flags: list[ReviewFlag] | None = None
                      ) -> tuple[list[DsComponent], list[DsComponent]]:
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
            continue
        ref_low = ref.lower()
        is_ds_ref = (any(h in ref_low for h in _DS_REF_HINTS)
                     or bool(_DS_BA_TAIL_RE.search(ref)))
        is_ds_name = any(h in low for h in _DS_NAME_HINTS)
        if is_ds_ref or is_ds_name:
            dedupe_key = obj.lot or ref
            if dedupe_key not in seen_ds:
                seen_ds.add(dedupe_key)
                ds.append(obj)
            continue
        # Unrecognized urn:batch: ref that isn't packaging or a known intermediate.
        # Surface it as a candidate DS so authors don't silently miss real DS lots
        # (Tim BAX001675 feedback: the DS ref namespace wasn't in our list).
        if flags is not None and not is_intermediate and ref.lower().startswith("urn:batch:"):
            flags.append(ReviewFlag("warn", "components",
                f"Unrecognized DS-ref namespace {ref!r} — component "
                f"{name!r} may be a drug substance. Confirm and add to _DS_REF_HINTS."))
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
    y = _compute_yield(stages, flags)
    post_vi = _compute_post_vi_count(stages)
    composition = _build_composition(raw.get("composition") or [],
                                     _quality_map(raw.get("quality_standard") or []))
    if not composition:
        flags.append(ReviewFlag("info", "composition",
            "No formulation rows found — composition (Table 2) will be blank / manual."))
    ds, pkg = _build_components(raw.get("batch_components") or [], flags)
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
    # Tim BAX001675 feedback: bulk-solution concentration wasn't recognized, so
    # widen the active-ingredient predicate and fall back to dose_strength when
    # composition doesn't surface it.
    _ACTIVE_TOKENS = REDACTED
    for row in composition:
        func_low = (row.component_function or "").lower()
        is_active = (any(tok in f" {func_low} " for tok in _ACTIVE_TOKENS)
                     or func_low.strip() == "api")
        if is_active and row.amount_per_unit:
            general_info.concentration = row.amount_per_unit
            break
    if not general_info.concentration and general_info.dose_strength:
        # dose_strength on view_batch_properties is already mg/mL for solution
        # products — accept it as the concentration when composition is silent.
        if "mg/ml" in general_info.dose_strength.lower():
            general_info.concentration = general_info.dose_strength
            flags.append(ReviewFlag("info", "general_info",
                "Bulk-solution concentration derived from dose_strength — no "
                "active-ingredient row in composition."))

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
        post_vi_count=post_vi,
        flags=flags,
        registration=registration,
    )
