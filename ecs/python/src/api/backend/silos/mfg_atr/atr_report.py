"""ATR shaping — turn the five Query-Pack result sets into an `AtrReport`.

Ported from `src/atr_report.py`. **No seam changes at all**: this module is pure functions
over the query-row dicts, touching neither the network, the filesystem nor the environment,
so the only difference from the source is where `config` is imported from.

ATR-specific (no generic/master-dataset abstraction). Pure functions over the query-row
dicts, so every rule is unit-testable against a fixture.

Encoded rules (ATR_Query.docx + Data Mapping analysis):
  * disambiguate techniques by parameter / method reference, NOT experiment id
    (pH + Clarity share experiment ids);
  * aggregate ELN ids across the experiments that share a (technique, method);
  * keep legitimate 2-method Compendial rows;
  * §4 is a variable-schema pivot: technique -> parameter rows x sample columns, with
    N/A for unmeasured (sample, parameter) cells; carry analyte (peak) + decoded units;
  * flag null method reference, missing values, and 3rd-party (_A01) techniques.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import ATTACHMENT_MARKER, normalize_request_id

NA = "N/A"

# Raw GRE header name (result_header_name suffix) -> clean display technique.
TECHNIQUE_ALIASES: dict[str, str] = {
    "MFI": "Sub-Visible Particles - MFI",
    "PS20": "Polysorbate 20",
    "SoloVPE": "Protein content",
    "Visible Particles": "Visible Particles",
}
AMBIGUOUS_HEADER = "Compendial"
# "Compendial" is ambiguous — disambiguate by a keyword in the parameter (results) ...
COMPENDIAL_PARAM_HINTS = [
    ("ph", "pH"), ("clarity", "Clarity and Opalescence"),
    ("opalesc", "Clarity and Opalescence"), ("ntu", "Clarity and Opalescence"),
    ("suspension", "Clarity and Opalescence"),
]
# ... or by a method-reference prefix (methods table, which has no parameter).
COMPENDIAL_METHOD_HINTS = [("P-500488", "pH"), ("ARM-20-00231", "Clarity and Opalescence")]

# German -> English: the final ATR must be entirely in English, but some source terms
# (technique names, parameter labels) come back in German. Whole-word, case-insensitive.
# Extend this map with any further German terms seen in live data.
GERMAN_TO_ENGLISH: dict[str, str] = {
    "Sichtbare Partikel": "Visible Particles",
    "Osmolalität": "Osmolality",
    "Viskosität": "Viscosity",
    "Trübung": "Turbidity",
    "Färbung": "Coloration",
    "Aussehen": "Appearance",
    "Reinheit": "Purity",
    "Konzentration": "Concentration",
    "Löslichkeit": "Solubility",
    "Bewertung": "Assessment",
    "Klarheit": "Clarity",
    "Gehalt": "Content",
    "Dichte": "Density",
    "Farbe": "Color",
}
# Letter-based boundaries so compound tokens joined by '_' / digits also translate
# (e.g. 'Dichte_Viskosität' -> 'Density_Viscosity'); a plain \b would treat '_' as a
# word char and miss both halves.
_TRANSLATE_RE = re.compile(
    r"(?<![A-Za-zÀ-ÿ])(" + "|".join(re.escape(k) for k in sorted(GERMAN_TO_ENGLISH, key=len, reverse=True))
    + r")(?![A-Za-zÀ-ÿ])", re.IGNORECASE)
_TRANSLATE_LOWER = {k.lower(): v for k, v in GERMAN_TO_ENGLISH.items()}


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was mis-decoded as Latin-1/CP1252 (so 'ViskositÃ¤t' becomes
    'Viskosität' again). Only attempted when the tell-tale 'Ã'/'Â' sequences are present,
    leaving correctly-encoded text untouched."""
    if "Ã" in text or "Â" in text:
        try:
            return text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def translate_terms(text: str | None) -> str:
    """Repair mojibake, then replace known German analytical terms with English."""
    if not text:
        return text or ""
    text = _fix_mojibake(str(text))
    return _TRANSLATE_RE.sub(lambda m: _TRANSLATE_LOWER[m.group(0).lower()], text)


_VERSION_SPLIT_RE = re.compile(r"(,|\s+[vV]\s*\d|\s*\([vV])")


def strip_method_version(method_ref: str | None) -> str:
    """'P-500488-E-B V8.0' -> 'P-500488-E-B'; 'ARM-19-01273, V 3.0' -> 'ARM-19-01273'."""
    if not method_ref:
        return ""
    m = _VERSION_SPLIT_RE.search(method_ref)
    return (method_ref[: m.start()] if m else method_ref).strip()


def _by_hints(text: str, hints) -> str | None:
    low = (text or "").lower()
    for needle, tech in hints:
        if needle.lower() in low:
            return tech
    return None


def resolve_result_technique(header: str, param: str) -> tuple[str, bool]:
    header = (header or "").strip()
    if header == AMBIGUOUS_HEADER:
        hit = _by_hints(param, COMPENDIAL_PARAM_HINTS)
        return (hit, True) if hit else (translate_terms(header), False)
    return translate_terms(TECHNIQUE_ALIASES.get(header, header)), True


def resolve_method_technique(header: str, method_ref: str) -> tuple[str, bool]:
    header = (header or "").strip()
    if header == AMBIGUOUS_HEADER:
        hit = _by_hints(method_ref, COMPENDIAL_METHOD_HINTS)
        return (hit, True) if hit else (translate_terms(header), False)
    return translate_terms(TECHNIQUE_ALIASES.get(header, header)), True


def _clean_param_label(technique: str, param: str, units: str, analyte: str | None) -> str:
    """Strip a leading technique keyword; append units/analyte when not already implied."""
    label = (param or "").strip()
    for kw in (technique, technique.split()[0] if technique else "", "Clarity", "pH"):
        if kw and label.lower().startswith(kw.lower() + " "):
            label = label[len(kw) + 1 :].strip()
            break
    if analyte and analyte.strip() and analyte.strip().lower() not in label.lower():
        label = f"{label} — {analyte.strip()}" if label else analyte.strip()
    if units and "[" not in label and units.lower() not in label.lower():
        label = f"{label} [{units}]" if label else f"[{units}]"
    return translate_terms(label)


# --- stability-ATR helpers (matrix layout: batch x timepoint x parameter) ---
# EAV metadata rows that are not measurements -> excluded from the matrix.
_METADATA = {"analysisid", "analysiscount", "resultid", "sampleid", "test material number",
             "peak name", "ecd", "replicate", "context", "result name", "analyte name", "uri"}
_CONDITION_ORDER = {"5C": 0, "25C": 1, "40C": 2}

# Some result headers emit an extra "row" per peak carrying the chromatography
# run/injection identifier (e.g. '20260427-i5221-c5223-r5449') in ACTUAL_VALUE_STRING
# instead of a measurement — reported value and numeric actual are both NULL. These are
# provenance metadata, not results; drop them so they don't pollute §4 cells (joined onto
# the real value) or create spurious identifier-only columns (e.g. a second "Main Peak").
_INJECTION_ID_RE = re.compile(r"^\s*\d{8}-i\d")


def _is_identifier_value(value) -> bool:
    """True for injection/sequence-id strings that are provenance metadata, not results."""
    return bool(value) and _INJECTION_ID_RE.match(str(value)) is not None


def _tech_key(t: str) -> str:
    """Group key ignoring non-ASCII so encoding variants collapse (e.g. Viskosität)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(t or "")).lower()


def _pick_display(names) -> str:
    names = list(names)
    clean = [n for n in names if "�" not in n]
    return translate_terms(sorted(clean or names, key=len)[0]) if names else ""


def _round_value(s) -> str:
    """Round decimal numbers to 2 decimal places; leave whole numbers and non-numeric
    text untouched. Values already at <=2 decimals keep their exact form, so '0.20' and
    '5.8' are preserved (never padded or trimmed). Only values with more than 2 decimal
    places are rounded (e.g. '177.456' -> '177.46', '0.21345' -> '0.21'). Handles
    European decimal commas ('3,111' -> '3.11')."""
    if s is None:
        return ""
    txt = str(s).strip()
    if not txt:
        return ""
    norm = txt.replace(",", ".") if re.fullmatch(r"-?\d+,\d+", txt) else txt
    try:
        f = float(norm)
    except ValueError:
        return txt                                  # non-numeric text — untouched
    frac = norm.split(".", 1)[1] if "." in norm else ""
    if "e" not in norm.lower() and len(frac) <= 2:  # whole number or <=2 decimals — keep form
        if norm.startswith("."):                    # add the leading zero: '.85' -> '0.85'
            return "0" + norm
        if norm.startswith("-."):
            return "-0" + norm[1:]
        return norm
    r = round(f, 2)
    if r == int(r):
        return str(int(r))
    return f"{r:.2f}".rstrip("0").rstrip(".")


def _stab_sub_label(row: dict) -> str:
    """Column leaf within a technique group (technique lives in the group header)."""
    size = row.get("particle_size_um")
    if size is not None:
        s = str(size)
        s = s.rstrip("0").rstrip(".") if "." in s else s
        return f">={s} um"
    analyte = row.get("analyte_name")
    if analyte:
        return translate_terms(str(analyte))
    return translate_terms(str(row.get("result_name") or ""))


def _display_unit(row: dict) -> str:
    """Unit as shown in the results table, folding in a 'per ...' qualifier from the
    warehouse's `detail_context` so a bare '[particles]' reads '[particles per container]'
    or '[particles per volume]'. The count unit alone is ambiguous — the per-container /
    per-volume basis lives in detail_context, and the business needs it in the label.

    Only 'per ...' contexts are folded in (they qualify the unit). Other context values
    (e.g. a reference standard like 'Ph. Eur. Reference') are left for the label/analyte
    rather than jammed into the unit bracket."""
    unit = (row.get("units") or "").strip()
    ctx = (row.get("detail_context") or "").strip()
    if ctx and re.match(r"(?i)per\b", ctx):
        return f"{unit} {ctx}".strip()
    return unit


def _sub_sort(label: str):
    m = re.search(r">=\s*([\d.]+)", label)
    return (0, float(m.group(1)), "") if m else (1, 0.0, label.lower())


def _row_sort(rk):
    tp, cond = rk
    if not tp:
        base = (2, 0)
    elif tp.lower().startswith("day"):
        base = (0, int(re.sub(r"\D", "", tp) or 0))
    else:
        m = re.match(r"(\d+)M", tp)
        base = (1, int(m.group(1)) if m else 999)
    return base + (_CONDITION_ORDER.get(cond or "", 9),)


# --- shaped data classes ----------------------------------------------------
@dataclass
class MethodRow:
    technique: str
    method_reference: str
    eln_unique_id: str          # comma-joined when shared across experiments
    confident: bool = True


@dataclass
class ResultParam:
    label: str
    values: dict[str, str]      # sample_id -> value (N/A-filled)


@dataclass
class ResultTechnique:
    technique: str
    params: list[ResultParam] = field(default_factory=list)


@dataclass
class BatchMatrix:
    """One §4 matrix for a batch (stability ATRs): rows = timepoint/condition,
    columns = parameters grouped by test (two-level header)."""
    batch_id: str
    lead_headers: list[str]                 # e.g. ["Timepoint", "Condition"]
    group_headers: list[tuple[str, int]]    # (technique_display, colspan)
    sub_headers: list[str]                  # flattened leaf headers (len = sum colspans)
    rows: list[list[str]]                   # each = lead values + one value per sub header
    # parallel to `rows`: True where the cell value came from Actual (reported value
    # unavailable) so the PDF can flag it for manual review. Lead cells are always False.
    actual_flags: list[list[bool]] = field(default_factory=list)


@dataclass
class ReviewFlag:
    severity: str               # "warn" | "info"
    area: str                   # "methods" | "results"
    message: str


@dataclass
class AtrReport:
    request_id_short: str
    request_id_display: str
    request_id_query: str
    header: dict[str, str]
    methods: list[MethodRow]
    sample_columns: list[str]
    results: list[ResultTechnique]
    scope: str
    summary_conclusion: str
    flags: list[ReviewFlag]
    is_stability: bool = False
    batch_matrices: list[BatchMatrix] = field(default_factory=list)


# --- builders ---------------------------------------------------------------
def _build_header(request_ids: dict[str, str], header_rows: list[dict]) -> dict[str, str]:
    h = header_rows[0] if header_rows else {}
    return {
        "cmc_request_id": request_ids["short"],
        "project_code": (h.get("project_code") or "").strip(),
        "batch_sample_id": (h.get("batch_sample_ids") or "").strip(),
    }


def _build_methods(method_rows: list[dict]) -> tuple[list[MethodRow], list[ReviewFlag]]:
    flags: list[ReviewFlag] = []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in method_rows:
        header = row.get("test_description") or ""
        # Match §4's technique extraction: keep only the trailing token after the last
        # '_' or '-' separator, dropping analyst/date prefixes (e.g. 'gosekmx_17Apr2026_
        # VisibleParticles' -> 'VisibleParticles'). Q2's SQL only split on '-', so §3 kept
        # the raw prefixes while §4 did not; this realigns them.
        seg = re.split(r"[_-]", header)[-1].strip()
        header = seg or header
        method_ref = row.get("test_method_reference")
        eln = str(row.get("eln_unique_id") or "").strip()
        technique, confident = resolve_method_technique(header, method_ref or "")
        base = strip_method_version(method_ref)
        key = (technique, base)
        if key not in grouped:
            grouped[key] = {"elns": set(), "confident": confident, "min_eln": eln}
            order.append(key)
        g = grouped[key]
        if eln:
            g["elns"].add(eln)
            if not g["min_eln"] or eln < g["min_eln"]:
                g["min_eln"] = eln
        g["confident"] = g["confident"] and confident

    rows: list[MethodRow] = []
    for key in sorted(order, key=lambda k: (grouped[k]["min_eln"], k[0])):
        technique, base = key
        g = grouped[key]
        elns = ", ".join(sorted(g["elns"], key=lambda x: (len(x), x)))
        rows.append(MethodRow(technique, base or "", elns, g["confident"]))
        if not base:
            flags.append(ReviewFlag("warn", "methods",
                f"No method reference recorded for '{technique}' (ELN {elns}) — manual entry required."))
        if not g["confident"]:
            flags.append(ReviewFlag("warn", "methods",
                f"Could not resolve an ambiguous 'Compendial' method (ELN {elns}) — verify manually."))
        if ATTACHMENT_MARKER.lower() in elns.lower():
            flags.append(ReviewFlag("info", "methods",
                f"'{technique}' is a 3rd-party result (out of scope) — append as {ATTACHMENT_MARKER}."))
    return rows, flags


def _build_results(result_rows: list[dict]) -> tuple[list[ResultTechnique], list[str], list[ReviewFlag]]:
    flags: list[ReviewFlag] = []
    samples = sorted({r.get("sample_id") for r in result_rows if r.get("sample_id")})

    tech_order: list[str] = []
    param_order: dict[str, list[str]] = {}
    cells: dict[str, dict[str, dict[str, str]]] = {}
    unconfident: set[str] = set()

    for row in result_rows:
        if _is_identifier_value(row.get("reported_value")):
            continue  # injection-id metadata row, not a result
        header = row.get("technique") or ""
        param_raw = row.get("result_name") or ""
        units = _display_unit(row)
        analyte = row.get("analyte_name")
        sample = row.get("sample_id") or ""
        value = row.get("reported_value")
        technique, confident = resolve_result_technique(header, param_raw)
        if not confident:
            unconfident.add(technique)
        label = _clean_param_label(technique, param_raw, units, analyte)

        cells.setdefault(technique, {})
        if technique not in param_order:
            param_order[technique] = []
            tech_order.append(technique)
        if label not in cells[technique]:
            cells[technique][label] = {}
            param_order[technique].append(label)
        if value is None or str(value).strip() == "":
            flags.append(ReviewFlag("warn", "results",
                f"Missing value for {technique} / {label} / {sample}."))
            value = ""
        cells[technique][label][sample] = _round_value(value)

    techniques = [
        ResultTechnique(t, [ResultParam(lbl, {s: cells[t][lbl].get(s, NA) for s in samples})
                            for lbl in param_order[t]])
        for t in tech_order
    ]
    for t in sorted(unconfident):
        flags.append(ReviewFlag("warn", "results",
            f"Ambiguous 'Compendial' result rows unassigned to a technique ('{t}') — verify manually."))
    return techniques, samples, flags


def _is_stability(result_rows: list[dict]) -> bool:
    """A request is a stability ATR if any result sample carries a parsed timepoint."""
    return any((r.get("timepoint_parsed") or "").strip() for r in result_rows)


def _batch_label(batch_id: str, lot_display: str | None) -> str:
    """Human batch label. For PEGA batches LOTS.displayname is the DP/SAP lot number
    used in the manual report -> show 'lot (system_id)', e.g. '1001707466 (pega-prod-BA00006111)'
    or '24-004735 (pega-prod-BA00007748)'. Otherwise (NEST/IDBS, where displayname is a
    process step or absent) show the system id."""
    bid = batch_id or ""
    lot = str(lot_display or "").strip()
    # A real DP/SAP lot number is digits and separators only — pure ('1005526968') or
    # hyphenated SAP form ('24-004735'). A process-step name (NEST/IDBS) has letters. So
    # accept a letter-free value carrying at least five digits; otherwise show the system id.
    if lot and not re.search(r"[A-Za-z]", lot) and len(re.sub(r"\D", "", lot)) >= 5:
        return f"{lot} ({bid})"
    return bid


def _build_batch_matrices(result_rows: list[dict], lot_map: dict | None = None) -> list[BatchMatrix]:
    """One §4 matrix per batch — universal (dev-samples AND stability):
        ROWS    = samples (with Timepoint / Storage lead columns when present),
        COLUMNS = tests grouped, each with its granularity leaf
                  (particle size / analyte-peak / parameter),
        CELLS   = value(s), '/'-joined when multiple.
    Lead columns / test columns that don't apply to a request simply don't appear.
    """
    from collections import defaultdict

    # Sub-visible particle (e.g. MFI) counts are stored twice — "per container" and
    # "per volume" (per mL) — both tied to a particle size via ECD. We keep only one
    # to avoid duplicate columns, preferring "per container". BUT many requests record
    # MFI *only* per volume; blanket-dropping per-volume made the whole MFI section
    # disappear for those. So drop a per-volume row only when a per-container value
    # exists for the SAME measurement (batch / sample / technique / size).
    _pc_keys = {
        (r.get("batch_id"), r.get("sample_id"), _tech_key(r.get("technique")),
         r.get("particle_size_um"))
        for r in result_rows
        if (r.get("detail_context") or "") == "per container"
        and r.get("particle_size_um") is not None
    }
    rows = []
    for r in result_rows:
        if str(r.get("result_name") or "").strip().lower() in _METADATA:
            continue
        if _is_identifier_value(r.get("reported_value")):
            continue  # injection-id metadata row, not a result
        if (r.get("particle_size_um") is not None
                and (r.get("detail_context") or "") == "per volume"
                and (r.get("batch_id"), r.get("sample_id"), _tech_key(r.get("technique")),
                     r.get("particle_size_um")) in _pc_keys):
            continue  # per-container duplicate exists → drop the per-volume copy
        rows.append(r)
    by_batch: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_batch[r.get("batch_id") or ""].append(r)

    has_tp = any((r.get("timepoint_parsed") or "") for r in rows)
    has_cond = any((r.get("storage_condition") or "") for r in rows)

    def _sample_of(r):
        return r.get("sample_name") or r.get("sample_id") or ""

    matrices: list[BatchMatrix] = []
    for batch in sorted(by_batch):
        brows = by_batch[batch]

        # columns: technique group -> granularity leaves
        tnames, subs, unit_of = defaultdict(set), defaultdict(set), {}
        for r in brows:
            tk = _tech_key(r.get("technique"))
            tnames[tk].add(r.get("technique") or "")
            sl = _stab_sub_label(r)
            subs[tk].add(sl)
            unit_of[(tk, sl)] = _display_unit(r)
        group_order = sorted(tnames, key=lambda k: _pick_display(tnames[k]).lower())
        groups = [(tk, _pick_display(tnames[tk]), sorted(subs[tk], key=_sub_sort)) for tk in group_order]
        colspec = [(tk, sl) for tk, _disp, sls in groups for sl in sls]

        # rows: one per SAMPLE (carry its timepoint / condition)
        samp_meta: dict[str, tuple] = {}
        for r in brows:
            s = _sample_of(r)
            samp_meta.setdefault(s, (r.get("timepoint_parsed") or "", r.get("storage_condition") or ""))
        samples = sorted(samp_meta, key=lambda s: (_row_sort(samp_meta[s]) if has_tp else (0,), s))

        cells: dict = defaultdict(list)
        cells_actual: dict = defaultdict(bool)   # key -> any contributing value from Actual
        for r in brows:
            val = _round_value(r.get("reported_value"))
            if not val:
                continue
            key = (_sample_of(r), _tech_key(r.get("technique")), _stab_sub_label(r))
            if val not in cells[key]:
                cells[key].append(val)
            if (r.get("value_source") or "") == "actual":
                cells_actual[key] = True

        lead_headers = ["Sample"] + (["Timepoint"] if has_tp else []) + (["Storage"] if has_cond else [])
        group_headers = [(disp, len(sls)) for _tk, disp, sls in groups]
        sub_headers = [f"{sl} [{unit_of[(tk, sl)]}]" if unit_of.get((tk, sl)) else sl
                       for tk, sl in colspec]
        n_lead = len(lead_headers)
        data_rows, flag_rows = [], []
        for s in samples:
            tp, cond = samp_meta[s]
            lead = [s] + ([tp] if has_tp else []) + ([cond] if has_cond else [])
            vals, flags = [], [False] * n_lead
            for tk, sl in colspec:
                cell_vals = cells.get((s, tk, sl), [])
                vals.append(" / ".join(cell_vals))
                flags.append(bool(cell_vals) and cells_actual.get((s, tk, sl), False))
            data_rows.append(lead + vals)
            flag_rows.append(flags)
        matrices.append(BatchMatrix(batch_id=_batch_label(batch, (lot_map or {}).get(batch)),
                                    lead_headers=lead_headers,
                                    group_headers=group_headers, sub_headers=sub_headers,
                                    rows=data_rows, actual_flags=flag_rows))
    return matrices


def build_atr_report(request_id: str, raw: dict[str, list[dict]]) -> AtrReport:
    """Build the full ATR report object from a request id and the Q1–Q4 output
    (``raw`` = {"header","methods","results","scope_summary", ...})."""
    ids = normalize_request_id(request_id)
    result_rows = raw.get("results") or []
    methods, mflags = _build_methods(raw.get("methods") or [])
    results, samples, rflags = _build_results(result_rows)
    ss = (raw.get("scope_summary") or [{}])
    ss0 = ss[0] if ss else {}
    stability = _is_stability(result_rows)
    lot_map = {r.get("batch_id"): r.get("lot_display") for r in (raw.get("batch_lots") or [])}
    header = _build_header(ids, raw.get("header") or [])
    if lot_map:   # show human batch/lot ids (with system id) instead of raw batch_id list
        labels = [_batch_label(b, lot_map.get(b)) for b in sorted(k for k in lot_map if k)]
        if labels:
            header["batch_sample_id"] = ", ".join(labels)

    batch_matrices = _build_batch_matrices(result_rows, lot_map)   # per-batch matrix for every request
    flags = mflags + rflags
    # Actual-Value use is no longer amber-shaded in the PDF (business decision); surface it
    # as a review remark the author sees before finalizing/downloading. The DOCX keeps amber.
    if any(any(fr) for bm in batch_matrices for fr in bm.actual_flags):
        flags.append(ReviewFlag("warn", "results",
            "Some results were taken from the Actual Value because a Reported Value was "
            "unavailable in the CMC Data Warehouse. Verify / convert to a reported value "
            "(rounding, significant figures) before finalizing."))
    return AtrReport(
        request_id_short=ids["short"],
        request_id_display=ids["display"],
        request_id_query=ids["query"],
        header=header,
        methods=methods,
        sample_columns=samples,
        results=results,
        scope=(ss0.get("scope") or "").strip(),
        summary_conclusion=(ss0.get("summary_conclusion") or "").strip(),
        flags=flags,
        is_stability=stability,
        batch_matrices=batch_matrices,
    )
