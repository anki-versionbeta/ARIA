"""MFGR constants and the batch-id normaliser. Ported from `src/config.py:96-238`.

Only the identifier contract lands here for now — `UNIT_MAP`, `UNIT_OP_MAP`,
`EXPECTED_UNIT_OP_LABELS` and `parse_batch_name` arrive with the MFGR pipeline.

`BATCH_ID_QUERY_PREFIX` was read from an `MFGR_BATCH_PREFIX` variable, defaulting to
"nest-br-prod-", and is now a plain constant at that default, for the same reason as ATR's
prefix: a silo may not read the environment. That drops the override — see the note in
`config.py`.
"""

from __future__ import annotations

import re

# Batch-id forms.  Two registration paths per RepBatch Data Mapping §4.2:
#   • NEST-registered — BAX###### (e.g. BAX000701, BAX000584) → query root
#     "nest-br-prod-BAX000701", displayed as "BAX000701".
#   • Manually-registered — BA####### (e.g. BA259821, BA2510880) → query root is
#     the bare id (no nest-br-prod- prefix); confirmed by comparing example MFGRs
#     L-RDLU-000658 (BA259821-08) and L-RDLU-001296 (BA2510880-02).
BATCH_ID_QUERY_PREFIX = "nest-br-prod-"
_BATCH_ID_RE = re.compile(r"\bBA[A-Z]?\d{4,}\b", re.IGNORECASE)
# Retained for legacy imports — same object under old name.
_BAX_RE = _BATCH_ID_RE


def normalize_batch_id(raw: str) -> dict[str, str]:
    """Normalize any accepted batch-id form to its representations.

    NEST-registered (BAX prefix) → query with 'nest-br-prod-' prefix.
    Manually-registered (BA + digits, no X) → query with bare id.

    >>> normalize_batch_id("BAX000584")
    {'short': 'BAX000584', 'display': 'BAX000584', 'query': 'nest-br-prod-BAX000584', 'registration': 'nest'}
    >>> normalize_batch_id("BA259821-08")
    {'short': 'BA259821', 'display': 'BA259821', 'query': 'BA259821', 'registration': 'manual'}
    """
    if not raw:
        raise ValueError("batch id is required")
    m = _BATCH_ID_RE.search(str(raw))
    if not m:
        raise ValueError(
            f"could not find a BA/BAX batch id in {raw!r} "
            "(expected e.g. 'BAX000584' or 'BA259821')"
        )
    short = m.group(0).upper()
    is_nest = short.startswith("BAX")
    return {
        "short": short,
        "display": short,
        "query": f"{BATCH_ID_QUERY_PREFIX}{short}" if is_nest else short,
        "registration": "nest" if is_nest else "manual",
    }

# Appended verbatim from src/config.py:104-209 - the ontology maps and the NEST batch-name
# parser. These were deferred when this file was first written; nothing about them changed.

# Unit-URI → human label. Units are decoded in-SQL where possible; this mirror is used
# for fixtures / any post-hoc decoding. Pending: DSDT_0000024 (currently unknown).
UNIT_MAP = {
    # QUDT vocab
    "http://qudt.org/vocab/unit/L":                     "L",
    "http://qudt.org/vocab/unit/MilliL":                "mL",
    "http://qudt.org/vocab/unit/GM":                    "g",
    "http://qudt.org/vocab/unit/MilliGM":               "mg",
    "http://qudt.org/vocab/unit/MilliGM-PER-MilliL":    "mg/mL",
    "http://qudt.org/vocab/unit/GM-PER-MilliL":         "g/mL",
    "http://qudt.org/vocab/unit/MilliMOL-PER-L":        "mM",
    "http://qudt.org/vocab/unit/MOL-PER-L":             "M",
    "http://qudt.org/vocab/unit/MicroGM-PER-MilliL":    "µg/mL",
    "http://qudt.org/vocab/unit/MicroGM":               "µg",
    "http://qudt.org/vocab/unit/NanoM":                 "nm",
    "http://qudt.org/vocab/unit/DEG_C":                 "°C",
    "http://qudt.org/vocab/unit/PERCENT":               "%",
    "http://qudt.org/vocab/unit/NTU":                   "NTU",
    "http://qudt.org/vocab/unit/MicroM":                "µm",
    "http://qudt.org/vocab/unit/KiloGM":                "kg",
    # AbbVie DSDT (confirmed against live data)
    "https://ontology.abbvienet.com/dsdt/DSDT_0000133": "L",
    "https://ontology.abbvienet.com/dsdt/DSDT_0000024": "g",   # confirmed: 538 g fill amount
    # Unit-count URIs (stubs — pending Alexander confirmation). Filling and Visual
    # Inspection stages emit counts (PFS/vials), not masses; without these, the
    # decoder falls back to raw URI or wrongly reuses "g". See Tim BAX001675 feedback.
    "http://qudt.org/vocab/unit/NUM":                   "units",   # bare count
    "http://qudt.org/vocab/unit/PACKAGE":               "units",   # unverified
}

# Unit-operation labels that produce *counts*, not masses/volumes. Used by the
# amount decoder to override a mis-mapped "g" unit for these stages (BAX001675
# regression — DWH tagged fill amount with the mass URI).
COUNT_OP_LABELS = frozenset({
    "filling",
    "crimping",
    "visual inspection",
    "100% visual inspection",
    "vacuum stopper",   # cartridge line: stage between Filling and Crimping
})

# Fallback stage-index → readable label. Applied only when the row's
# unit_operation_uri is undecoded AND BATCH_PROCESS_STEPS.STAGE is empty.
# Ordered by the typical rep-batch stage sequence (mapping doc §5).
HEURISTIC_STAGE_NAMES: dict[int, str] = {
    1: "Thawing and Pooling",
    2: "Mixing",
    3: "Bioburden Reduction Filtration",
    4: "Sterilization by Filtration",
    5: "Filling",
    6: "Filling",
    7: "Visual Inspection",
    8: "Storage",
}

# DSDT unit-operation URIs → readable stage label. Every entry below is UNVERIFIED
# (guessed by inspection) — the client owns the DSDT ontology and must give us the
# authoritative lookup. Unknown URIs surface as review flags at report-build time.
UNIT_OP_MAP = {
    "https://ontology.abbvienet.com/dsdt/DSDT_0000341": "Compounding",     # unverified
    "https://ontology.abbvienet.com/dsdt/DSDT_0000360": "Filling",         # unverified
    "https://ontology.abbvienet.com/dsdt/DSDT_0000419": "Formulation",     # unverified
    "https://ontology.abbvienet.com/dsdt/DSDT_0000539": "Filtration",      # unverified
    # BAX000754 / BAX000825 cartridge line — reviewer confirmed 0362.
    # 0619 / 0358 inferred from stage ordering (Vacuum Stopper → Crimping → VI).
    "https://ontology.abbvienet.com/dsdt/DSDT_0000362": "Vacuum Stopper",  # confirmed BAX000754
    "https://ontology.abbvienet.com/dsdt/DSDT_0000619": "Crimping",        # unverified
    "https://ontology.abbvienet.com/dsdt/DSDT_0000358": "Visual Inspection",  # unverified
}

# Human-readable unit-operation labels observed in real MFGR reports
# (L-RDLU-000658 §5, L-RDLU-001296 §5, mapping doc rows 16-17). This is the
# vocabulary we expect the client's DSDT lookup to cover. Kept for downstream
# validation — if a decoded label is NOT in this set, flag it for review.
EXPECTED_UNIT_OP_LABELS = frozenset({
    "Thawing and Pooling",
    "Mixing",
    "Bioburden Reduction Filtration",
    "Sterilization by Filtration",   # a.k.a. "Sterile Filtration"
    "Filling",
    "Crimping",                       # feeds pre-VI yield
    "Visual Inspection",              # feeds post-VI yield (a.k.a. "100% Visual Inspection")
    "Storage",                        # feeds actual batch size
    "Freeze-drying",                  # lyo batches only
    "Reconstitution",                 # lyo batches only
    "Compounding",                    # legacy
    "Formulation",                    # legacy
    "Filtration",                     # legacy
})

# --- NEST batch-name parser -------------------------------------------------
# Per RepBatch Data Mapping doc §3.1: several MFGR title fields are encoded in the NEST
# batch name string. Examples: "ABBV-423 360 mg S.INJ 2 mL Vial".
# The parser is intentionally lenient (whitespace / mg / mL optional).
_DOSE_RE = re.compile(r"(?P<dose>\d+(?:\.\d+)?)\s*mg\b", re.IGNORECASE)
_FORM_RE = re.compile(r"\b(?P<form>P\.?S\.?INJ|S\.?INJ|SOL\.?F\.?INJ|P\.?F\.?SOL\.?F\.?INJ)\b", re.IGNORECASE)
_VOL_RE = re.compile(r"(?P<vol>\d+(?:\.\d+)?)\s*mL\b", re.IGNORECASE)
_CONTAINER_RE = re.compile(r"\b(?P<container>PFS|Vial|Cartridge|Syringe)\b", re.IGNORECASE)

DOSE_FORM_LABELS = {
    "S.INJ":         "Solution for Injection",
    "SINJ":          "Solution for Injection",
    "P.S.INJ":       "Powder for Solution for Injection",
    "PSINJ":         "Powder for Solution for Injection",
    "SOL.F.INJ":     "Solution for Injection",
    "P.F.SOL.F.INJ": "Powder for Solution for Injection",
}


def parse_batch_name(name: str | None) -> dict[str, str]:
    """Extract dose / dosage form / withdraw volume / container from a NEST batch name.

    Missing pieces come back as empty strings. Callers should treat empties as "manual".

    >>> parse_batch_name("ABBV-423 360 mg S.INJ 2 mL Vial")
    {'dose_mg': '360', 'dose_form_code': 'S.INJ', 'dose_form_label': 'Solution for Injection', 'withdraw_volume_ml': '2', 'container': 'Vial'}
    """
    s = (name or "").strip()
    out = {"dose_mg": "", "dose_form_code": "", "dose_form_label": "",
           "withdraw_volume_ml": "", "container": ""}
    if not s:
        return out
    m = _DOSE_RE.search(s)
    if m:
        out["dose_mg"] = m.group("dose")
    m = _FORM_RE.search(s)
    if m:
        code = m.group("form").upper().replace(".", "")
        # normalize back with dots for lookup key
        code_dotted = m.group("form").upper()
        out["dose_form_code"] = code_dotted
        out["dose_form_label"] = DOSE_FORM_LABELS.get(code_dotted) or DOSE_FORM_LABELS.get(code, code_dotted)
    m = _VOL_RE.search(s)
    if m:
        out["withdraw_volume_ml"] = m.group("vol")
    m = _CONTAINER_RE.search(s)
    if m:
        raw = m.group("container")
        # Keep acronyms uppercase (PFS); title-case ordinary words (Vial / Syringe / Cartridge)
        out["container"] = raw.upper() if raw.isupper() or raw.upper() == "PFS" else raw.capitalize()
    return out
