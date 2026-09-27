"""Mapping between the BOP document and the platform's editable sections.

The app being replaced stores the edited HTML string straight back at the dotted path.
That works for prose leaves, but where the slot held a *list* (Tools, Troubleshooting,
Abbreviations) it replaces the list with a string — and the emitter then renders
nothing for it, silently dropping the section from the .docx.

Types are preserved here instead: a list leaf is rendered to `<ul><li>` for editing and
parsed back into a list on build. The only other difference is that an **untouched**
section is never written back, so generated structure cannot be overwritten by a
rendering of itself.
"""

from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
from typing import Any

from .schema import OP_PAIRED_PHASES, dotted_get, dotted_set

# Prose leaves: edited as HTML, stored as HTML. Straightforward.
PROSE_LEAVES: list[str] = [
    "purpose",
    "scope",
    "description.components.vessel",
    "description.components.pump",
    "description.components.agitation",
    "description.components.instrumentation",
    "description.components.software",
    "description.specifications.dimensions",
    "description.specifications.capacity",
    "description.specifications.temperature_range",
    "description.specifications.pressure_range",
    "description.specifications.wetted_materials",
    "description.utilities.electrical",
    "description.utilities.heating_cooling",
    "description.utilities.water_steam",
    "description.utilities.gases",
    "safety.hazards",
    "safety.engineering_controls",
    "safety.ppe",
]

# List-of-string leaves: rendered to <ul>, parsed back to a list.
LIST_LEAVES: list[str] = [
    "description.tools",
    "description.spare_parts",
    "description.consumables",
    "description.materials",
    "related_documents",
    *[f"operating_procedure.{phase}.equipment" for phase in OP_PAIRED_PHASES],
    *[f"operating_procedure.{phase}.software" for phase in OP_PAIRED_PHASES],
]


class _ListItemParser(HTMLParser):
    """Collect the text of each <li>, falling back to whole-text on plain input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[str] = []
        self._depth = 0
        self._buffer: list[str] = []
        self._loose: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "li":
            self._depth += 1
            self._buffer = []
        elif tag.lower() == "br" and self._depth:
            self._buffer.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() == "li" and self._depth:
            self._depth -= 1
            text = "".join(self._buffer).strip()
            if text:
                self.items.append(text)
            self._buffer = []

    def handle_data(self, data):
        if self._depth:
            self._buffer.append(data)
        else:
            self._loose.append(data)

    def result(self) -> list[str]:
        if self.items:
            return self.items
        # No <li> at all: treat non-empty lines as entries.
        return [line.strip() for line in "".join(self._loose).splitlines() if line.strip()]


def html_to_list(html: str) -> list[str]:
    parser = _ListItemParser()
    parser.feed(html or "")
    parser.close()
    return parser.result()


def list_to_html(items: Any) -> str:
    if not isinstance(items, list):
        return str(items or "")
    entries = [str(item).strip() for item in items if str(item or "").strip()]
    if not entries:
        return ""
    body = "".join(f"<li>{entry}</li>" for entry in entries)
    return f"<ul>{body}</ul>"


def flatten(bop_json: dict[str, Any]) -> dict[str, str]:
    """Editable sections for the platform, keyed by dotted path."""
    sections: dict[str, str] = {}
    for key in PROSE_LEAVES:
        value = dotted_get(bop_json, key, "")
        sections[key] = value if isinstance(value, str) else str(value or "")
    for key in LIST_LEAVES:
        sections[key] = list_to_html(dotted_get(bop_json, key, []))
    return sections


def apply_edits(
    bop_json: dict[str, Any], rows: list[tuple[str, str, int]]
) -> dict[str, Any]:
    """Overlay edited sections onto the generated document.

    `revision == 1` means the section is exactly as generated, so it is skipped —
    writing it back would turn structured values into a rendering of themselves.
    """
    result = deepcopy(bop_json)
    list_leaves = set(LIST_LEAVES)
    for key, html, revision in rows:
        if revision <= 1:
            continue
        if key in list_leaves:
            dotted_set(result, key, html_to_list(html))
        else:
            dotted_set(result, key, html)
    return result
