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

# The order the built document puts these in, so the review screen lists them the way the
# reader will meet them. The two lists above are grouped by *kind*, which is the wrong
# axis for a reader: it separates `description.tools` from the rest of DESCRIPTION and
# leaves `purpose` in the middle of the alphabet.
#
# `emit.build_docx` is the source of truth for this sequence. A leaf added above without a
# place here is caught by test_bop_section_order.py, which asserts this covers exactly the
# keys `flatten()` produces.
SECTION_ORDER: list[str] = [
    "purpose",
    "scope",
    # DESCRIPTION
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
    "description.tools",
    "description.spare_parts",
    "description.consumables",
    "description.materials",
    # SAFETY
    "safety.hazards",
    "safety.engineering_controls",
    "safety.ppe",
    # OPERATING PROCEDURE — equipment and software interleave per phase in the document,
    # rather than all equipment followed by all software.
    *[
        f"operating_procedure.{phase}.{group}"
        for phase in OP_PAIRED_PHASES
        for group in ("equipment", "software")
    ],
    # RELATED DOCUMENTS is the document's last section.
    "related_documents",
]


# Tags that end a line of text. Used to keep entries apart outside a list, where the
# editor separates them with markup rather than newlines.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "ul", "ol", "li", "table", "tr", "blockquote", "pre",
        "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)


class _ListItemParser(HTMLParser):
    """Collect list entries, from <li> elements and from text typed outside them.

    Text outside a <li> is still an entry. The editor wraps a line typed below the
    bullets in a <p> after the </ul>, so dropping it loses that edit silently — the
    section saves, a version is appended, and the built document never mentions it.

    Entries are emitted in the order they appear, so a line typed above the list keeps
    its place rather than being appended at the end.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[str] = []
        self._depth = 0
        self._buffer: list[str] = []
        self._loose: list[str] = []

    def _flush_loose(self) -> None:
        """Turn text collected outside any <li> into entries, one per line."""
        for line in "".join(self._loose).splitlines():
            line = line.strip()
            if line:
                self.entries.append(line)
        self._loose = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "li":
            # Anything loose before this item belongs ahead of it.
            self._flush_loose()
            self._depth += 1
            self._buffer = []
        elif tag == "br" and self._depth:
            self._buffer.append(" ")
        elif tag in _BLOCK_TAGS and not self._depth:
            self._loose.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "li" and self._depth:
            self._depth -= 1
            text = "".join(self._buffer).strip()
            if text:
                self.entries.append(text)
            self._buffer = []
        elif tag in _BLOCK_TAGS and not self._depth:
            self._loose.append("\n")

    def handle_data(self, data):
        if self._depth:
            self._buffer.append(data)
        else:
            self._loose.append(data)

    def result(self) -> list[str]:
        self._flush_loose()
        return self.entries


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
