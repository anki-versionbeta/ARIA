"""Per-section body extraction (`silos/iso/body.py`).

`_extract_body` is one 300-line loop and it was at 0% coverage, which is the worst
possible state for it: every one of its filters exists because something real leaked into
a shipped report, and none of those filters is observable from the outside. A refactor
could delete the running-header check, or invert the `> 0.5` overlap test, and every other
test in the suite would still pass while the DOCX quietly grew a column of page numbers.

So the tests here are organised as one per *reason to drop a block* and one per *reason to
emit a row*, and each asserts on the returned rows rather than on a log line, because the
rows are what the DOCX is built from.

Two deliberate construction choices:

**Pages are hand-built, not rendered.** `_extract_body` asks a page for exactly two
things: `get_text("blocks", sort=True)` and `rect.height`. Feeding those directly is not a
shortcut around a real PDF — it is the only way to place a block at a chosen coordinate,
which is the whole input to the header/footer band and the three rectangle-overlap
suppressions. A synthetic PDF would also silently rewrite the text: fitz's base-14 fonts
cannot encode the math and Greek glyphs these documents are full of, so they arrive as
U+FFFD and the wrong branch gets exercised.

**Media is raw bytes, not files.** `_media_as_bytes` accepts either, and bytes keep the
tests off the filesystem, where `prescan_media` would otherwise have to have run first.
"""

from __future__ import annotations

import fitz
import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "body.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def body():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "body.py", name="body")


# ── test doubles ─────────────────────────────────────────────────────────────


def text_block(text, y0=100.0, y1=140.0, *, x0=72.0, x1=520.0, btype=0, number=0):
    """A `get_text("blocks")` tuple: (x0, y0, x1, y1, text, block_no, block_type).

    The default y band sits clear of the 65pt header and footer margins, so a block only
    lands in those bands when a test asks it to.
    """
    return (x0, y0, x1, y1, text, number, btype)


class FakePage:
    """A page that answers only the two questions `_extract_body` asks of one."""

    def __init__(self, blocks=(), width=595.0, height=842.0):
        self._blocks = list(blocks)
        self.rect = fitz.Rect(0, 0, width, height)

    def get_text(self, kind, sort=False):
        if kind == "blocks":
            # `sort=True` is what the source passes; assert it so a future edit that
            # drops it (and with it the reading order of every row) is caught here.
            assert sort is True
            return list(self._blocks)
        raise AssertionError(f"unexpected get_text kind {kind!r}")


class FakeDoc:
    """`_extract_body` only ever indexes a doc and takes its length."""

    def __init__(self, pages):
        self._pages = list(pages)

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, index):
        return self._pages[index]


SECTION = {"title": "7.1 General requirements", "level": 2}
HEADING = "7.1 General requirements"


def run(body, pages, *, section=SECTION, base_num="7", start_0=0, end_0=None, **kwargs):
    """Call `_extract_body` over hand-built pages with the media dicts defaulted empty.

    `pages` is a list of block-lists (fitz path) — one entry per page.
    """
    doc = FakeDoc([FakePage(blocks) for blocks in pages])
    return body._extract_body(
        doc,
        section,
        start_0,
        len(pages) - 1 if end_0 is None else end_0,
        base_num,
        kwargs.pop("figures", {}),
        kwargs.pop("tables", {}),
        kwargs.pop("formulas", {}),
        **kwargs,
    )


def formula(index=0, *, page_0=0, anchor="", bbox=(72, 700, 520, 730), image=b"PNG",
            eqn_number="", latex=""):
    """One `find_unnumbered_formulas` entry, as `(global_index, dict)`.

    The default bbox sits below the default text band so a formula does not accidentally
    suppress the very block it is supposed to be anchored to.
    """
    return (
        index,
        {
            "page_0": page_0,
            "bbox": bbox,
            "image": image,
            "anchor_text": anchor,
            "eqn_number": eqn_number,
            "latex": latex,
        },
    )


def texts(rows):
    return [row["text"] for row in rows]


# ── the empty and defaulted cases ────────────────────────────────────────────


def test_a_document_with_no_pages_in_range_yields_no_rows(body):
    """Guards the whole loop; `end_0` is clamped by `len(doc)`, not trusted."""
    assert run(body, [[]]) == []


def test_every_optional_collection_argument_defaults_to_empty(body):
    """Callers in `rows.py` omit most of these, so the defaults are the live path."""
    doc = FakeDoc([FakePage([text_block(HEADING), text_block("Body prose here.")])])
    rows = body._extract_body(doc, SECTION, 0, 0, "7", {}, {}, {})
    assert texts(rows) == ["Body prose here."]


def test_an_end_page_past_the_document_end_is_clamped(body):
    """`end_0` comes from the ToC, which can name a page the PDF does not have."""
    rows = run(body, [[text_block(HEADING), text_block("Body prose here.")]], end_0=99)
    assert texts(rows) == ["Body prose here."]


# ── finding, and then skipping, the section's own heading ────────────────────


def test_the_heading_is_recognised_by_its_section_number_and_dropped(body):
    """The number is the reliable half: vision output often mangles the title text."""
    rows = run(body, [[text_block("7.1 something else entirely"), text_block("Prose.")]])
    assert texts(rows) == ["Prose."]


def test_the_heading_is_also_recognised_by_its_title_appearing_anywhere_in_a_block(body):
    """PDFs that store the number and title as one block behind some leading text, which
    stops the anchored number regex from matching."""
    rows = run(body, [[text_block("Clause " + HEADING), text_block("Prose.")]])
    assert texts(rows) == ["Prose."]


def test_blocks_before_the_heading_on_the_start_page_are_discarded(body):
    """The start page usually begins mid-way through the *previous* section."""
    rows = run(
        body,
        [[text_block("Tail of the previous clause."), text_block(HEADING),
          text_block("Prose.")]],
    )
    assert texts(rows) == ["Prose."]


def test_on_a_later_page_blocks_are_emitted_even_though_no_heading_was_seen(body):
    """Characterisation, and arguably a defect (body.py:238-243).

    The `page_0 == start_0` guard means the "still before the heading" suppression only
    applies to the start page. If the heading was never matched — a garbled or
    vision-rewritten start page — every block from page two onward is emitted with
    `heading_skipped` still False, so the *heading itself* would be emitted as a content
    row when it finally appears. Pinned rather than fixed.
    """
    rows = run(
        body,
        [[text_block("Unmatched start page.")], [text_block("Page two prose.")]],
    )
    assert texts(rows) == ["Page two prose."]


def test_the_heading_is_skipped_only_once(body):
    """A repeated heading on a continuation page is content, not a second heading."""
    rows = run(body, [[text_block(HEADING), text_block(HEADING)]])
    assert texts(rows) == [HEADING]


# ── the fitz path: coordinate-driven suppression ─────────────────────────────


def test_a_block_in_the_top_sixty_five_points_is_a_running_header_and_is_dropped(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("Header band text.", y0=20, y1=60),
          text_block("Prose.")]],
    )
    assert texts(rows) == ["Prose."]


def test_a_block_in_the_bottom_sixty_five_points_is_a_footer_and_is_dropped(body):
    """The band is measured from the page height, not a constant, for A4 vs Letter."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Footer band.", y0=790, y1=820),
          text_block("Prose.")]],
    )
    assert texts(rows) == ["Prose."]


def test_a_block_straddling_the_footer_boundary_survives(body):
    """Pins the boundary from the inside: `y0 > page_h - 65` is strict."""
    rows = run(body, [[text_block(HEADING), text_block("Low prose.", y0=770, y1=800)]])
    assert texts(rows) == ["Low prose."]


def test_a_non_text_block_is_skipped_before_anything_else_looks_at_it(body):
    """`btype != 0` is an image block; its "text" field is not text."""
    rows = run(
        body, [[text_block(HEADING), text_block("raw image payload", btype=1)]]
    )
    assert rows == []


def test_an_empty_block_contributes_nothing(body):
    rows = run(body, [[text_block(HEADING), text_block("   \n  ")]])
    assert rows == []


def test_text_mostly_inside_an_accepted_formula_rectangle_is_dropped(body):
    """fitz returns a typeset equation as a cloud of positioned glyphs.

    Without this the raw glyph soup ("x k s USL + × ( )") appears as a content row right
    beside the equation's own PNG.
    """
    rows = run(
        body,
        [[text_block(HEADING),
          text_block("x k s USL + glyph soup", y0=300, y1=330)]],
        unnumbered_formulas=[formula(bbox=(60, 290, 540, 340))],
    )
    # The row that survives is the end-of-page flush for the never-anchored formula,
    # which carries no text — proof the glyph block itself was suppressed.
    assert texts(rows) == [""]


def test_a_block_only_half_covered_by_a_formula_rectangle_survives(body):
    """The gate is `> 0.5` of the *block's* area, so partial overlap is not enough."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose.", y0=300, y1=340)]],
        unnumbered_formulas=[formula(bbox=(72, 300, 520, 320))],
    )
    assert "Prose." in texts(rows)


def test_per_cell_text_inside_a_detected_table_rectangle_is_dropped(body):
    """fitz returns a table as one block per cell; the table ships as a PNG instead."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("42", y0=300, y1=320),
          text_block("Prose outside.", y0=500, y1=520)]],
        table_rects_by_page={0: [(60, 290, 540, 340)]},
    )
    assert texts(rows) == ["Prose outside."]


def test_text_drawn_inside_a_figure_region_is_dropped(body):
    """Axis labels and key numbers live inside circuit diagrams and are already in the
    PNG."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("axis label", y0=300, y1=320)]],
        fig_rects_by_page={0: [(60, 290, 540, 340)]},
    )
    assert rows == []


def test_a_figure_caption_inside_a_figure_region_is_let_through_on_purpose(body):
    """The caption row is what the figure's image gets attached to, so it must survive
    the suppression that removes everything else in the same rectangle."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Figure 4 — Test rig", y0=300, y1=320)]],
        fig_rects_by_page={0: [(60, 290, 540, 340)]},
        figures={4: b"FIGPNG"},
    )
    assert texts(rows) == ["Figure 4 — Test rig"]
    assert rows[0]["inline_images"][0]["data"] == b"FIGPNG"


def test_a_zero_area_block_is_never_considered_inside_any_rectangle(body):
    """Guards the division in `_inside_any_rect`; degenerate bboxes do occur."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose.", y0=300, y1=300, x0=72, x1=72)]],
        table_rects_by_page={0: [(0, 290, 595, 340)]},
    )
    assert texts(rows) == ["Prose."]


def test_rectangle_suppression_is_skipped_entirely_when_no_rectangles_are_known(body):
    """The early `if not rects` return is the common case — most pages have neither."""
    rows = run(body, [[text_block(HEADING), text_block("Prose.")]])
    assert texts(rows) == ["Prose."]


# ── text-driven filters, shared by both paths ────────────────────────────────


def test_a_block_shorter_than_three_characters_is_noise(body):
    rows = run(body, [[text_block(HEADING), text_block("a"), text_block("Prose.")]])
    assert texts(rows) == ["Prose."]


def test_a_licensing_watermark_overlay_is_dropped(body):
    """These are drawn over the page and extracted as ordinary text."""
    rows = run(
        body,
        [[text_block(HEADING),
          text_block("Copyrighted material licensed to ABBVIE, single user")]],
    )
    assert rows == []


def test_the_watermark_scan_only_looks_at_the_first_hundred_and_twenty_characters(body):
    """A deliberate bound: prose that happens to mention licensing later is content."""
    tail = "x" * 130 + " licensed to somebody"
    rows = run(body, [[text_block(HEADING), text_block(tail)]])
    assert texts(rows) == [tail]


def test_a_block_that_is_only_a_page_number_is_dropped(body):
    """Four digits, because anything shorter is already caught by the length floor."""
    rows = run(body, [[text_block(HEADING), text_block("1024")]])
    assert rows == []


def test_a_bare_standard_number_line_is_a_running_header_and_is_dropped(body):
    rows = run(body, [[text_block(HEADING), text_block("ISO 10943:2023(E)")]])
    assert rows == []


def test_an_all_rights_reserved_footer_is_dropped(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("14 All rights reserved"),
          text_block("© ISO 2022 – All rights reserved")]],
    )
    assert rows == []


def test_back_matter_ends_the_section_and_the_remaining_pages_are_not_read(body):
    """Bibliography and the BSI copyright pages are not part of any clause."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose."), text_block("Bibliography"),
          text_block("Reference list entry.")],
         [text_block("Page two prose.")]],
    )
    assert texts(rows) == ["Prose."]


def test_a_child_or_sibling_heading_ends_the_section(body):
    """This is what stops content being emitted twice when ToC entries share a page."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose."), text_block("7.2 Materials"),
          text_block("Material prose.")]],
        stop_nums={"7.2"},
    )
    assert texts(rows) == ["Prose."]


def test_an_unnumbered_heading_ends_the_section_when_its_title_is_known(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose."), text_block("Classification"),
          text_block("More.")]],
        stop_titles={"Classification"},
    )
    assert texts(rows) == ["Prose."]


def test_the_stop_heading_check_is_skipped_when_no_stop_set_was_supplied(body):
    """The last ToC entry has nothing after it, so both sets are empty and a block that
    looks like a heading is content."""
    rows = run(body, [[text_block(HEADING), text_block("7.2 Materials")]])
    assert texts(rows) == ["7.2 Materials"]


def test_a_table_continued_marker_is_a_layout_artifact_and_is_dropped(body):
    rows = run(body, [[text_block(HEADING), text_block("Table 3 (continued)")]])
    assert rows == []


def test_a_table_caption_repeated_after_its_image_shipped_is_dropped(body):
    """Print-optimised PDFs repeat the caption on every page the table spans; without
    this each repeat becomes an empty row that reads as truncated content."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Table 3 — Dimensions")]],
        emitted={"table:3"},
    )
    assert rows == []


def test_a_repeated_table_footnote_is_emitted_once_per_section(body):
    """Multi-page tables repeat "a  This applies…" on every continuation page."""
    note = "a  This test applies only to reusable devices."
    rows = run(body, [[text_block(HEADING), text_block(note)], [text_block(note)]])
    assert texts(rows) == [note]


def test_footnote_deduplication_also_silently_drops_a_repeated_numbered_sub_item(body):
    """Characterisation of an overlap between two patterns (body.py:216-219).

    `_FOOTNOTE_MARKER_RE` matches "1) " as well as "a  ", and sub-item lists in these
    documents legitimately restart at "1)" under each new lettered branch. The second
    occurrence is dropped as a duplicate footnote. Pinned, not fixed.
    """
    item = "1) The device shall be inspected."
    rows = run(body, [[text_block(HEADING), text_block(item), text_block(item)]])
    assert texts(rows) == [item]


# ── inline subsection headings ───────────────────────────────────────────────


ANNEX = {"title": "Annex B", "level": 1}


def test_a_deeper_numbered_heading_inside_the_body_becomes_a_header_row(body):
    """The ToC is often coarser than the body: one "Annex B" entry covers B.1..B.n."""
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.1 Deflection"), text_block("Prose.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [(row["type"], row["section_num"]) for row in rows] == [
        ("header", "B.1"),
        ("content", "B.1"),
    ]


def test_a_short_heading_merged_with_a_short_body_is_kept_whole_as_the_header_row(body):
    """Characterisation, and the reason the first-line fallback exists at all.

    `_detect_inline_subsection` accepts the merged block outright when the trailing text
    is under sixty characters, so the body sentence is swallowed into the header row's
    text and never becomes a content row of its own.
    """
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.2 Stiffness\nThe stiffness is measured.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [(row["type"], row["text"]) for row in rows] == [
        ("header", "B.2 Stiffness\nThe stiffness is measured."),
    ]


def test_a_heading_merged_with_a_long_body_is_split_on_its_first_line(body):
    """Vision output joins them with one newline, which is not a paragraph break, so the
    whole thing arrives as a single block. Only the long-sentence case reaches the
    fallback, because that is the only case the whole-block detector rejects."""
    sentence = (
        "The stiffness of the assembly shall be measured under the applied load."
    )
    rows = run(
        body,
        [[text_block("Annex B"), text_block(f"B.2 Stiffness\n{sentence}")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [(row["type"], row["text"]) for row in rows] == [
        ("header", "B.2 Stiffness"),
        ("content", sentence),
    ]


def test_a_bare_inline_heading_emits_a_header_row_and_nothing_else(body):
    """The `if not body_remainder: continue` guard; without it every heading would be
    followed by a content row holding the heading text a second time."""
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.3 Torque"), text_block("Prose.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [(row["type"], row["text"]) for row in rows] == [
        ("header", "B.3 Torque"),
        ("content", "Prose."),
    ]


def test_a_single_line_block_is_not_probed_for_a_merged_heading(body):
    """The fallback needs `len(lines) > 1`; a one-line non-heading stays content."""
    rows = run(
        body,
        [[text_block("Annex B"), text_block("See B.1 for the deflection limit values.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [row["type"] for row in rows] == ["content"]


def test_the_inline_subsection_number_carries_forward_to_later_rows(body):
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.1 Deflection"), text_block("First."),
          text_block("Second.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [row["section_num"] for row in rows] == ["B.1", "B.1", "B.1"]


def test_a_new_inline_heading_resets_the_sub_item_state(body):
    """Otherwise an a) from B.1 would keep decorating every row under B.2."""
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.1 Deflection"),
          text_block("a) first condition"), text_block("B.2 Stiffness"),
          text_block("Plain prose.")]],
        section=ANNEX,
        base_num="Annex B",
    )
    assert [row["section_num"] for row in rows] == ["B.1", "B.1 a)", "B.2", "B.2"]


# ── sub-item markers and the compound section number ─────────────────────────


def test_a_lettered_sub_item_is_appended_to_the_section_number(body):
    rows = run(body, [[text_block(HEADING), text_block("a) the first condition")]])
    assert rows[0]["section_num"] == "7 a)"


def test_sub_item_levels_nest_and_a_shallower_marker_clears_the_deeper_ones(body):
    """The stack is truncated on the way back up, which is why the later b) does not
    inherit the arabic marker from the branch it just left."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("a) outer"), text_block("1) inner"),
          text_block("b) next outer")]],
    )
    assert [row["section_num"] for row in rows] == ["7 a)", "7 a) 1)", "7 b)"]


def test_a_single_letter_roman_sub_item_is_captured_as_a_lettered_one_instead(body):
    """Characterisation of a pattern collision (rows.py:55-58, applied at body.py:299).

    The level-0 pattern `^([a-z])\\)` matches "i)" before the level-2 roman pattern is
    ever tried, so the first roman item overwrites its own parent letter. The next item,
    "ii)", does land at level 2 — but level 1 is empty, so the compound number stops at
    the stale "i)" and both items come out identically numbered. Pinned, not fixed.
    """
    rows = run(
        body,
        [[text_block(HEADING), text_block("a) outer"), text_block("i) first roman"),
          text_block("ii) second roman")]],
    )
    assert [row["section_num"] for row in rows] == ["7 a)", "7 i)", "7 i)"]


def test_the_compound_number_stops_at_the_first_gap_in_the_stack(body):
    """A roman marker with no lettered parent must not produce "7  iii)"."""
    rows = run(body, [[text_block(HEADING), text_block("ii) orphaned roman item")]])
    # Level 1 (arabic) is empty, so the level-2 marker is never reached.
    assert rows[0]["section_num"] == "7"


def test_the_sub_item_stack_is_cleared_when_the_page_changes(body):
    """A letter bullet from one page must not corrupt the number on the next."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("a) condition on page one")],
         [text_block("Prose on page two.")]],
    )
    assert [row["section_num"] for row in rows] == ["7 a)", "7"]


# ── media attachment through `_build_row_images` ─────────────────────────────


def test_a_caption_row_carries_the_image_it_names(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("Figure 2 — Assembly")]],
        figures={2: b"FIG2"},
    )
    assert rows[0]["inline_images"] == [
        {"type": "figure", "number": 2, "data": b"FIG2", "caption": ""}
    ]


def test_a_table_captured_outside_the_selected_page_range_is_not_attached(body):
    """The page-range arguments are passed straight through, and they are the only thing
    stopping a reference to Table 9 from pasting a table from an unselected clause."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("as given in Table 9 of this clause")]],
        tables={"9": [b"TBL9"]},
        media_first_page={"table:9": 40},
        range_start_0=0,
        range_end_0=5,
    )
    assert rows[0]["inline_images"] == []


# ── unnumbered formulas: anchoring and the end-of-page flush ─────────────────


def test_a_formula_is_attached_to_the_block_its_anchor_ends(body):
    """`_find_preceding_anchor` stores the tail of the block above the equation, so an
    `endswith` match is the tight, always-safe case."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("calculated as follows:")]],
        unnumbered_formulas=[formula(anchor="calculated as follows:", image=b"EQ",
                                     eqn_number="(7)", latex="a=b")],
    )
    assert rows[0]["inline_images"] == [
        {"type": "formula", "data": b"EQ", "caption": "(7)", "latex": "a=b"}
    ]
    assert len(rows) == 1, "an anchored formula must not also be flushed at page end"


def test_a_long_anchor_may_match_in_the_middle_of_a_block(body):
    """Anchors at or above ten characters are specific enough for a contains-check."""
    rows = run(
        body,
        [[text_block(HEADING),
          text_block("where the deflection angle is measured at the tip")]],
        unnumbered_formulas=[formula(anchor="deflection angle")],
    )
    assert rows[0]["inline_images"][0]["type"] == "formula"


def test_a_long_anchor_must_still_sit_on_word_boundaries(body):
    """Ten characters is not enough to make a mid-word hit meaningful."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("the deflectionangles are tabulated below")]],
        unnumbered_formulas=[formula(anchor="deflection")],
    )
    assert rows[0]["inline_images"] == []


def test_a_short_anchor_is_never_matched_as_a_substring(body):
    """Otherwise "and" would anchor an equation to the word "standard"."""
    assert body._ANCHOR_MIN_LEN == 10, "the substring threshold this test is built on"
    rows = run(
        body,
        [[text_block(HEADING), text_block("the standard requires periodic checks")]],
        unnumbered_formulas=[formula(anchor="and")],
    )
    assert rows[0]["inline_images"] == []


def test_an_anchorless_formula_is_flushed_as_its_own_row_at_the_end_of_the_page(body):
    """Nothing captured is allowed to vanish: a formula whose anchor could not be found
    still has to reach the document, even without a paragraph to sit under."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Ordinary prose.")]],
        unnumbered_formulas=[formula(anchor="", image=b"EQ")],
    )
    assert texts(rows) == ["Ordinary prose.", ""]
    assert rows[1]["inline_images"][0]["data"] == b"EQ"
    assert rows[1]["type"] == "content"


def test_the_flushed_formula_row_uses_the_current_inline_subsection_number(body):
    rows = run(
        body,
        [[text_block("Annex B"), text_block("B.4 Force")]],
        section=ANNEX,
        base_num="Annex B",
        unnumbered_formulas=[formula(anchor="")],
    )
    assert rows[-1]["section_num"] == "B.4"


def test_a_formula_already_emitted_by_another_section_is_neither_matched_nor_flushed(body):
    """`formula_emitted` is shared across sections precisely so a formula on a page two
    clauses both touch is pasted once."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Ordinary prose.")]],
        unnumbered_formulas=[formula(index=3, anchor="Ordinary prose.")],
        formula_emitted={3},
    )
    assert rows[0]["inline_images"] == []
    assert len(rows) == 1


def test_two_formula_entries_sharing_an_index_are_flushed_only_once(body):
    """The flush re-checks `formula_emitted` on every iteration, not just at page start.

    That second check is only reachable when the detector hands back the same global index
    twice — which it can, since the list is concatenated per section slice — and it is what
    stops one equation being pasted twice into the same cell.
    """
    rows = run(
        body,
        [[text_block(HEADING), text_block("Ordinary prose.")]],
        unnumbered_formulas=[formula(index=0, image=b"A"), formula(index=0, image=b"B")],
    )
    flushed = [row for row in rows if row["text"] == ""]
    assert len(flushed) == 1
    assert flushed[0]["inline_images"][0]["data"] == b"A"


def test_a_formula_belonging_to_another_page_is_left_for_that_page(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("Page one prose.")],
         [text_block("Page two prose.")]],
        unnumbered_formulas=[formula(page_0=1, anchor="Page two prose.")],
    )
    attached = [row for row in rows if row["inline_images"]]
    assert [row["text"] for row in attached] == ["Page two prose."]


# ── unlabeled tables: the second end-of-page flush ───────────────────────────


def test_a_table_with_no_caption_is_flushed_as_its_own_row(body):
    """Some ISO tables carry no "Table N" line at all; the PNG would otherwise be
    dropped on the floor with no trace in the output."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose.")]],
        tables={"x1": [b"P1", b"P2"]},
        unlabeled_tables_by_page={0: ["x1"]},
    )
    assert texts(rows) == ["Prose.", ""]
    assert [img["data"] for img in rows[1]["inline_images"]] == [b"P1", b"P2"]
    assert rows[1]["inline_images"][0]["caption"] == "Table (page 1)"


def test_an_unlabeled_table_already_emitted_is_not_flushed_again(body):
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose.")]],
        tables={"x1": [b"P1"]},
        unlabeled_tables_by_page={0: ["x1"]},
        emitted={"table:x1"},
    )
    assert texts(rows) == ["Prose."]


def test_an_unlabeled_table_reference_with_no_stored_image_is_skipped(body):
    """Defensive: the rect list and the image dict are built by separate passes."""
    rows = run(
        body,
        [[text_block(HEADING), text_block("Prose.")]],
        tables={},
        unlabeled_tables_by_page={0: ["missing"]},
    )
    assert texts(rows) == ["Prose."]


def test_the_flush_marks_the_table_emitted_so_a_second_section_skips_it(body):
    """The set is the caller's, so the effect has to be visible outside the call."""
    emitted = set()
    run(
        body,
        [[text_block(HEADING), text_block("Prose.")]],
        tables={"x1": [b"P1"]},
        unlabeled_tables_by_page={0: ["x1"]},
        emitted=emitted,
    )
    assert emitted == {"table:x1"}


# ── the vision path ──────────────────────────────────────────────────────────


def vision(body, page_text, **kwargs):
    """Run one page through the LLM-text branch instead of the fitz branch."""
    return run(body, [[]], llm_texts={0: page_text}, **kwargs)


def test_llm_page_text_is_split_into_paragraph_blocks(body):
    rows = vision(body, f"{HEADING}\n\nFirst paragraph.\n\nSecond paragraph.")
    assert texts(rows) == ["First paragraph.", "Second paragraph."]


def test_the_coordinate_header_filter_does_not_apply_to_llm_text(body):
    """The vision prompt already excludes running headers, and LLM blocks have no
    coordinates to filter by — so a page supplied as text has no y-band suppression at
    all, only the pattern-based filters."""
    rows = vision(body, f"{HEADING}\n\nProse that would sit in the footer band.")
    assert texts(rows) == ["Prose that would sit in the footer band."]


def test_a_bare_key_header_over_a_figure_is_suppressed_on_the_vision_path(body):
    """It labels the legend inside the figure PNG, so as a row it is a stray word."""
    rows = vision(
        body,
        f"{HEADING}\n\nKey\n\nBody prose.",
        fig_rects_by_page={0: [(0, 0, 595, 842)]},
    )
    assert texts(rows) == ["Body prose."]


def test_a_multi_line_key_block_is_kept_because_it_is_the_legend_content(body):
    """"Key\\n1 position of handle\\n2 extension" is body content below the figure, and
    dropping it loses real requirements text."""
    legend = "Key\n1 position from where the handle extends"
    rows = vision(
        body, f"{HEADING}\n\n{legend}", fig_rects_by_page={0: [(0, 0, 595, 842)]}
    )
    assert texts(rows) == [legend]


def test_a_key_header_on_a_page_with_no_detected_figure_is_kept(body):
    """The suppression is gated on a figure actually having been found on the page."""
    rows = vision(body, f"{HEADING}\n\nKey\n\nBody prose.")
    assert texts(rows) == ["Key", "Body prose."]


def test_a_figure_caption_starting_with_key_is_never_suppressed(body):
    """Defensive ordering in the source; the caption row must reach the image."""
    rows = vision(
        body,
        f"{HEADING}\n\nFigure 5 — Key components",
        fig_rects_by_page={0: [(0, 0, 595, 842)]},
    )
    assert texts(rows) == ["Figure 5 — Key components"]


def test_table_rectangles_are_ignored_on_the_vision_path(body):
    """Documented as intentional: the LLM's narrative output already excludes per-cell
    text, and there are no block coordinates to test containment against."""
    rows = vision(
        body,
        f"{HEADING}\n\nA paragraph that overlaps the table region.",
        table_rects_by_page={0: [(0, 0, 595, 842)]},
    )
    assert texts(rows) == ["A paragraph that overlaps the table region."]


def test_a_page_absent_from_the_llm_text_map_falls_back_to_fitz_blocks(body):
    """Mixed documents exist: vision is run only on the pages that came out garbled."""
    doc = FakeDoc([
        FakePage([]),
        FakePage([text_block("Fitz prose from page two.")]),
    ])
    rows = body._extract_body(
        doc, SECTION, 0, 1, "7", {}, {}, {}, llm_texts={0: HEADING}
    )
    assert texts(rows) == ["Fitz prose from page two."]
