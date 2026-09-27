"""A normalised structural projection of a .docx, for golden-file comparison.

Two .docx files built from identical content are never byte-identical: the zip stores a
modification time per entry, `docProps/core.xml` carries created/modified stamps, and Word
rewrites revision ids on every save. So `assert produced == expected` on bytes fails 100%
of the time and tells you nothing about whether the document changed.

This projects `word/document.xml` down to what a reviewer would actually notice — the
heading sequence, table geometry, cell text, and the count and alt text of embedded
images — and compares those. Headers, footers, styles and numbering definitions are out of
scope: they are set once at document build time and are covered by unit tests instead.

Standard library only. Nothing can be installed here (the proxy denies pypi), and a
comparison helper that needed a dependency would be worse than no helper.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree

# OOXML namespaces, spelled out rather than discovered, so a malformed part yields an
# empty projection instead of a confusing KeyError.
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

DOCUMENT_PART = "word/document.xml"
MEDIA_PREFIX = "word/media/"

# Paragraph styles whose TEXT is reported but never fails a comparison. The title printed
# inside the document comes from the PDF's own metadata and falls back to the file's
# basename — which under the platform is a materialised temporary file, so it cannot be
# reproduced exactly for a PDF carrying no metadata title. The Title paragraph's presence
# and position ARE compared; only its text is exempt.
TITLE_STYLES = frozenset({"title"})

# Cell text is compared as one stream with a similarity floor rather than for equality,
# because any page routed through garbled-text repair has its body text produced by a live
# model and is not reproducible word for word. Structural drift moves this ratio far more
# than wording jitter does.
CELL_TEXT_SIMILARITY = 0.98

_WHITESPACE = re.compile(r"\s+")


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _norm(text: str) -> str:
    """Collapse whitespace and non-breaking spaces.

    Word splits one sentence across arbitrarily many runs and inserts soft breaks wherever
    it likes, so raw concatenated text is not comparable without this.
    """
    return _WHITESPACE.sub(" ", (text or "").replace(" ", " ")).strip()


def _style(element: ElementTree.Element) -> str:
    """The paragraph's style id, lowercased with spaces removed.

    python-docx writes "Heading 1" while Word may normalise the same style to "Heading1";
    both must project identically.
    """
    properties = element.find(_q(W, "pPr"))
    if properties is None:
        return ""
    style = properties.find(_q(W, "pStyle"))
    if style is None:
        return ""
    return (style.get(_q(W, "val")) or "").replace(" ", "").lower()


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    pieces: list[str] = []
    for node in paragraph.iter():
        if node.tag == _q(W, "t"):
            pieces.append(node.text or "")
        elif node.tag in (_q(W, "br"), _q(W, "cr"), _q(W, "tab")):
            pieces.append(" ")
    return _norm("".join(pieces))


@dataclass(frozen=True)
class TableShape:
    """Geometry and content of one table, ignoring styling."""

    grid_columns: int
    row_cell_counts: tuple[int, ...]
    cell_text: tuple[str, ...]
    nested_tables: int


@dataclass(frozen=True)
class DocxProjection:
    headings: tuple[tuple[str, str], ...]
    tables: tuple[TableShape, ...]
    image_count: int
    image_alt_text: tuple[str, ...]
    media_parts: tuple[str, ...]


def _table_shape(table: ElementTree.Element) -> TableShape:
    grid = table.find(_q(W, "tblGrid"))
    grid_columns = 0 if grid is None else len(grid.findall(_q(W, "gridCol")))

    row_cell_counts: list[int] = []
    cell_text: list[str] = []
    nested = 0
    # Direct children only: `.//w:tr` would also collect the rows of nested tables and
    # inflate the parent's row count.
    for row in table.findall(_q(W, "tr")):
        cells = row.findall(_q(W, "tc"))
        row_cell_counts.append(len(cells))
        for cell in cells:
            paragraphs = [_paragraph_text(p) for p in cell.findall(_q(W, "p"))]
            cell_text.append(_norm(" ".join(text for text in paragraphs if text)))
            nested += len(cell.findall(_q(W, "tbl")))
    return TableShape(grid_columns, tuple(row_cell_counts), tuple(cell_text), nested)


def project_docx(source: Path | str | bytes) -> DocxProjection:
    """Project a .docx given a path or its bytes."""
    handle: object = io.BytesIO(source) if isinstance(source, bytes) else str(source)

    with zipfile.ZipFile(handle) as archive:  # type: ignore[arg-type]
        media_parts = tuple(
            sorted(name for name in archive.namelist() if name.startswith(MEDIA_PREFIX))
        )
        root = ElementTree.fromstring(archive.read(DOCUMENT_PART))

    body = root.find(_q(W, "body"))
    if body is None:
        return DocxProjection((), (), 0, (), media_parts)

    headings: list[tuple[str, str]] = []
    tables: list[TableShape] = []
    for child in body:
        if child.tag == _q(W, "p"):
            style = _style(child)
            if style in TITLE_STYLES or style.startswith("heading"):
                headings.append((style, _paragraph_text(child)))
        elif child.tag == _q(W, "tbl"):
            tables.append(_table_shape(child))

    # Alt text lives on wp:docPr/@descr. python-docx exposes no accessor for it, hence the
    # direct XML read. `@name` is the fallback, because a picture added without alt text
    # still carries a generated name.
    alt_text: list[str] = []
    for properties in root.iter(_q(WP, "docPr")):
        alt_text.append(_norm(properties.get("descr") or properties.get("name") or ""))

    # Counted from drawing references rather than from word/media, because one media part
    # reused by two drawings is two images on the page.
    image_count = sum(1 for _ in root.iter(_q(A, "blip")))

    return DocxProjection(
        headings=tuple(headings),
        tables=tuple(tables),
        image_count=image_count,
        image_alt_text=tuple(alt_text),
        media_parts=media_parts,
    )


@dataclass
class ProjectionDiff:
    """`hard` differences fail a comparison; `soft` ones are reported and tolerated."""

    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.hard)

    def report(self) -> str:
        lines = [f"{len(self.hard)} structural difference(s):"]
        lines.extend(f"  ! {entry}" for entry in self.hard)
        if self.soft:
            lines.append(f"{len(self.soft)} tolerated difference(s):")
            lines.extend(f"  ~ {entry}" for entry in self.soft)
        return "\n".join(lines)


def compare_projections(
    expected: DocxProjection,
    actual: DocxProjection,
    *,
    cell_text_similarity: float = CELL_TEXT_SIMILARITY,
    max_examples: int = 10,
) -> ProjectionDiff:
    diff = ProjectionDiff()

    # ── headings ──────────────────────────────────────────────────────────────
    expected_styles = [style for style, _ in expected.headings]
    actual_styles = [style for style, _ in actual.headings]
    if expected_styles != actual_styles:
        diff.hard.append(
            f"heading style sequence: expected {expected_styles}, got {actual_styles}"
        )
    else:
        for index, ((style, want), (_, got)) in enumerate(
            zip(expected.headings, actual.headings)
        ):
            if want == got:
                continue
            entry = f"heading {index} ({style}): expected {want!r}, got {got!r}"
            (diff.soft if style in TITLE_STYLES else diff.hard).append(entry)

    # ── tables ────────────────────────────────────────────────────────────────
    if len(expected.tables) != len(actual.tables):
        diff.hard.append(
            f"table count: expected {len(expected.tables)}, got {len(actual.tables)}"
        )
    else:
        for index, (want, got) in enumerate(zip(expected.tables, actual.tables)):
            if want.grid_columns != got.grid_columns:
                diff.hard.append(
                    f"table {index} columns: expected {want.grid_columns}, "
                    f"got {got.grid_columns}"
                )
            if want.row_cell_counts != got.row_cell_counts:
                diff.hard.append(
                    f"table {index} rows: expected {len(want.row_cell_counts)} row(s), "
                    f"got {len(got.row_cell_counts)} row(s)"
                )
            if want.nested_tables != got.nested_tables:
                diff.hard.append(
                    f"table {index} nested tables: expected {want.nested_tables}, "
                    f"got {got.nested_tables}"
                )

    # ── cell text ─────────────────────────────────────────────────────────────
    want_cells = [text for shape in expected.tables for text in shape.cell_text]
    got_cells = [text for shape in actual.tables for text in shape.cell_text]
    ratio = SequenceMatcher(None, want_cells, got_cells).ratio() if want_cells else 1.0
    shown = 0
    for index, (want, got) in enumerate(zip(want_cells, got_cells)):
        if want == got or shown >= max_examples:
            continue
        diff.soft.append(f"cell {index}: expected {want!r}, got {got!r}")
        shown += 1
    if ratio < cell_text_similarity:
        diff.hard.append(
            f"cell text similarity {ratio:.4f} is below {cell_text_similarity} "
            f"({len(want_cells)} expected cells, {len(got_cells)} produced)"
        )

    # ── images ────────────────────────────────────────────────────────────────
    if expected.image_count != actual.image_count:
        diff.hard.append(
            f"image count: expected {expected.image_count}, got {actual.image_count}"
        )
    # Compared as a sorted multiset: alt text identifies which figure or table was
    # embedded, while the drawing order within a cell is not meaningful.
    want_alt, got_alt = sorted(expected.image_alt_text), sorted(actual.image_alt_text)
    if want_alt != got_alt:
        only_expected = [item for item in want_alt if item not in got_alt]
        only_actual = [item for item in got_alt if item not in want_alt]
        diff.hard.append(
            f"image alt text: missing {only_expected[:max_examples]}, "
            f"unexpected {only_actual[:max_examples]}"
        )

    return diff
