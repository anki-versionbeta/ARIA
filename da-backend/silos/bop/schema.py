"""The BOP document shape and its generation rules.

Ported verbatim from the BOP app: these strings are prompt *content* — the rules the
model is held to — so changing their wording changes the output. Treat edits here as
behaviour changes, not refactoring.
"""

from __future__ import annotations

from typing import Any

# ── per-section JSON shapes, one per generation call ──────────────────────────

SECTION_SCHEMAS: dict[str, str] = {
    # purpose/scope are HTML strings rendered by the emitter's HTML-aware body
    # writer — a lead sentence plus capability bullets for purpose, and
    # Personnel/Sites labelled paragraphs for scope.
    "purpose": '"purpose": "<p>lead sentence…</p><ul><li>capability…</li></ul>"',
    "scope": '"scope": "<p><strong>Personnel:</strong> …</p><p><strong>Sites:</strong> …</p>"',
    "description": (
        '"description": {\n'
        '    "components":     {"vessel": "...", "pump": "...", "agitation": "...",\n'
        '                       "instrumentation": "...", "software": "..."},\n'
        '    "specifications": {"dimensions": "...", "capacity": "...",\n'
        '                       "temperature_range": "...", "pressure_range": "...",\n'
        '                       "wetted_materials": "..."},\n'
        '    "utilities":      {"electrical": "...", "heating_cooling": "...",\n'
        '                       "water_steam": "...", "gases": "..."},\n'
        '    "tools":          ["..."],\n'
        '    "spare_parts":    ["..."],\n'
        '    "consumables":    ["..."],\n'
        '    "materials":      ["..."]\n'
        "  }"
    ),
    "safety": (
        '"safety": {\n'
        '    "hazards":              "<prose>",\n'
        '    "engineering_controls": "<prose>",\n'
        '    "ppe":                  "<prose>"\n'
        "  }"
    ),
    "abbreviations": '"abbreviations": [["ACRONYM", "definition"]]',
    "related_documents": '"related_documents": ["..."]',
}

# Only sections listed here get an extra authoritative style block in their prompt.
SECTION_STYLE_NOTES: dict[str, str] = {
    "purpose": (
        'Return the "purpose" value as a single HTML string built as follows:\n'
        "1. One lead <p> stating that this document is a standard operating "
        "procedure (SOP) establishing best operating practices for the safe "
        "setup, operation, maintenance, and shutdown of the system. Name the "
        "system exactly as it appears in the manual and include its manual / "
        "part number if the manual provides one. End the lead sentence by "
        'introducing what the system is designed to do (e.g. "… designed to:").\n'
        "2. Immediately follow with a <ul> containing 3-6 <li> bullets, each a "
        "concrete capability of the system taken from the manual (what it does, "
        "including quantified specs only where the manual states them).\n"
        "Use only <p>, <ul>, and <li> tags. Do not invent capabilities the "
        "manual does not support."
    ),
    "scope": (
        'Return the "scope" value as a single HTML string containing exactly '
        "two labeled paragraphs:\n"
        "<p><strong>Personnel:</strong> …</p> — who is authorized to operate, "
        "maintain, and service the system (e.g. trained laboratory analysts, "
        "technicians, scientists, administrators; service/repair by authorized "
        "manufacturer personnel or qualified engineers).\n"
        "<p><strong>Sites:</strong> …</p> — the environments and settings where "
        "the system is used (e.g. GLP/GMP-regulated labs, research environments, "
        "and the relevant industries/applications).\n"
        "Draw specifics from the manual; do not fabricate. Use only <p> and "
        "<strong> tags."
    ),
}

# ── operating procedure ───────────────────────────────────────────────────────
# Each paired phase is its own LLM call returning
# {"<phase>": {"equipment": [...], "software": [...]}}. Splitting per phase keeps
# every response small enough not to truncate and keeps the two step groups apart.

OP_PAIRED_PHASES: list[str] = [
    "setup",
    "startup",
    "operation",
    "shutdown",
    "emergency_shutdown",
    "cleaning",
    "storage",
    "maintenance",
]

OP_PHASE_LABELS: dict[str, str] = {
    "setup": "Setup",
    "startup": "Startup",
    "operation": "Routine Operation",
    "shutdown": "Shutdown",
    "emergency_shutdown": "Emergency Shutdown",
    "cleaning": "Cleaning",
    "storage": "Storage",
    "maintenance": "Maintenance",
    "troubleshooting": "Troubleshooting",
}

OP_PHASE_SCHEMAS: dict[str, str] = {
    phase: f'"{phase}": {{"equipment": ["..."], "software": ["..."]}}'
    for phase in OP_PAIRED_PHASES
}
OP_PHASE_SCHEMAS["troubleshooting"] = (
    '"troubleshooting": [{"issue": "...", "resolution": "..."}]'
)

OP_PHASE_EMPTY: dict[str, Any] = {
    phase: {"equipment": [], "software": []} for phase in OP_PAIRED_PHASES
}
OP_PHASE_EMPTY["troubleshooting"] = []

# Safe default per section, used when a call fails, so the final document is still
# well-formed for the emitter and the editor.
SECTION_EMPTY: dict[str, Any] = {
    "purpose": "",
    "scope": "",
    "description": {
        "components": {},
        "specifications": {},
        "utilities": {},
        "tools": [],
        "spare_parts": [],
        "consumables": [],
        "materials": [],
    },
    "safety": {"hazards": "", "engineering_controls": "", "ppe": ""},
    "operating_procedure": {
        "setup": {"equipment": [], "software": []},
        "startup": {"equipment": [], "software": []},
        "operation": {"equipment": [], "software": []},
        "shutdown": {"equipment": [], "software": []},
        "emergency_shutdown": {"equipment": [], "software": []},
        "cleaning": {"equipment": [], "software": []},
        "storage": {"equipment": [], "software": []},
        "maintenance": {"equipment": [], "software": []},
        "troubleshooting": [],
    },
    "abbreviations": [],
    "related_documents": [],
}

# Shared contract for EVERY operating-procedure phase. Used by both the static
# bounds below and the gold-derived bound, so the two paths always agree on the
# equipment/software split.
OP_BOUND: str = (
    "For this phase, produce TWO separate groups of steps:\n"
    '- "equipment": physical actions on the hardware (power, valves, '
    "connections, mechanical adjustments, and reading gauges/indicators).\n"
    '- "software": actions in the system\'s control or data-acquisition '
    "software (login, method/recipe selection, parameter entry, start/stop, "
    "and reading on-screen values or alarms). Use the software named in the "
    "source manual.\n"
    "Order the steps within each group as an operator actually performs them, "
    "so the two groups together describe operating the system end-to-end for "
    "this phase. Write MANY FINE-GRAINED steps rather than a few coarse ones, "
    "and give EACH step enough rationale to follow confidently — the action, "
    "what to confirm, and why it matters. If the source manual describes no "
    'software interaction for this phase, return an EMPTY "software" list — '
    "do NOT invent software steps."
)

# Hardcoded length/style rules per section. These are RULES (how the model should
# write), not content. Paired with the template examples extracted at runtime.
# Gold-derived bounds override these where the gold document supplies one.
SECTION_BOUNDS: dict[str, str] = {
    "purpose": (
        "Write 1–2 short sentences, ≤80 words total. State that this is an SOP "
        "and what the system is used for. Do NOT enumerate applications, "
        "configurations, pressure/temperature limits, or industries — those "
        "belong in the Description section."
    ),
    "scope": (
        "Write ONE short sentence (≤60 words) identifying the personnel "
        "responsible for operation/maintenance and the site or building where "
        "the system is located. Do NOT list system modules, configurations, "
        "operating ranges, training requirements, or environmental conditions. "
        "Do NOT produce multiple paragraphs."
    ),
    "description": (
        "Each subsection field must be 1 short sentence or a short bullet "
        "list. Specifications and utilities use noun-phrase fragments "
        "(e.g. 'Up to 250 °C', '208 V 3-phase'), not paragraphs. "
        "Tools / spare parts / consumables / materials are concise lists, not "
        "prose."
    ),
    "safety": (
        "Each prose field (hazards, engineering_controls, ppe) is at most "
        "ONE short paragraph or a brief category-by-category list. "
        "Do NOT expand any single category into multiple paragraphs."
    ),
    "operating_procedure": OP_BOUND,
    "abbreviations": (
        "Each entry is a [acronym, definition] pair. The definition is the "
        "short expansion only (e.g. 'Supercritical Fluid Extraction'), "
        "NOT a sentence."
    ),
    "related_documents": (
        "List of short document titles only (e.g. 'Operating Manual', "
        "'Maintenance Guide', 'Cleaning Procedure'). Do NOT include "
        "descriptions, attachments, signature pages, or change-control notes."
    ),
}

# The six sections generated by their own call; operating_procedure is generated
# per phase instead.
GENERATED_SECTIONS: list[str] = [
    "purpose",
    "scope",
    "description",
    "safety",
    "abbreviations",
    "related_documents",
]


def dotted_set(obj: Any, key_path: str, value: Any) -> None:
    """Set a value at a dotted path, creating intermediate dicts.

    Preserves the list-ness of an existing value, because the editor saves an HTML
    string into a slot that may have held a list.
    """
    parts = key_path.split(".")
    cursor = obj
    for part in parts[:-1]:
        if isinstance(cursor, dict):
            if part not in cursor or not isinstance(cursor[part], (dict, list)):
                cursor[part] = {}
            cursor = cursor[part]
        else:
            raise KeyError(f"Cannot descend into non-dict at {part!r}")
    last = parts[-1]
    if not isinstance(cursor, dict):
        raise KeyError(f"Cannot set {last!r} on non-dict")
    existing = cursor.get(last)
    if isinstance(existing, list):
        cursor[last] = value if isinstance(value, list) else [value]
    else:
        cursor[last] = value


def dotted_get(obj: Any, key_path: str, default: Any = None) -> Any:
    cursor = obj
    for part in key_path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def empty_document() -> dict[str, Any]:
    """A deep copy of the empty shape, safe to mutate."""
    from copy import deepcopy

    return deepcopy(SECTION_EMPTY)
