"""Caption search and Textract page analysis (`silos/iso/textract.py`).

Offline: `FakeTextract` returns hand-built `Blocks` arrays. Real Textract responses are
verbose, but only four block shapes matter — TABLE with CELL children, LAYOUT_TABLE and
LAYOUT_FIGURE — and each is a few lines.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "textract.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def iso():
    _load_module("iso", ISO_DIR / "silo.py")
    return {
        "textract": _load_module("iso", ISO_DIR / "textract.py", name="textract"),
        "patterns": _load_module("iso", ISO_DIR / "patterns.py", name="patterns"),
    }


def bbox(left, top, width, height):
    return {"Geometry": {"BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": height}}}


def table_with_cells(cells):
    """A TABLE block whose envelope is computed from its CELL children."""
    blocks = [
        {
            "Id": "t1",
            "BlockType": "TABLE",
            "Relationships": [{"Type": "CHILD", "Ids": [f"c{i}" for i in range(len(cells))]}],
        }
    ]
    for index, (left, top, width, height) in enumerate(cells):
        blocks.append({"Id": f"c{index}", "BlockType": "CELL", **bbox(left, top, width, height)})
    return {"Blocks": blocks}


def responder(payload):
    def call(png_bytes, feature_types=None):
        return payload

    return call


# ── caption search ────────────────────────────────────────────────────────────


def test_a_table_caption_is_found_above_the_table(iso):
    """ISO puts table captions above the table."""
    text = "Some intro prose\n\nTable E.1 — Correspondence\n\nrow data here"
    ref = iso["textract"]._find_caption_above(
        {0: text}, 0, 0.9, iso["patterns"]._TABLE_CAPTION_RE
    )
    assert ref == "E.1"


def test_a_figure_caption_is_found_below_the_figure(iso):
    """ISO puts figure captions below the figure."""
    text = "prose\n\nmore prose\n\nFigure 1 — Relationship of terms"
    ref = iso["textract"]._find_caption_below(
        {0: text}, 0, 0.1, iso["patterns"]._FIGURE_CAPTION_RE
    )
    assert ref == "1"


def test_a_caption_far_below_the_table_is_not_matched(iso):
    """The positional cutoff is the point: a later caption, or a body cross-reference,
    must not be attached to a table that sits above it.

    The cutoff is `int(y0_norm * n) + 2`, so there are always two blocks of slack for a
    caption sitting right at the boundary. This table is at the very top of the page and
    the caption is four blocks down, which is outside that slack.
    """
    text = "prose\n\nprose\n\nprose\n\nTable E.1 — the wrong one"
    ref = iso["textract"]._find_caption_above(
        {0: text}, 0, 0.0, iso["patterns"]._TABLE_CAPTION_RE
    )
    assert ref is None


def test_caption_search_on_an_unknown_page_returns_none(iso):
    assert (
        iso["textract"]._find_caption_above({}, 7, 0.5, iso["patterns"]._TABLE_CAPTION_RE)
        is None
    )
    assert (
        iso["textract"]._find_caption_below({0: ""}, 0, 0.5, iso["patterns"]._FIGURE_CAPTION_RE)
        is None
    )


# ── page analysis ─────────────────────────────────────────────────────────────


def test_a_table_envelope_is_computed_from_its_cells(iso):
    """Cell-level geometry is why TABLES is requested: it works on a continuation page
    with no header row and no borders."""
    payload = table_with_cells(
        [(0.10, 0.50, 0.20, 0.05), (0.30, 0.50, 0.20, 0.05), (0.10, 0.60, 0.40, 0.05)]
    )
    result = iso["textract"]._call_textract_page(b"png", responder(payload))

    assert result["figures"] == []
    assert len(result["tables"]) == 1
    x0, y0, x1, y1 = result["tables"][0]
    assert (round(x0, 2), round(y0, 2)) == (0.10, 0.50)
    assert (round(x1, 2), round(y1, 2)) == (0.50, 0.65)


def test_layout_figures_are_returned_as_normalised_boxes(iso):
    payload = {"Blocks": [{"Id": "f1", "BlockType": "LAYOUT_FIGURE", **bbox(0.2, 0.3, 0.5, 0.25)}]}
    result = iso["textract"]._call_textract_page(b"png", responder(payload))
    assert result["tables"] == []
    assert result["figures"] == [[0.2, 0.3, 0.7, 0.55]]


def test_layout_table_is_only_a_fallback(iso):
    """When TABLES found cells, LAYOUT_TABLE must not also be reported — that would
    double-count the same table."""
    payload = table_with_cells([(0.1, 0.5, 0.4, 0.1)])
    payload["Blocks"].append(
        {"Id": "lt", "BlockType": "LAYOUT_TABLE", **bbox(0.05, 0.45, 0.9, 0.3)}
    )

    result = iso["textract"]._call_textract_page(b"png", responder(payload))
    assert len(result["tables"]) == 1
    assert round(result["tables"][0][0], 2) == 0.10, "the cell envelope should win"


def test_layout_table_is_used_when_no_cells_were_found(iso):
    payload = {"Blocks": [{"Id": "lt", "BlockType": "LAYOUT_TABLE", **bbox(0.05, 0.45, 0.9, 0.3)}]}
    result = iso["textract"]._call_textract_page(b"png", responder(payload))
    # approx because the edges are Left + Width in floating point.
    assert result["tables"] == [pytest.approx([0.05, 0.45, 0.95, 0.75])]


def test_a_table_block_with_no_usable_cells_is_skipped(iso):
    payload = {
        "Blocks": [
            {"Id": "t1", "BlockType": "TABLE", "Relationships": [{"Type": "CHILD", "Ids": ["missing"]}]}
        ]
    }
    assert iso["textract"]._call_textract_page(b"png", responder(payload))["tables"] == []


def test_an_analysis_failure_degrades_to_empty_lists(iso):
    """The caller then falls back to the model's own bbox: worse crops, but a document."""

    def explode(png_bytes, feature_types=None):
        raise RuntimeError("throttled")

    assert iso["textract"]._call_textract_page(b"png", explode) == {
        "tables": [],
        "figures": [],
        "layout": [],
    }


def test_the_platform_default_feature_types_are_relied_on(iso):
    """The source asked for LAYOUT + TABLES; the platform's analyze_document already
    defaults to exactly that, so the silo must not pass its own list."""
    seen = {}

    def call(png_bytes, feature_types=None):
        seen["feature_types"] = feature_types
        return {"Blocks": []}

    iso["textract"]._call_textract_page(b"png", call)
    assert seen["feature_types"] is None


# ── matching the model's bbox to Textract's ───────────────────────────────────


def test_the_best_overlapping_box_wins(iso):
    near_miss = [0.80, 0.80, 0.95, 0.95]
    direct_hit = [0.10, 0.30, 0.60, 0.50]

    matched = iso["textract"]._best_textract_match(
        [0.11, 0.31, 0.59, 0.49], [near_miss, direct_hit], 595.0, 842.0
    )

    assert matched is not None
    # Returned in page points, not normalised fractions.
    assert round(matched.x0) == round(0.10 * 595)
    assert round(matched.y1) == round(0.50 * 842)


def test_a_poor_overlap_is_rejected_so_the_model_bbox_is_used(iso):
    """min_iou=0.10. Below it, None tells the caller to keep the model's coordinates."""
    assert (
        iso["textract"]._best_textract_match(
            [0.01, 0.01, 0.05, 0.05], [[0.80, 0.80, 0.95, 0.95]], 595.0, 842.0
        )
        is None
    )


def test_no_textract_boxes_means_no_match(iso):
    assert iso["textract"]._best_textract_match([0.1, 0.1, 0.5, 0.5], [], 595.0, 842.0) is None


def test_a_degenerate_model_bbox_is_rejected(iso):
    """A zero-area box would divide by zero when scoring."""
    assert (
        iso["textract"]._best_textract_match(
            [0.5, 0.5, 0.5, 0.5], [[0.1, 0.1, 0.9, 0.9]], 595.0, 842.0
        )
        is None
    )
