"""Rectangle maths and page-region detection (`silos/iso/geometry.py`).

`_extract_page_regions` decides what a figure is. Everything the ISO report shows for a
page follows from it: which rectangle becomes a PNG, which text is suppressed so it is
not printed twice, which caption names the file, and whether a detected table is dropped
because it is already visible inside a figure. That decision is made by roughly a dozen
numeric thresholds -- a 30pt clustering gap, a 0.01/0.85 area band, 0.05/0.20/0.30/0.60
overlap ratios, a 400pt sub-figure reach, an 8pt minimum height -- and a refactor can
nudge any of them without breaking anything a reader of the diff would notice. This file
pins each one from both sides.

Two deliberate choices about how these tests are built:

**The page is hand-built, never a synthetic PDF.** `_extract_page_regions` asks a page
for exactly six things: `rect`, `get_text("dict")`, `find_tables()`, `get_images(full=)`,
`get_image_rects(xref)` and `get_drawings()`. Feeding those directly is not a shortcut
around a real PDF; it is the only way to reach most of the branches. The clustering and
area gates are decided by drawing rectangles measured in fractions of a page, and
PyMuPDF will not lay out a page to order -- `page.draw_rect` gives you a rect *plus*
whatever stroke width, path bookkeeping and rounding it feels like, which is precisely
what the thresholds under test are sensitive to. Nor can a real PDF be made to raise from
`find_tables()` or `get_image_rects()`, both of which the source swallows on purpose.

**Every geometry number in this file is derived, not copied.** The page is 595x842, so
`HY` is 67.36, `FY` is 774.64 and the area band admits a cluster between 5010 and 425842
square points. Rects are chosen relative to those, and the assertions name the resulting
edge, so a test that fails tells you which gate moved.

Two suspected defects are characterised rather than fixed; both are commented at the
test that pins them.
"""

from __future__ import annotations

import fitz
import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "geometry.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def geometry():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "geometry.py", name="geometry")


# The page the whole file is measured against, and the constants that follow from it.
PAGE_W = 595.0
PAGE_H = 842.0
HY = PAGE_H * 0.08          # 67.36 -- header band, everything above it is page furniture
FY = PAGE_H * 0.92          # 774.64 -- footer band
PAREA = PAGE_W * PAGE_H     # 500990
MIN_CLUSTER_AREA = PAREA * 0.01   # 5009.9
MAX_CLUSTER_AREA = PAREA * 0.85   # 425841.5

# Imported by geometry from mediastore; restated here so a change there fails loudly.
TWO_LINE_GAP = 26
PROX = 60
CAP_PROX = 80


# ── test doubles ─────────────────────────────────────────────────────────────


def span(text, *, size=10.0, italic=False, bold=False, bbox=(0, 0, 0, 0)):
    """One fitz text span. fitz reports italic as `flags & 2` and bold as `flags & 16`."""
    return {
        "text": text,
        "size": size,
        "flags": (2 if italic else 0) | (16 if bold else 0),
        "bbox": bbox,
    }


def block(text, bbox, *, size=10.0, italic=False, bold=False, spans=None):
    """A `type == 0` text block holding one line."""
    return {
        "type": 0,
        "bbox": tuple(bbox),
        "lines": [
            {
                "bbox": tuple(bbox),
                "spans": spans
                or [span(text, size=size, italic=italic, bold=bold, bbox=tuple(bbox))],
            }
        ],
    }


def image_block(bbox):
    """A `type == 1` block: an inline image as `get_text("dict")` reports it."""
    return {"type": 1, "bbox": tuple(bbox)}


def drawing(bbox, *, items=None, fill=None):
    """One entry of `get_drawings()`. `items` carries the path ops; "c" means a curve."""
    return {"rect": tuple(bbox), "items": items or [("l", (0, 0), (1, 1))], "fill": fill}


def strokes(bbox, count=6, *, fill=None):
    """`count` touching paths spanning *bbox*, so they cluster into exactly it.

    Six is the `many_paths` threshold, so the default is a cluster that `_is_real_figure`
    accepts on path count alone.
    """
    x0, y0, x1, y1 = bbox
    step = (y1 - y0) / count
    out = []
    for index in range(count):
        top = y0 + index * step
        out.append(drawing((x0, top, x1, top + step), fill=fill))
    return out


class FakeTable:
    """`find_tables()` returns objects; only `.bbox` is read."""

    def __init__(self, bbox):
        self.bbox = tuple(bbox)


class FakePage:
    """A page answering only the six things `_extract_page_regions` asks of one."""

    def __init__(
        self,
        *,
        blocks=(),
        drawings=(),
        tables=(),
        images=(),
        image_rects=None,
        width=PAGE_W,
        height=PAGE_H,
        tables_raise=False,
        rects_raise=(),
    ):
        self._blocks = list(blocks)
        self._drawings = list(drawings)
        self._tables = list(tables)
        self._images = list(images)
        self._image_rects = dict(image_rects or {})
        self._tables_raise = tables_raise
        self._rects_raise = set(rects_raise)
        self.rect = fitz.Rect(0, 0, width, height)

    def get_text(self, kind):
        assert kind == "dict", f"unexpected get_text kind {kind!r}"
        return {"blocks": list(self._blocks)}

    def find_tables(self):
        if self._tables_raise:
            raise RuntimeError("find_tables cannot parse this page")
        return [FakeTable(b) for b in self._tables]

    def get_images(self, full=False):
        assert full, "the source always asks for full=True"
        return [(xref,) for xref in self._images]

    def get_image_rects(self, xref):
        if xref in self._rects_raise:
            raise RuntimeError("no rect for a broken xref")
        return [fitz.Rect(r) for r in self._image_rects.get(xref, [])]

    def get_drawings(self):
        return list(self._drawings)


def images_of(regions):
    return [r for r in regions if r["type"] == "image"]


def tables_of(regions):
    return [r for r in regions if r["type"] == "table"]


# ── _rect_area ───────────────────────────────────────────────────────────────


def test_rect_area_multiplies_the_two_side_lengths(geometry):
    assert geometry._rect_area(fitz.Rect(10, 20, 30, 70)) == pytest.approx(1000.0)


@pytest.mark.parametrize(
    "corners",
    [
        (30, 20, 10, 70),   # x1 behind x0
        (10, 70, 30, 20),   # y1 above y0
        (10, 20, 10, 70),   # zero width
    ],
)
def test_an_inverted_or_degenerate_rect_has_zero_area_not_a_negative_one(
    geometry, corners
):
    """A negative area would sail through every `area > 0` guard and then poison the
    ratios those guards protect, so the clamp is load-bearing rather than cosmetic."""
    assert geometry._rect_area(fitz.Rect(*corners)) == 0.0


# ── _intersect_area ──────────────────────────────────────────────────────────


def test_the_intersection_of_two_overlapping_rects_is_the_shared_area(geometry):
    left = fitz.Rect(0, 0, 100, 100)
    right = fitz.Rect(90, 50, 200, 200)
    assert geometry._intersect_area(left, right) == pytest.approx(10 * 50)


@pytest.mark.parametrize(
    "other",
    [
        (200, 0, 300, 100),   # clear to the right
        (0, 200, 100, 300),   # clear below
        (100, 0, 200, 100),   # edge to edge, touching but not overlapping
    ],
)
def test_rects_that_do_not_overlap_intersect_in_nothing(geometry, other):
    assert geometry._intersect_area(fitz.Rect(0, 0, 100, 100), fitz.Rect(*other)) == 0.0


# ── _overlaps_any ────────────────────────────────────────────────────────────


def test_a_zero_area_rect_is_reported_as_overlapping_everything(geometry):
    """The early return is a divide-by-zero guard, and it answers True: a rect with no
    area can never be a region, so claiming it is already covered discards it."""
    assert geometry._overlaps_any(fitz.Rect(10, 10, 10, 50), []) is True


@pytest.mark.parametrize(
    "height,expected",
    [
        (0.6, True),    # 6/100 of the rect's area, just over the 0.05 default
        (0.4, False),   # 0.04, just under
    ],
)
def test_the_default_overlap_threshold_sits_between_four_and_six_percent(
    geometry, height, expected
):
    rect = fitz.Rect(0, 0, 10, 10)
    regions = [{"bbox": [0, 0, 10, height]}]
    assert geometry._overlaps_any(rect, regions) is expected


def test_overlap_is_measured_against_every_region_not_only_the_first(geometry):
    rect = fitz.Rect(0, 0, 10, 10)
    regions = [{"bbox": [500, 500, 510, 510]}, {"bbox": [0, 0, 10, 8]}]
    assert geometry._overlaps_any(rect, regions, 0.30) is True


def test_nothing_overlaps_an_empty_region_list(geometry):
    assert geometry._overlaps_any(fitz.Rect(0, 0, 10, 10), []) is False


# ── _cluster_drawings ────────────────────────────────────────────────────────


def test_clustering_nothing_returns_nothing(geometry):
    assert geometry._cluster_drawings([]) == []


@pytest.mark.parametrize(
    "size,clusters",
    [
        (3.5, 1),   # 3.5 > 3 on both sides, so it survives
        (3.0, 0),   # exactly 3 is not > 3
        (2.0, 0),
    ],
)
def test_hairline_paths_are_dropped_before_clustering(geometry, size, clusters):
    """Rules, underlines and table borders arrive as paths a few points thick. Keeping
    them would join every unrelated diagram on the page into one cluster."""
    found = geometry._cluster_drawings([drawing((100, 100, 100 + size, 100 + size))])
    assert len(found) == clusters


@pytest.mark.parametrize(
    "second_x0,clusters",
    [
        (229.0, 1),   # 29pt clear of the first, inside the 30pt pad
        (231.0, 2),   # 31pt clear, outside it
    ],
)
def test_the_clustering_gap_joins_paths_within_thirty_points_and_no_further(
    geometry, second_x0, clusters
):
    found = geometry._cluster_drawings(
        [drawing((100, 100, 200, 200)), drawing((second_x0, 100, second_x0 + 100, 200))]
    )
    assert len(found) == clusters


def test_clustering_is_transitive_across_a_chain_of_paths(geometry):
    """The repeat-until-stable loop is the whole point: A near B and B near C must give
    one cluster even though A and C are 60pt apart."""
    found = geometry._cluster_drawings(
        [
            drawing((100, 100, 150, 150)),
            drawing((170, 100, 220, 150)),
            drawing((240, 100, 290, 150)),
        ],
        gap=30,
    )
    assert len(found) == 1
    rect, members = found[0]
    assert list(rect) == [100, 100, 290, 150]
    assert len(members) == 3, "every path must stay attached to its cluster"


def test_a_path_already_absorbed_is_not_offered_to_a_later_cluster(geometry):
    """Within one pass the first cluster may reach past its neighbours to a path further
    down the list. That path is spoken for, and offering it again would put the same
    drawing in two clusters and so the same artwork in two crops."""
    found = geometry._cluster_drawings(
        [
            drawing((0, 0, 50, 50)),      # reaches the fourth entry, not the second
            drawing((200, 0, 250, 50)),
            drawing((400, 0, 450, 50)),
            drawing((60, 0, 110, 50)),    # 10pt from the first, absorbed by it
        ],
        gap=30,
    )
    assert sorted(list(r) for r, _ in found) == [
        [0, 0, 110, 50],
        [200, 0, 250, 50],
        [400, 0, 450, 50],
    ]
    assert sum(len(members) for _, members in found) == 4, "no path is used twice"


def test_a_wider_gap_merges_clusters_a_narrower_one_leaves_apart(geometry):
    far = [drawing((100, 100, 150, 150)), drawing((300, 100, 350, 150))]
    assert len(geometry._cluster_drawings(far, gap=30)) == 2
    assert len(geometry._cluster_drawings(far, gap=200)) == 1


# ── _is_real_figure ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "x0,expected",
    [
        (PAGE_W * 0.83, False),   # a margin annotation, not a figure
        (PAGE_W * 0.81, True),
    ],
)
def test_a_cluster_starting_past_82_percent_of_the_width_is_margin_furniture(
    geometry, x0, expected
):
    bbox = fitz.Rect(x0, 100, x0 + 40, 200)
    paths = strokes(tuple(bbox), 8)
    assert geometry._is_real_figure(paths, bbox, [], PAGE_W) is expected


@pytest.mark.parametrize("count,expected", [(6, True), (5, False)])
def test_six_drawing_paths_are_a_figure_regardless_of_the_text_over_them(
    geometry, count, expected
):
    """`many_paths` deliberately runs before the text-coverage gate: a circuit diagram
    sits amongst body text, which would otherwise veto it. Five plain line paths with
    no curve and no fill therefore fail where six succeed."""
    bbox = fitz.Rect(100, 100, 300, 400)
    covering_text = [{"bbox": [100, 100, 300, 400]}]
    paths = strokes(tuple(bbox), count)
    assert geometry._is_real_figure(paths, bbox, covering_text, PAGE_W) is expected


@pytest.mark.parametrize(
    "text_height,expected",
    [
        (31.0, False),   # 31*200 / 60000 = 0.103 of the box, just over 0.10
        (29.0, True),    # 0.0967, just under
    ],
)
def test_text_covering_more_than_a_tenth_of_the_box_vetoes_a_curvy_figure(
    geometry, text_height, expected
):
    bbox = fitz.Rect(100, 100, 300, 400)
    curvy = [drawing((100, 100, 300, 400), items=[("c", (0, 0), (1, 1), (2, 2))])]
    free_text = [{"bbox": [100, 100, 300, 100 + text_height]}]
    assert geometry._is_real_figure(curvy, bbox, free_text, PAGE_W) is expected


@pytest.mark.parametrize(
    "paths,expected",
    [
        ([drawing((100, 100, 300, 400), items=[("c", (0, 0))])], True),   # a curve
        ([drawing((100, 100, 300, 400), fill=(0.5, 0.5, 0.5))], True),    # a grey fill
        ([drawing((100, 100, 300, 400), fill=(1, 1, 1))], False),         # white is paper
        ([drawing((100, 100, 300, 400), fill=[1, 1, 1])], False),         # as a list too
        ([drawing((100, 100, 300, 400))], False),                         # a bare line
    ],
)
def test_a_thin_cluster_needs_a_curve_or_a_non_white_fill_to_be_a_figure(
    geometry, paths, expected
):
    bbox = fitz.Rect(100, 100, 300, 400)
    assert geometry._is_real_figure(paths, bbox, [], PAGE_W) is expected


def test_hairline_paths_do_not_count_towards_the_six_path_threshold(geometry):
    """The substantial-path filter is applied again here, not inherited from the
    clusterer, so a cluster of one real path plus ten hairlines is still one path."""
    bbox = fitz.Rect(100, 100, 300, 400)
    paths = [drawing((100, 100, 300, 400))] + [
        drawing((100 + n, 100, 102 + n, 102)) for n in range(10)
    ]
    assert geometry._is_real_figure(paths, bbox, [], PAGE_W) is False


def test_a_zero_area_box_skips_the_text_coverage_check_entirely(geometry):
    """Guarding the division: with no area there is no ratio, so the decision falls
    through to the curve/fill evidence."""
    flat = fitz.Rect(100, 100, 300, 100)
    curvy = [drawing((100, 100, 300, 400), items=[("c", (0, 0))])]
    assert geometry._is_real_figure(curvy, flat, [{"bbox": [0, 0, PAGE_W, PAGE_H]}],
                                   PAGE_W) is True


# ── _group_text_blocks ───────────────────────────────────────────────────────


def test_grouping_no_text_blocks_returns_nothing(geometry):
    assert geometry._group_text_blocks([]) == []


@pytest.mark.parametrize(
    "second_y0,groups",
    [
        (100 + TWO_LINE_GAP, 1),       # exactly two lines' gap still belongs together
        (100 + TWO_LINE_GAP + 1, 2),   # one point more is a new paragraph
    ],
)
def test_the_two_line_gap_decides_where_one_text_group_ends(geometry, second_y0, groups):
    found = geometry._group_text_blocks(
        [
            {"bbox": [72, 50, 500, 100]},
            {"bbox": [72, second_y0, 500, second_y0 + 50]},
        ]
    )
    assert len(found) == groups


def test_a_text_group_reports_the_union_of_its_blocks_and_sorts_by_top_edge(geometry):
    """Input order must not matter: the source sorts by `bbox[1]` first, and a page's
    blocks arrive in reading order, not geometric order, whenever there are columns."""
    found = geometry._group_text_blocks(
        [
            {"bbox": [100, 120, 400, 140]},
            {"bbox": [72, 50, 300, 100]},
        ]
    )
    assert found == [{"type": "text", "bbox": [72, 50, 400, 140]}]


# ── _extract_page_regions: tables ────────────────────────────────────────────


def test_a_detected_table_claims_the_table_n_title_sitting_above_it(geometry):
    page = FakePage(
        tables=[(100, 200, 400, 300)],
        blocks=[
            block("Table 4 — Permitted limits", (100, 175, 400, 190), size=9.0),
            block("Body text " * 40, (72, 400, 520, 500)),
        ],
    )
    found = geometry._extract_page_regions(page)

    assert [r["type"] for r in found] == ["table"]
    # The title is inside the bbox, so the cropped table image shows its own number.
    assert found[0]["bbox"] == [100, 175, 400, 300]
    assert found[0]["caption"] == "Table 4 — Permitted limits"
    # Documented contract: caption_y0 exists only on merged figures.
    assert "caption_y0" not in found[0]


@pytest.mark.parametrize(
    "title_y1,claimed",
    [
        (165.0, True),    # 35pt above the table, inside the 40pt reach
        (155.0, False),   # 45pt above, out of reach
        (210.0, False),   # below the table top + 5, so it is not a title at all
    ],
)
def test_a_table_title_must_sit_within_forty_points_above_the_table(
    geometry, title_y1, claimed
):
    page = FakePage(
        tables=[(100, 200, 400, 300)],
        blocks=[block("Table 4 — Limits", (100, title_y1 - 15, 400, title_y1), size=9.0)],
    )
    found = geometry._extract_page_regions(page)
    assert (found[0]["caption"] == "Table 4 — Limits") is claimed
    assert (found[0]["bbox"][1] < 200) is claimed


def test_only_the_first_table_may_claim_a_shared_title(geometry):
    """Two `find_tables` hits for one printed table -- common when a table has a header
    band -- must not both absorb the caption, and the second must lose to the first."""
    page = FakePage(
        tables=[(100, 200, 400, 300), (100, 210, 400, 310)],
        blocks=[block("Table 7 — Coefficients", (100, 175, 400, 190), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1, "the second, 90%-overlapping table is a duplicate"
    assert found[0]["caption"] == "Table 7 — Coefficients"


@pytest.mark.parametrize("bbox", [(100, 200, 118, 300), (100, 200, 400, 218)])
def test_a_table_under_twenty_points_on_either_side_is_not_a_table(geometry, bbox):
    """A single ruled line is reported by `find_tables` as a table of no height."""
    assert geometry._extract_page_regions(FakePage(tables=[bbox])) == []


def test_a_page_whose_table_finder_raises_still_yields_its_figures(geometry):
    """`find_tables` is wrapped because it walks its own path parser and throws on real
    documents. Losing the tables on such a page is acceptable; losing the page is not."""
    page = FakePage(
        tables_raise=True,
        drawings=strokes((100, 150, 300, 400)),
        blocks=[block("Figure 2 — Overview", (100, 410, 400, 424), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert [r["type"] for r in found] == ["image"]
    assert found[0]["caption"] == "Figure 2 — Overview"


# ── _extract_page_regions: figure seeds ──────────────────────────────────────


def test_a_captioned_vector_figure_reports_its_caption_and_its_crop_line(geometry):
    """The whole contract in one page: the cluster is the bbox, the "Figure N" block
    below it is the caption, and `caption_y0` tells the cropper where to stop so the
    caption is not baked into the PNG *and* printed underneath it."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("Figure 3 — A block diagram", (100, 410, 400, 424), size=9.0),
            block("Body prose " * 30, (72, 600, 520, 700)),
        ],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    region = found[0]
    assert region["type"] == "image"
    assert region["bbox"] == [100, 150, 400, 424]
    assert region["caption"] == "Figure 3 — A block diagram"
    assert region["caption_y0"] == 410
    # Prose 200pt below the caption is body text and stays in the document.
    assert region["bbox"][3] < 600


def test_a_caption_printed_above_the_figure_pulls_the_bbox_up_but_sets_no_crop_line(
    geometry
):
    """`caption_y0` means "crop above this"; a caption already above the artwork would
    crop the figure away, so it is deliberately left unset."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[block("Figure 4 — Above its artwork", (100, 80, 400, 94), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert found[0]["bbox"] == [100, 80, 400, 400]
    assert found[0]["caption"] == "Figure 4 — Above its artwork"
    assert found[0]["caption_y0"] is None


def test_a_smaller_font_below_a_figure_is_taken_as_its_caption_without_a_figure_label(
    geometry
):
    """ISO prints plenty of unlabelled figure legends. Anything smaller than body text
    by more than 0.3pt, or italic, is treated as caption style -- but it never sets
    `caption_y0`, because it is not reliable enough to crop against."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("Key 1 resistor 2 capacitor", (100, 410, 400, 424), size=8.0),
            block("Body prose " * 40, (72, 600, 520, 700), size=10.0),
        ],
    )
    found = geometry._extract_page_regions(page)

    assert found[0]["caption"] == "Key 1 resistor 2 capacitor"
    assert found[0]["caption_y0"] is None


@pytest.mark.parametrize(
    "size,italic,captioned",
    [
        (9.6, False, True),    # 0.4pt smaller than body: caption style
        (9.7, False, False),   # exactly 0.3pt smaller is not smaller enough
        (10.0, True, True),    # same size but italic
        (10.0, False, False),  # indistinguishable from body text
    ],
)
def test_caption_style_needs_a_third_of_a_point_of_shrink_or_an_italic(
    geometry, size, italic, captioned
):
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("An unlabelled legend", (100, 410, 400, 424), size=size, italic=italic),
            block("Body prose " * 60, (72, 600, 520, 700), size=10.0),
        ],
    )
    found = geometry._extract_page_regions(page)
    assert (found[0]["caption"] is not None) is captioned


def test_body_text_above_the_seed_is_not_swallowed_by_the_figure(geometry):
    """Absorbing the paragraph above a diagram cascades: the grown bbox then reaches the
    paragraph above that, and eventually merges two separate figures into one."""
    page = FakePage(
        drawings=strokes((100, 300, 300, 500)),
        blocks=[
            block("Body prose " * 40, (72, 200, 520, 280), size=10.0),
            block("Figure 6 — Below", (100, 510, 400, 524), size=9.0),
        ],
    )
    found = geometry._extract_page_regions(page)

    assert found[0]["bbox"][1] == 300, "the seed top must hold against body text"
    assert found[0]["caption"] == "Figure 6 — Below"


def test_a_page_header_is_never_absorbed_into_a_figure(geometry):
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("ISO 20417:2021(E)", (72, 40, 520, 55), size=8.0),
            block("Figure 7 — Detail", (100, 410, 400, 424), size=9.0),
        ],
    )
    found = geometry._extract_page_regions(page)

    assert found[0]["bbox"][1] == 150
    assert found[0]["caption"] == "Figure 7 — Detail"


def test_text_more_than_five_points_below_the_caption_stops_the_figure_growing(geometry):
    """Once a "Figure N" block is found, the caption bottom is a hard floor: the next
    paragraph would otherwise be absorbed and then suppressed from the report."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("Figure 8 — Detail", (100, 410, 400, 424), size=9.0),
            # 6pt below the caption bottom, well inside the 60pt growth pad.
            block("The next paragraph " * 5, (100, 430, 400, 460), size=10.0),
        ],
    )
    found = geometry._extract_page_regions(page)
    assert found[0]["bbox"][3] == 424


@pytest.mark.parametrize(
    "bbox,found_figure",
    [
        # Area as a fraction of the 500990pt page; the lower gate is 0.01.
        ((100, 150, 200, 210), True),    # 6000/500990 = 0.0120 -> just inside
        ((100, 150, 200, 200), False),   # 5000 = 0.00998 -> just outside
    ],
)
def test_a_cluster_smaller_than_one_percent_of_the_page_is_a_glyph_not_a_figure(
    geometry, bbox, found_figure
):
    """Below 1% is a bullet, a logo or a tick mark, and cropping it wastes a figure slot
    in the report as well as a Textract call."""
    page = FakePage(drawings=strokes(bbox))
    assert bool(images_of(geometry._extract_page_regions(page))) is found_figure


def test_a_cluster_covering_the_whole_page_is_still_accepted_as_a_figure(geometry):
    """SUSPECTED DEFECT geometry.py:247 -- the upper half of the area band, `af < 0.85`,
    is unreachable. `af` is computed from `u_vis`, the cluster clipped to the band between
    HY and FY, and that band is only 84% of the page height, so the largest possible `af`
    is 595*707.28/500990 = 0.840. A background wash or a full-page border -- the thing the
    gate names -- therefore passes. Correct behaviour would be to measure the ratio
    against the visible band's area rather than the whole page, or to lower the ceiling.
    Pinned as-is because `prescan_media` currently relies on full-bleed diagrams being
    croppable, so changing it is a behaviour change, not a fix.
    """
    page = FakePage(drawings=strokes((1, -20, 594, PAGE_H + 20)))
    found = images_of(geometry._extract_page_regions(page))

    assert len(found) == 1
    assert found[0]["bbox"] == [1, pytest.approx(HY), 594, pytest.approx(FY)]


@pytest.mark.parametrize(
    "bbox",
    [
        (100, 10, 300, HY - 1),        # entirely in the header band
        (100, FY + 1, 300, PAGE_H),    # entirely in the footer band
    ],
)
def test_artwork_confined_to_the_header_or_footer_band_is_page_furniture(geometry, bbox):
    """Rules, logos and the standard's own footer marks live there on every page."""
    assert geometry._extract_page_regions(FakePage(drawings=strokes(bbox))) == []


@pytest.mark.parametrize(
    "y1,found_figure",
    [
        (HY + 9.0, True),    # 9pt of the cluster is on the page
        (HY + 7.0, False),   # 7pt is under the 8pt minimum visible height
    ],
)
def test_a_cluster_is_clipped_to_the_visible_page_before_its_height_is_judged(
    geometry, y1, found_figure
):
    """Real drawing paths run off the top of the page. Judging the raw cluster would
    call an 8pt sliver a figure because its unclipped bbox looks tall."""
    page = FakePage(drawings=strokes((10, -400, 585, y1)))
    assert bool(images_of(geometry._extract_page_regions(page))) is found_figure


def test_a_cluster_sitting_on_a_detected_table_is_the_tables_own_ruling(geometry):
    """A ruled table is a dense pile of paths. Seeding a figure from it would emit the
    table twice, once as a crop and once as rows."""
    page = FakePage(
        tables=[(100, 200, 400, 400)],
        drawings=strokes((110, 210, 390, 390)),
    )
    found = geometry._extract_page_regions(page)
    assert [r["type"] for r in found] == ["table"]


def test_a_cluster_with_no_curve_no_fill_and_few_paths_is_not_seeded(geometry):
    page = FakePage(drawings=[drawing((100, 150, 300, 250)), drawing((100, 255, 300, 350))])
    assert geometry._extract_page_regions(page) == []


def test_a_raster_image_seeds_a_figure_and_a_broken_xref_is_skipped(geometry):
    """`get_image_rects` raises on a damaged xref; the source swallows that per image so
    one bad object does not cost the page its other figures."""
    page = FakePage(
        images=[7, 8, 9],
        image_rects={
            7: [(100, 150, 400, 400)],
            8: [(100, 500, 105, 505)],   # 5pt: a spacer gif, not a figure
            9: [(100, 600, 400, 700)],
        },
        rects_raise=[9],
        blocks=[block("Figure 9 — A photograph", (100, 410, 400, 424), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(page))

    assert len(found) == 1
    assert found[0]["bbox"] == [100, 150, 400, 424]
    assert found[0]["caption"] == "Figure 9 — A photograph"


def test_an_inline_image_block_seeds_a_figure_and_a_tiny_one_does_not(geometry):
    page = FakePage(
        blocks=[
            image_block((100, 500, 400, 700)),
            image_block((100, 100, 108, 108)),   # 8pt, under the 10pt minimum
        ]
    )
    found = images_of(geometry._extract_page_regions(page))
    assert [r["bbox"] for r in found] == [[100, 500, 400, 700]]


def test_a_raster_and_an_inline_block_at_the_same_place_seed_only_one_figure(geometry):
    """The same drawn image is reported by both `get_images` and `get_text("dict")`;
    the rounded-corner key is what stops it becoming two overlapping crops."""
    page = FakePage(
        images=[7],
        image_rects={7: [(100.2, 150.4, 400.1, 400.3)]},
        blocks=[image_block((100, 150, 400, 400))],
    )
    assert len(images_of(geometry._extract_page_regions(page))) == 1


# ── _extract_page_regions: text bookkeeping ──────────────────────────────────


def test_a_page_with_no_text_at_all_still_yields_its_figure(geometry):
    """With no spans there is no dominant body size, so the 10pt default decides caption
    style for the whole page. A full-page diagram is exactly that page."""
    page = FakePage(drawings=strokes((100, 150, 300, 400)))
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    assert found[0]["caption"] is None


def test_whitespace_only_blocks_and_spans_do_not_vote_on_the_body_font_size(geometry):
    """A blank block with a large font would otherwise become the page's body size and
    turn every real caption into body text."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("   ", (72, 60, 520, 75), size=30.0),
            block(
                "ignored",
                (72, 600, 520, 700),
                spans=[
                    span("   ", size=30.0, bbox=(72, 600, 520, 620)),
                    span("Body prose " * 40, size=10.0, bbox=(72, 620, 520, 700)),
                ],
            ),
            block("A legend at nine point", (100, 410, 400, 424), size=9.0),
        ],
    )
    found = geometry._extract_page_regions(page)
    assert found[0]["caption"] == "A legend at nine point"


# ── _extract_page_regions: merge and the second caption sweep ────────────────


def test_two_clusters_within_fifty_points_become_one_figure_sharing_a_caption(geometry):
    """A diagram whose halves cluster separately is still one figure, and the caption
    found for either half names the merged whole."""
    page = FakePage(
        drawings=strokes((100, 150, 300, 300)) + strokes((100, 330, 300, 480)),
        blocks=[block("Figure 11 — Both halves", (100, 490, 400, 504), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    assert found[0]["bbox"] == [100, 150, 400, 504]
    assert found[0]["caption"] == "Figure 11 — Both halves"


def test_the_second_sweep_recovers_a_caption_the_growth_pass_had_to_refuse(geometry):
    """The growth pass rejects everything above the header band, which loses the caption
    of a figure printed high on the page. The sweep has no such rule, so it picks it up
    and the figure is named after all."""
    page = FakePage(
        drawings=strokes((100, 100, 300, 350)),
        blocks=[block("Figure 12 — High on the page", (100, 40, 400, 54), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    assert found[0]["caption"] == "Figure 12 — High on the page"
    assert found[0]["bbox"][1] == 40


def test_an_uncaptioned_sub_figure_is_absorbed_into_the_captioned_one_below_it(geometry):
    """Sub-diagrams (a) and (b) cluster separately and only the lower one is near the
    "Figure N" caption. Without the union the upper half is emitted as an unnamed orphan
    image, or dropped.

    SUSPECTED DEFECT geometry.py:373/376 -- the absorbed entry is marked with
    `mc["_merged_into"] = _j`, then filtered by `if not m.get("_merged_into")`. `_j` is
    an index, and the captioned parent is almost always index 0 because `merged` is
    sorted by descending area, so the marker is falsy and the absorbed entry survives
    the filter. Correct behaviour would be a sentinel that is always truthy (e.g. `True`
    or `_j + 1`). It is masked here only by the 0.20 overlap gate at line 379, which
    rejects the survivor for overlapping the parent it was just unioned into -- so the
    observable output is right today and would break the moment that gate changed.
    """
    page = FakePage(
        drawings=strokes((100, 100, 300, 250)) + strokes((120, 400, 320, 550)),
        blocks=[block("Figure 13 — Two views", (120, 560, 400, 574), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1, "the upper sub-figure must not also be emitted"
    assert found[0]["bbox"] == [100, 100, 400, 574]
    assert found[0]["caption"] == "Figure 13 — Two views"
    assert found[0]["caption_y0"] == 560


@pytest.mark.parametrize(
    "upper_bbox,merged",
    [
        ((120, 100, 320, 250), True),   # x-ranges overlap by 180pt
        ((10, 100, 90, 250), False),    # a different column, 30pt clear in x
    ],
)
def test_the_sub_figure_union_needs_the_two_halves_to_overlap_in_x(
    geometry, upper_bbox, merged
):
    page = FakePage(
        drawings=strokes(upper_bbox) + strokes((120, 550, 320, 700)),
        blocks=[block("Figure 14 — Lower", (120, 710, 400, 724), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(page))
    assert (len(found) == 1) is merged


def test_the_sub_figure_union_reaches_any_distance_upward_but_none_downward(geometry):
    """SUSPECTED DEFECT geometry.py:369 -- the comment above it says the captioned entry
    must have its "top edge within 400 px below the uncaptioned entry's bottom", but the
    test written is `mc.y1 <= mp.y0 + 400`, which is satisfied by *any* uncaptioned entry
    above the captioned one, however far above. On a page with an unrelated uncaptioned
    diagram at the top and "Figure N" 600pt lower, the two are unioned into one crop.
    Correct behaviour for the comment would be `mp.y0 - mc.y1 <= 400`. The asymmetry is
    real and observable, so it is pinned rather than fixed.
    """
    far_above = FakePage(
        # 300pt of clear page between the two, far more than the 400pt the comment allows
        # once measured as a gap.
        drawings=strokes((120, 80, 320, 180)) + strokes((120, 480, 320, 620)),
        blocks=[block("Figure 15 — Lower", (120, 630, 400, 644), size=9.0)],
    )
    assert len(images_of(geometry._extract_page_regions(far_above))) == 1

    below = FakePage(
        drawings=strokes((120, 100, 320, 250)) + strokes((120, 690, 320, 760)),
        blocks=[block("Figure 16 — Upper", (120, 260, 400, 274), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(below))
    assert len(found) == 2, "an uncaptioned diagram below the caption is left alone"


def test_an_uncaptioned_neighbour_is_not_absorbed_by_a_caption_without_a_number(
    geometry
):
    """The union only fires for a real "Figure N". A caption-styled legend is too weak
    a signal to justify swallowing a separate diagram."""
    page = FakePage(
        drawings=strokes((120, 100, 320, 250)) + strokes((120, 400, 320, 550)),
        blocks=[
            block("Dimensions in millimetres", (120, 560, 400, 574), size=8.0),
            # Body prose is what makes 8pt count as caption style at all.
            block("Body prose " * 40, (72, 700, 520, 760), size=10.0),
        ],
    )
    found = images_of(geometry._extract_page_regions(page))

    assert len(found) == 2
    assert found[0]["caption"] == "Dimensions in millimetres"
    assert found[1]["caption"] is None


def test_two_uncaptioned_diagrams_on_one_page_stay_two_regions(geometry):
    """Neither can donate a caption to the other, so the sub-figure union has nothing to
    do and both are emitted -- which is what `prescan_media` needs in order to name them
    by page and index instead."""
    page = FakePage(drawings=strokes((100, 100, 300, 250)) + strokes((100, 400, 300, 550)))
    found = geometry._extract_page_regions(page)

    # Equal areas, so the largest-first sort is stable and page order is preserved.
    assert [r["bbox"] for r in found] == [[100, 100, 300, 250], [100, 400, 300, 550]]
    assert all(r["caption"] is None for r in found)


def test_a_larger_uncaptioned_cluster_takes_the_caption_of_the_smaller_one_it_absorbs(
    geometry
):
    """Merging walks largest-first, so the entry that keeps its identity is usually the
    one *without* the caption -- the caption belongs to the small sub-diagram beneath it.
    Both the text and the crop line have to travel with it, or the figure is emitted
    unnamed and with its caption baked into the PNG."""
    page = FakePage(
        drawings=strokes((100, 100, 400, 400)) + strokes((100, 440, 200, 510)),
        blocks=[block("Figure 31 — The whole assembly", (100, 520, 300, 534), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    assert found[0]["bbox"] == [100, 100, 400, 534]
    assert found[0]["caption"] == "Figure 31 — The whole assembly"
    assert found[0]["caption_y0"] == 520


def test_a_table_cells_own_text_never_grows_the_figure_beside_it(geometry):
    """Text inside a claimed table is that table's content. Absorbing it would both
    stretch the figure crop over the table and mark the rows as already-emitted, so the
    table would vanish from the report."""
    page = FakePage(
        tables=[(100, 400, 400, 500)],
        drawings=strokes((100, 150, 300, 360)),
        blocks=[block("1,5 mm 2,0 mm 2,5 mm", (110, 410, 390, 490), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert [r["type"] for r in found] == ["table", "image"]
    assert found[1]["bbox"] == [100, 150, 300, 360]


# ── _extract_page_regions: the orphaned-caption fallback ─────────────────────


def test_a_caption_with_only_scattered_strokes_above_it_recovers_a_page_strip(geometry):
    """Circuit diagrams are hundreds of isolated thin strokes that never cluster into
    anything big enough to pass the area band. The caption proves a figure is there, so
    the strip above it is captured instead of losing the figure altogether."""
    page = FakePage(
        drawings=[drawing((120, 300, 170, 350))],
        blocks=[block("Figure 15 — A circuit", (100, 500, 400, 514), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(found) == 1
    # From the header band down to 2pt above the caption, and 75% of the width.
    assert found[0]["bbox"] == [0, pytest.approx(HY), pytest.approx(PAGE_W * 0.75), 498]
    assert found[0]["caption"] == "Figure 15 — A circuit"
    # Documented contract: the fallback sets no crop line.
    assert "caption_y0" not in found[0]


def test_the_orphan_strip_starts_at_the_previous_figures_tall_strokes_not_its_bottom(
    geometry
):
    """Two figures on one page share a drawing area. Starting the second strip at the
    first figure's bbox bottom cuts the top off the second circuit, so the strip is
    extended up to any path taller than 50pt within 250pt above that boundary."""
    page = FakePage(
        images=[7],
        image_rects={7: [(100, 100, 350, 300)]},
        drawings=[drawing((150, 180, 200, 290))],   # 110pt tall, under the raster
        blocks=[block("Figure 17 — The second circuit", (100, 500, 400, 514), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(page))

    assert len(found) == 2
    assert found[0]["bbox"] == [100, 100, 350, 300]
    assert found[1]["bbox"] == [100, 175, 350, 498], "strip top = tall stroke top - 5"
    assert found[1]["caption"] == "Figure 17 — The second circuit"


def test_with_no_tall_strokes_the_orphan_strip_starts_just_below_the_previous_figure(
    geometry
):
    page = FakePage(
        images=[7],
        image_rects={7: [(100, 100, 350, 300)]},
        drawings=[
            drawing((150, 200, 200, 240)),   # only 40pt tall, so it does not extend up
            drawing((150, 350, 200, 400)),   # proves the strip is not empty
        ],
        blocks=[block("Figure 18 — Below", (100, 500, 400, 514), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(page))

    assert len(found) == 2
    assert found[1]["bbox"] == [100, 302, 350, 498], "strip top = previous bottom + 2"


@pytest.mark.parametrize(
    "tall_height,expected_top",
    [
        (51.0, 175.0),   # taller than 50pt: the strip is pulled up to its top - 5
        (50.0, 302.0),   # exactly 50 is not tall enough
    ],
)
def test_only_strokes_over_fifty_points_tall_pull_the_orphan_strip_upward(
    geometry, tall_height, expected_top
):
    page = FakePage(
        images=[7],
        image_rects={7: [(100, 100, 350, 300)]},
        drawings=[
            drawing((150, 180, 200, 180 + tall_height)),
            drawing((150, 350, 200, 400)),   # keeps the strip non-empty either way
        ],
        blocks=[block("Figure 19 — Below", (100, 500, 400, 514), size=9.0)],
    )
    found = images_of(geometry._extract_page_regions(page))
    assert found[1]["bbox"][1] == pytest.approx(expected_top)


def test_a_caption_already_inside_a_claimed_figure_does_not_start_a_second_one(geometry):
    page = FakePage(
        drawings=strokes((100, 150, 300, 400)),
        blocks=[block("Figure 20 — Already claimed", (100, 410, 400, 424), size=9.0)],
    )
    assert len(images_of(geometry._extract_page_regions(page))) == 1


@pytest.mark.parametrize(
    "caption_y0,recovered",
    [
        (99.0, False),    # strip is 67.36..97, i.e. 29.64pt -- just under 30
        (100.0, True),    # 30.64pt -- just over
    ],
)
def test_an_orphan_strip_thinner_than_thirty_points_is_not_worth_cropping(
    geometry, caption_y0, recovered
):
    """A caption printed a line below the header band leaves no room for artwork, and a
    30pt-tall crop in the report is a smear rather than a figure."""
    page = FakePage(
        drawings=[drawing((120, 70, 170, 95))],
        blocks=[block("Figure 21 — High", (100, caption_y0, 400, caption_y0 + 14),
                      size=9.0)],
    )
    assert bool(images_of(geometry._extract_page_regions(page))) is recovered


def test_a_caption_with_no_vector_content_above_it_recovers_nothing(geometry):
    """Without a single path there is nothing to crop, and a blank strip in the report
    is worse than a missing figure."""
    page = FakePage(
        blocks=[block("Figure 22 — Nothing here", (100, 500, 400, 514), size=9.0)]
    )
    assert geometry._extract_page_regions(page) == []


# ── _extract_page_regions: tables inside figures ─────────────────────────────


@pytest.mark.parametrize(
    "table_bbox,kept",
    [
        ((100, 200, 400, 300), False),   # wholly inside the recovered strip
        ((100, 600, 400, 700), True),    # below it, a table in its own right
    ],
)
def test_a_table_swallowed_by_a_figure_crop_is_not_also_emitted_as_rows(
    geometry, table_bbox, kept
):
    """The crop already shows the table, so keeping the row version prints it twice."""
    page = FakePage(
        tables=[table_bbox],
        drawings=[drawing((120, 350, 170, 400))],
        blocks=[block("Figure 23 — Contains a table", (100, 500, 400, 514), size=9.0)],
    )
    found = geometry._extract_page_regions(page)

    assert len(images_of(found)) == 1
    assert bool(tables_of(found)) is kept


@pytest.mark.parametrize(
    "table_bbox,kept",
    [
        # The recovered strip is 0..446.25 x 67.36..498. Overlap as a fraction of the
        # table's own area decides, and the gate is 0.60.
        ((100, 400, 400, 465), False),   # 98/165 = 0.594 outside -> 0.606 inside
        ((100, 400, 400, 655), True),    # 98/255 = 0.384 inside, so it survives
    ],
)
def test_the_table_removal_gate_needs_more_than_sixty_percent_containment(
    geometry, table_bbox, kept
):
    page = FakePage(
        tables=[table_bbox],
        drawings=[drawing((120, 200, 170, 250))],
        blocks=[block("Figure 24 — Overlaps a table", (100, 500, 400, 514), size=9.0)],
    )
    found = geometry._extract_page_regions(page)
    assert bool(tables_of(found)) is kept


def test_only_images_and_tables_are_returned(geometry):
    """`get_regions` in the source also returned plain text regions; this port does not,
    and `prescan_media` iterates the result assuming only the two kinds."""
    page = FakePage(
        tables=[(100, 600, 400, 700)],
        drawings=strokes((100, 150, 300, 400)),
        blocks=[
            block("Figure 25 — Both kinds", (100, 410, 400, 424), size=9.0),
            block("Body prose " * 40, (72, 720, 520, 760), size=10.0),
        ],
    )
    found = geometry._extract_page_regions(page)
    assert sorted(r["type"] for r in found) == ["image", "table"]
    assert all(set(r) >= {"type", "bbox", "caption"} for r in found)
