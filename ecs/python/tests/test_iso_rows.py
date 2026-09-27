"""Row assembly for the ISO silo (`silos/iso/rows.py`).

This module was at 0% coverage while owning the one invariant the whole ISO report is
judged on: **every figure, table and formula appears exactly once**. That invariant is
not enforced by a single check — it is spread across a four-way rule in
`_build_row_images`, a shared `emitted` set threaded through every section, and two
end-of-run flush passes that place whatever the rule never reached. Any one of those
three can be "improved" in isolation and produce a report with a table printed twice, or
silently missing. So the bulk of what follows pins the arbitration itself: caption wins,
a reference defers to a caption still to come, a reference to an out-of-range caption
adopts the image, and anything already emitted gets a highlight only.

Two deliberate choices about how these tests are built:

**`extract_all_rows` gets a real PDF but a stubbed `_extract_body`.** The real body
extractor is a separate 400-statement module owned elsewhere; letting it run would mean
every assertion here about placement was really an assertion about its block filtering.
It is stubbed through the *module object* (`body._extract_body`) rather than through
`rows`, because `rows` imports it function-locally to break an import cycle — so the name
is looked up on the module at call time, and patching the module is the only patch that
takes effect.

**Captions that must be seen by the pre-scan are fed as LLM text where they contain
maths.** fitz's base-14 fonts cannot encode "=" ... they can, but they cannot encode an
em-dash or "≤" (both arrive as U+FFFD), so a formula caption written into a synthetic PDF
would never match `_MATH_CHARS_RE`. Real-PDF pre-scan tests therefore use ASCII-hyphen
figure/table captions, and the formula pre-scan is driven through `llm_texts`.
"""

from __future__ import annotations

import fitz
import pytest
from docx import Document
from docx.shared import Pt

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "rows.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def rows():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "rows.py", name="rows")


@pytest.fixture(scope="module")
def body_mod(rows):
    """The module `rows` reaches into at call time; patch target for `_extract_body`."""
    return _load_module("iso", ISO_DIR / "body.py", name="body")


# ── test doubles ─────────────────────────────────────────────────────────────


def span(text, *, font="Helvetica", italic=False, bbox=(72, 100, 200, 114)):
    """One fitz text span. `flags & 2` is how fitz reports italic."""
    return {"text": text, "font": font, "flags": 2 if italic else 0, "bbox": bbox}


def line(spans, bbox=None):
    """One fitz text line. Defaults its bbox to the union of its spans."""
    if bbox is None and spans:
        xs = [s["bbox"][0] for s in spans] + [s["bbox"][2] for s in spans]
        ys = [s["bbox"][1] for s in spans] + [s["bbox"][3] for s in spans]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    return {"bbox": bbox or (0, 0, 0, 0), "spans": spans}


class FakePage:
    """A page that answers only what this module actually asks of one."""

    def __init__(self, lines=(), blocks=(), width=595.0, height=842.0, image_block=False):
        self._lines = list(lines)
        self._blocks = list(blocks)
        self.rect = fitz.Rect(0, 0, width, height)
        self._image_block = image_block

    def get_text(self, kind, sort=False):
        if kind == "dict":
            blocks = [{"type": 0, "lines": self._lines}]
            if self._image_block:
                blocks.insert(0, {"type": 1, "bbox": (0, 0, 10, 10)})
            return {"blocks": blocks}
        if kind == "blocks":
            return list(self._blocks)
        raise AssertionError(f"unexpected get_text kind {kind!r}")


def text_block(text, y0, y1, *, x0=72.0, x1=520.0, btype=0, number=0):
    """A `get_text("blocks")` tuple: (x0, y0, x1, y1, text, block_no, block_type)."""
    return (x0, y0, x1, y1, text, number, btype)


def build_pdf(path, *, pages=1, lines_per_page=None):
    """A real PDF. Text is ASCII only: helv cannot encode an em-dash or a math glyph."""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        entries = (lines_per_page or {}).get(index, [("Ordinary body text.", 300)])
        for text, y in entries:
            page.insert_text((72, y), text, fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    return str(path)


def content_row(section_num, text, level=1, images=None):
    return {
        "type": "content",
        "section_num": section_num,
        "text": text,
        "level": level,
        "inline_images": list(images or []),
    }


@pytest.fixture
def stub_body(monkeypatch, body_mod):
    """Install a stand-in `_extract_body`; returns the recorded call list.

    `by_section` maps a section's base number to the rows it should return, so a test can
    place a reference in exactly one section's text and watch where the flush lands.
    """

    def install(by_section=None, on_call=None):
        calls = []

        def _fake(doc, section, start_0, end_0, base_num, figures, tables, formulas, **kw):
            calls.append(
                {
                    "base_num": base_num,
                    "start_0": start_0,
                    "end_0": end_0,
                    "title": section["title"],
                    **kw,
                }
            )
            if on_call is not None:
                on_call(kw)
            return [dict(r) for r in (by_section or {}).get(base_num, [])]

        monkeypatch.setattr(body_mod, "_extract_body", _fake)
        return calls

    return install


# ── _split_llm_blocks ────────────────────────────────────────────────────────


def test_blank_lines_separate_paragraphs_in_llm_page_text(rows):
    assert rows._split_llm_blocks("first para\n\nsecond para") == ["first para", "second para"]


def test_runs_of_more_than_two_newlines_still_split_only_once(rows):
    """The vision model pads generously; three blank lines is not three paragraphs."""
    assert rows._split_llm_blocks("a\n\n\n\nb") == ["a", "b"]


def test_whitespace_only_paragraphs_are_dropped(rows):
    assert rows._split_llm_blocks("a\n\n   \n\nb") == ["a", "b"]


def test_a_caption_buried_mid_paragraph_is_promoted_to_its_own_block(rows):
    """Otherwise `_FIGURE_CAPTION_RE.match` never fires and the figure is never placed.

    The model returns caption and body glued together far more often than not, so this
    re-split is the only reason the vision path emits figures at all.
    """
    assert rows._split_llm_blocks("intro sentence\nFigure 1 - The caption") == [
        "intro sentence",
        "Figure 1 - The caption",
    ]


def test_a_table_caption_buried_mid_paragraph_is_also_promoted(rows):
    assert rows._split_llm_blocks("intro\nTable 3.1 - Limits\ntrailing note") == [
        "intro",
        "Table 3.1 - Limits\ntrailing note",
    ]


def test_a_caption_already_starting_a_block_is_left_alone(rows):
    """No leading text to split off, so the caption must not produce an empty block."""
    assert rows._split_llm_blocks("Figure 1 - Caption\nand its note") == [
        "Figure 1 - Caption\nand its note"
    ]


def test_two_captions_in_one_paragraph_become_two_blocks(rows):
    assert rows._split_llm_blocks("Figure 1 - A\nFigure 2 - B") == [
        "Figure 1 - A",
        "Figure 2 - B",
    ]


def test_empty_page_text_yields_no_blocks(rows):
    assert rows._split_llm_blocks("") == []


# ── _add_run_with_breaks ─────────────────────────────────────────────────────


@pytest.fixture
def para():
    return Document().add_paragraph()


def test_text_without_newlines_becomes_a_single_run(rows, para):
    rows._add_run_with_breaks(para, "one line", Pt(9))
    assert [r.text for r in para.runs] == ["one line"]


def breaks(para):
    """Count `w:br` elements. Read off the serialised XML: the `w:` prefix is fixed here."""
    return para._p.xml.count("<w:br/>")


def test_each_newline_becomes_a_word_line_break_element(rows, para):
    """A cell with `\\n` rendered literally would show one long unreadable line."""
    rows._add_run_with_breaks(para, "first\nsecond", Pt(9))
    assert breaks(para) == 1
    # `Run.text` renders a `w:br` child back as "\n", which is why the first run reads
    # "first\n": the break element is appended to the run that precedes it.
    assert [r.text for r in para.runs] == ["first\n", "second"]


def test_a_leading_newline_gets_an_empty_carrier_run_for_its_break(rows, para):
    """There is no previous run to hang the `w:br` on, so one has to be created."""
    rows._add_run_with_breaks(para, "\nsecond", Pt(9))
    assert breaks(para) == 1
    assert [r.text for r in para.runs] == ["\n", "second"]


def test_highlighting_is_applied_to_every_part_of_a_multi_line_run(rows, para):
    from docx.enum.text import WD_COLOR_INDEX

    rows._add_run_with_breaks(para, "Table 1\nTable 2", Pt(9), highlight=True)
    assert all(r.font.highlight_color == WD_COLOR_INDEX.YELLOW for r in para.runs if r.text)


# ── _write_text_highlighted ──────────────────────────────────────────────────


def highlighted(para):
    from docx.enum.text import WD_COLOR_INDEX

    return [r.text for r in para.runs if r.font.highlight_color == WD_COLOR_INDEX.YELLOW]


def test_a_reference_inside_a_sentence_is_the_only_highlighted_run(rows, para):
    rows._write_text_highlighted(para, "As shown in Table 3.1 the limit applies.", Pt(9))
    assert highlighted(para) == ["Table 3.1"]


def test_a_reference_at_the_very_start_needs_no_preceding_run(rows, para):
    rows._write_text_highlighted(para, "Figure 4 shows the profile.", Pt(9))
    assert highlighted(para) == ["Figure 4"]
    assert para.runs[0].text == "Figure 4"


def test_every_reference_in_a_sentence_is_highlighted_independently(rows, para):
    rows._write_text_highlighted(para, "see Figure 1 and Table A.1 and Formula (2)", Pt(9))
    assert highlighted(para) == ["Figure 1", "Table A.1", "Formula (2)"]


def test_text_with_no_references_produces_one_unhighlighted_run(rows, para):
    rows._write_text_highlighted(para, "Plain requirement text.", Pt(9))
    assert highlighted(para) == []
    assert [r.text for r in para.runs] == ["Plain requirement text."]


def test_xml_illegal_control_bytes_are_stripped_before_the_text_is_written(rows, para):
    """PyMuPDF extracts these verbatim from some ISO PDFs and lxml refuses the document."""
    rows._write_text_highlighted(para, "before\x08after", Pt(9))
    assert "".join(r.text for r in para.runs) == "beforeafter"


# ── _build_row_images: figures ───────────────────────────────────────────────


def test_a_figure_caption_row_adopts_the_image_and_suppresses_the_subtitle(rows):
    """Rule 1. The caption is already the row text, so a second label would duplicate it."""
    emitted = set()
    imgs = rows._build_row_images("Figure 1 - Bore profile", {1: b"png"}, {}, {}, emitted)
    assert imgs == [{"type": "figure", "number": 1, "data": b"png", "caption": ""}]
    assert emitted == {"figure:1"}


def test_a_figure_caption_for_an_uncaptured_figure_yields_nothing(rows):
    assert rows._build_row_images("Figure 9 - Missing", {1: b"png"}, {}, {}, set()) == []


def test_a_second_row_repeating_the_caption_does_not_paste_the_figure_again(rows):
    """Continuation pages repeat captions verbatim; this is the duplicate-image guard."""
    emitted = {"figure:1"}
    assert rows._build_row_images("Figure 1 - Bore profile", {1: b"png"}, {}, {}, emitted) == []


def test_a_reference_defers_to_a_caption_that_is_still_to_come(rows):
    """Rule 2. The caption row will place it properly, with its real caption text."""
    emitted = set()
    imgs = rows._build_row_images(
        "as shown in Figure 1", {1: b"png"}, {}, {}, emitted, captions_in_range={"figure:1"}
    )
    assert imgs == []
    assert emitted == set()


def test_a_reference_to_an_out_of_range_caption_adopts_the_image_itself(rows):
    """Rule 3. Nothing else will ever place it, so the reference has to."""
    imgs = rows._build_row_images("see Figure 1", {1: b"png"}, {}, {}, set(), captions_in_range=set())
    assert imgs == [{"type": "figure", "number": 1, "data": b"png", "caption": "Figure 1"}]


def test_a_reference_carries_a_synthetic_caption_the_caption_row_would_not(rows):
    """The reader needs a label because the surrounding text is not the caption."""
    imgs = rows._build_row_images("see Figure 1", {1: b"png"}, {}, {}, set())
    assert imgs[0]["caption"] == "Figure 1"


def test_several_figure_references_in_one_row_each_attach_once(rows):
    emitted = set()
    imgs = rows._build_row_images(
        "compare Figure 1 with Figure 2 and Figure 1 again",
        {1: b"a", 2: b"b"},
        {},
        {},
        emitted,
    )
    assert [i["number"] for i in imgs] == [1, 2]


def test_a_figure_captured_outside_the_selected_pages_is_not_pulled_in_by_a_reference(rows):
    """The user picked clause 7; a figure from clause 12 must not appear in their report."""
    imgs = rows._build_row_images(
        "see Figure 1",
        {1: b"png"},
        {},
        {},
        set(),
        media_first_page={"figure:1": 40},
        range_start_0=0,
        range_end_0=10,
    )
    assert imgs == []


def test_a_figure_whose_capture_page_is_unknown_is_given_the_benefit_of_the_doubt(rows):
    """Dropping it would lose content; the alternative failure is only misplacement."""
    imgs = rows._build_row_images(
        "see Figure 1", {1: b"png"}, {}, {}, set(),
        media_first_page={}, range_start_0=0, range_end_0=10,
    )
    assert len(imgs) == 1


def test_without_a_page_index_the_range_filter_is_inert(rows):
    imgs = rows._build_row_images(
        "see Figure 1", {1: b"png"}, {}, {}, set(), media_first_page=None, range_start_0=None
    )
    assert len(imgs) == 1


def test_a_figure_stored_as_a_file_path_is_read_off_disk(rows, tmp_path):
    """The persisted-session path hands `figures` filenames, not bytes."""
    png = tmp_path / "fig.png"
    png.write_bytes(b"\x89PNG-on-disk")
    imgs = rows._build_row_images("Figure 1 - From disk", {1: str(png)}, {}, {}, set())
    assert imgs[0]["data"] == b"\x89PNG-on-disk"


# ── _build_row_images: tables ────────────────────────────────────────────────


def test_a_table_caption_row_adopts_every_page_slice_of_the_table(rows):
    """A table spanning three pages is three images: one row, three inline entries."""
    emitted = set()
    imgs = rows._build_row_images(
        "Table 3.1 - Limits", {}, {"3.1": [b"p1", b"p2", b"p3"]}, {}, emitted
    )
    assert [i["data"] for i in imgs] == [b"p1", b"p2", b"p3"]
    assert all(i["caption"] == "" for i in imgs)
    assert emitted == {"table:3.1"}


def test_a_legacy_single_image_table_still_produces_one_entry(rows):
    imgs = rows._build_row_images("Table 3.1 - Limits", {}, {"3.1": b"only"}, {}, set())
    assert [i["data"] for i in imgs] == [b"only"]


def test_an_annex_lettered_table_caption_is_recognised(rows):
    imgs = rows._build_row_images("Table A.1 - Annex limits", {}, {"A.1": b"x"}, {}, set())
    assert imgs[0]["ref"] == "A.1"


def test_a_table_reference_defers_to_a_caption_in_range(rows):
    assert (
        rows._build_row_images(
            "given in Table 3.1", {}, {"3.1": b"x"}, {}, set(), captions_in_range={"table:3.1"}
        )
        == []
    )


def test_a_table_reference_to_an_out_of_range_caption_adopts_the_slices_with_a_label(rows):
    imgs = rows._build_row_images("given in Table 3.1", {}, {"3.1": [b"a", b"b"]}, {}, set())
    assert [i["caption"] for i in imgs] == ["Table 3.1", "Table 3.1"]


def test_a_table_captured_outside_the_selected_pages_is_not_pulled_in_by_a_reference(rows):
    imgs = rows._build_row_images(
        "given in Table 3.1", {}, {"3.1": b"x"}, {}, set(),
        media_first_page={"table:3.1": 99}, range_start_0=0, range_end_0=5,
    )
    assert imgs == []


def test_an_already_emitted_table_is_highlighted_only(rows):
    """Rule 4: the row still gets its yellow reference, but no second copy of the image."""
    emitted = {"table:3.1"}
    assert rows._build_row_images("see Table 3.1", {}, {"3.1": b"x"}, {}, emitted) == []


# ── _build_row_images: formulas ──────────────────────────────────────────────


def test_a_trailing_number_plus_a_math_glyph_is_treated_as_the_formula_itself(rows):
    """Both halves are required: "(3)" alone is a list item, "x = y" alone is unnumbered."""
    emitted = set()
    imgs = rows._build_row_images("x = y + z (3)", {}, {}, {3: b"f"}, emitted)
    assert imgs == [{"type": "formula", "number": 3, "data": b"f", "caption": "Formula (3)"}]
    assert emitted == {"formula:3"}


def test_a_trailing_number_without_any_math_glyph_is_not_a_formula_body(rows):
    """It is an enumerated clause item, and pasting a formula there would be wrong."""
    imgs = rows._build_row_images("the following applies (3)", {}, {}, {3: b"f"}, set())
    assert imgs[0]["caption"] == "Formula (3)"


def test_an_explicit_formula_reference_attaches_the_image(rows):
    imgs = rows._build_row_images("as given by Formula (3) above", {}, {}, {3: b"f"}, set())
    assert [i["number"] for i in imgs] == [3]


def test_a_formula_reference_defers_to_a_caption_in_range(rows):
    assert (
        rows._build_row_images(
            "as given by Formula (3) above", {}, {}, {3: b"f"}, set(),
            captions_in_range={"formula:3"},
        )
        == []
    )


def test_a_formula_captured_outside_the_range_is_still_attached_by_a_reference(rows):
    """Characterization, not endorsement.

    Figures and tables both consult `_in_media_range` on the reference-fallback path;
    the formula branch (rows.py:225-233) never does. So a formula whose capture page is
    far outside the selection is emitted anyway. Pinned as-is: if the omission is
    deliberate it should be commented in src, and if it is not, this test will fail loudly
    when it is fixed.
    """
    imgs = rows._build_row_images(
        "as given by Formula (3)", {}, {}, {3: b"f"}, set(),
        media_first_page={"formula:3": 99}, range_start_0=0, range_end_0=5,
    )
    assert len(imgs) == 1


def test_an_already_emitted_formula_is_not_repeated(rows):
    assert rows._build_row_images("x = y (3)", {}, {}, {3: b"f"}, {"formula:3"}) == []


def test_a_row_can_carry_a_figure_a_table_and_a_formula_at_once(rows):
    """The three branches are independent, and real body paragraphs do reference all three."""
    imgs = rows._build_row_images(
        "see Figure 1, Table 3.1 and Formula (2)",
        {1: b"f"},
        {"3.1": b"t"},
        {2: b"m"},
        set(),
    )
    assert [i["type"] for i in imgs] == ["figure", "table", "formula"]


def test_ordinary_prose_attaches_nothing(rows):
    assert rows._build_row_images("This clause specifies requirements.", {1: b"f"}, {}, {}, set()) == []


# ── extract_all_rows: structure ──────────────────────────────────────────────


TOC = [
    {"title": "4 General requirements", "page": 1, "level": 1},
    {"title": "5 Test methods", "page": 3, "level": 1},
    {"title": "6 Marking", "page": 4, "level": 1},
]


def test_every_selected_section_contributes_a_header_row_then_its_body(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "body of four")], "5": [content_row("5", "body of five")]})
    out = rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=4), TOC, 0, 1, {}, {}, {})
    assert [(r["type"], r["text"]) for r in out] == [
        ("header", "4 General requirements"),
        ("content", "body of four"),
        ("header", "5 Test methods"),
        ("content", "body of five"),
    ]


def test_the_header_row_carries_the_bare_section_number_and_the_toc_level(rows, stub_body, tmp_path):
    stub_body()
    out = rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=4), TOC, 0, 0, {}, {}, {})
    assert out[0]["section_num"] == "4"
    assert out[0]["level"] == 1
    assert out[0]["inline_images"] == []


def test_a_section_is_bounded_by_the_next_toc_entry_and_by_the_selection(rows, stub_body, tmp_path):
    """Each section reaches the *start* page of the next one, deliberately.

    `_section_end_page_0` overshoots by design so trailing content sitting above the next
    heading is still captured; `_extract_body` stops on the heading itself. The last
    selected section is then clamped to the selection's own end, not the document's.
    """
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=6), TOC, 0, 1, {}, {}, {})
    assert [(c["start_0"], c["end_0"]) for c in calls] == [(0, 2), (2, 3)]


def test_the_last_selected_section_ends_at_the_last_page_of_the_document(rows, stub_body, tmp_path):
    """No later TOC entry to bound it, so everything to the end of the PDF belongs to it."""
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=6), TOC, 2, 2, {}, {}, {})
    assert (calls[0]["start_0"], calls[0]["end_0"]) == (3, 5)


def test_the_emitted_set_is_the_same_object_for_every_section(rows, stub_body, tmp_path):
    """This identity is what makes "exactly once" hold across section boundaries."""
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=4), TOC, 0, 2, {}, {}, {})
    assert calls[0]["emitted"] is calls[1]["emitted"] is calls[2]["emitted"]


def test_each_section_is_told_the_numbers_and_titles_of_every_later_section(rows, stub_body, tmp_path):
    """These are the stop signals; a missing one runs one section's text into the next."""
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=4), TOC, 0, 1, {}, {}, {})
    assert calls[0]["stop_nums"] == {"5", "6"}
    assert calls[0]["stop_titles"] == {"5 Test methods", "6 Marking"}
    assert calls[1]["stop_nums"] == {"6"}


def test_a_toc_entry_titled_only_with_whitespace_makes_the_whole_run_raise(rows, stub_body, tmp_path):
    """Characterization of a crash, not of desired behaviour.

    Suspected defect: `sections.py:17` falls back to `title.split()[0]` guarded only by
    `if title`, which is truthy for "   " while `split()` returns []. The stop-signal
    comprehension at rows.py:311-313 calls `_section_num` on *every* later TOC title, so a
    single whitespace-only bookmark — which real PDF outlines do contain — aborts the
    entire extraction with IndexError instead of being skipped as unnumbered. The guard
    wants to be `if title.strip()`.
    """
    toc = [dict(TOC[0]), {"title": "   ", "page": 3, "level": 2}, dict(TOC[2])]
    stub_body()
    with pytest.raises(IndexError):
        rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=5), toc, 0, 0, {}, {}, {})


def test_a_toc_entry_with_an_empty_title_is_not_offered_as_a_stop_signal(rows, stub_body, tmp_path):
    """A genuinely empty bookmark title yields no number, so it cannot become a stop."""
    toc = [dict(TOC[0]), {"title": "", "page": 3, "level": 2}, dict(TOC[2])]
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=5), toc, 0, 0, {}, {}, {})
    assert calls[0]["stop_titles"] == {"6 Marking"}
    assert calls[0]["stop_nums"] == {"6"}


def test_the_selection_page_bounds_are_handed_to_the_body_extractor(rows, stub_body, tmp_path):
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=6), TOC, 0, 1, {}, {}, {})
    assert (calls[0]["range_start_0"], calls[0]["range_end_0"]) == (0, 3)


def test_a_missing_unlabeled_table_map_becomes_an_empty_dict_not_none(rows, stub_body, tmp_path):
    """`_extract_body` indexes it unconditionally, so None would raise on every section."""
    calls = stub_body()
    rows.extract_all_rows(build_pdf(tmp_path / "s.pdf", pages=4), TOC, 0, 0, {}, {}, {})
    assert calls[0]["unlabeled_tables_by_page"] == {}


# ── extract_all_rows: the caption pre-scan ───────────────────────────────────


def test_captions_on_the_selected_pages_are_found_before_extraction_starts(rows, stub_body, tmp_path):
    """The pre-scan is what lets a reference know a caption is coming and stand down."""
    pdf = build_pdf(
        tmp_path / "cap.pdf",
        pages=4,
        lines_per_page={
            0: [("Figure 1 - Bore profile", 300)],
            1: [("Table 3.1 - Limits", 300)],
        },
    )
    calls = stub_body()
    rows.extract_all_rows(pdf, TOC, 0, 0, {}, {}, {})
    assert calls[0]["captions_in_range"] == {"figure:1", "table:3.1"}


def test_a_caption_outside_the_selected_pages_is_not_in_the_pre_scan(rows, stub_body, tmp_path):
    """If it were, the in-range reference would defer to a caption row that never comes."""
    pdf = build_pdf(
        tmp_path / "cap.pdf", pages=6, lines_per_page={4: [("Figure 9 - Elsewhere", 300)]}
    )
    calls = stub_body()
    rows.extract_all_rows(pdf, TOC, 0, 0, {}, {}, {})
    assert calls[0]["captions_in_range"] == set()


def test_the_pre_scan_reads_llm_text_in_preference_to_the_pdf_for_a_page(rows, stub_body, tmp_path):
    """On the vision path the fitz text layer is garbled, so its captions are unusable."""
    pdf = build_pdf(tmp_path / "llm.pdf", pages=4, lines_per_page={0: [("Figure 1 - Garbled", 300)]})
    calls = stub_body()
    rows.extract_all_rows(
        pdf, TOC, 0, 0, {}, {}, {}, llm_texts={0: "prose\n\nTable 7 - From the model"}
    )
    assert calls[0]["captions_in_range"] == {"table:7"}


def test_a_formula_caption_is_recognised_by_the_pre_scan(rows, stub_body, tmp_path):
    """Driven through llm_texts because helv cannot encode the math glyph the rule needs."""
    calls = stub_body()
    rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {}, {}, {},
        llm_texts={0: "sigma = 2 x y (4)"},
    )
    assert calls[0]["captions_in_range"] == {"formula:4"}


def test_a_selection_reaching_past_the_end_of_the_pdf_does_not_read_a_missing_page(rows, stub_body, tmp_path):
    """The TOC page numbers come from the bookmarks and routinely overrun a trimmed PDF."""
    toc = [{"title": "4 General", "page": 1, "level": 1}, {"title": "5 Next", "page": 40, "level": 1}]
    stub_body()
    out = rows.extract_all_rows(build_pdf(tmp_path / "short.pdf", pages=2), toc, 0, 0, {}, {}, {})
    assert [r["type"] for r in out] == ["header"]


# ── extract_all_rows: unnumbered formula flush ───────────────────────────────


def uf(section_idx, image=b"png", **extra):
    entry = {"section_idx": section_idx, "image": image}
    entry.update(extra)
    return entry


def test_unnumbered_formulas_are_indexed_by_section_for_the_body_extractor(rows, stub_body, tmp_path):
    calls = stub_body()
    rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=4), TOC, 0, 1, {}, {}, {},
        unnumbered_formulas=[uf(0), uf(1), uf(1)],
    )
    assert len(calls[0]["unnumbered_formulas"]) == 1
    assert len(calls[1]["unnumbered_formulas"]) == 2


def test_a_formula_with_no_section_is_dropped_rather_than_misplaced(rows, stub_body, tmp_path):
    """`_section_idx_for_page` returns None when it cannot decide; guessing would be worse."""
    calls = stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=4), TOC, 0, 0, {}, {}, {},
        unnumbered_formulas=[uf(None)],
    )
    assert calls[0]["unnumbered_formulas"] == []
    assert len(out) == 1


def test_a_formula_no_text_anchor_matched_is_appended_as_a_standalone_row(rows, stub_body, tmp_path):
    """The anchor match is best-effort; without this flush the equation vanishes silently."""
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=4), TOC, 0, 0, {}, {}, {},
        unnumbered_formulas=[uf(0, eqn_number="(7)", latex="a=b")],
    )
    assert out[-1]["text"] == ""
    assert out[-1]["section_num"] == "4"
    assert out[-1]["inline_images"] == [
        {"type": "formula", "data": b"png", "caption": "(7)", "latex": "a=b"}
    ]


def test_a_formula_already_placed_by_the_body_extractor_is_not_flushed_again(rows, stub_body, tmp_path):
    """`formula_emitted` is the shared ledger; the flush must respect what body consumed."""
    stub_body(on_call=lambda kw: kw["formula_emitted"].add(0))
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=4), TOC, 0, 0, {}, {}, {},
        unnumbered_formulas=[uf(0)],
    )
    assert len(out) == 1


def test_a_formula_belonging_to_an_unselected_section_is_never_flushed(rows, stub_body, tmp_path):
    """It would otherwise appear in a report whose page range does not contain it."""
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=5), TOC, 0, 0, {}, {}, {},
        unnumbered_formulas=[uf(2)],
    )
    assert len(out) == 1


def test_a_flushed_formula_with_no_equation_number_gets_an_empty_caption(rows, stub_body, tmp_path):
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "u.pdf", pages=4), TOC, 0, 0, {}, {}, {},
        unnumbered_formulas=[uf(0, eqn_number=None)],
    )
    assert out[-1]["inline_images"][0]["caption"] == ""
    assert out[-1]["inline_images"][0]["latex"] == ""


# ── extract_all_rows: table flush ────────────────────────────────────────────


def test_an_unemitted_table_is_placed_at_the_end_of_the_section_owning_its_page(rows, stub_body, tmp_path):
    """Page-based placement beats reference-based: the TOC end page is often one short."""
    stub_body({"4": [content_row("4", "four body")], "5": [content_row("5", "five body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 1, {}, {"3.1": b"img"}, {},
        media_first_page={"table:3.1": 0},
    )
    assert [r["text"] for r in out] == [
        "4 General requirements", "four body", "Table 3.1", "5 Test methods", "five body",
    ]


def test_a_page_placed_flush_row_inherits_the_section_and_level_of_the_row_above(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "four body", level=3)]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 0, {}, {"3.1": b"img"}, {},
        media_first_page={"table:3.1": 0},
    )
    assert (out[-1]["section_num"], out[-1]["level"]) == ("4", 3)


def test_the_latest_section_starting_on_or_before_the_capture_page_claims_the_table(rows, stub_body, tmp_path):
    """Section 5 starts on the capture page, so it owns it, not the earlier section 4."""
    stub_body({"4": [content_row("4", "four body")], "5": [content_row("5", "five body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 1, {}, {"3.1": b"img"}, {},
        media_first_page={"table:3.1": 2},
    )
    assert [r["text"] for r in out][-1] == "Table 3.1"


def test_without_a_capture_page_the_table_lands_after_the_first_row_referencing_it(rows, stub_body, tmp_path):
    stub_body(
        {
            "4": [content_row("4", "no mention here")],
            "5": [content_row("5", "as given in Table 3.1"), content_row("5", "later text")],
        }
    )
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 1, {}, {"3.1": b"img"}, {}
    )
    assert [r["text"] for r in out].index("Table 3.1") == 4


def test_an_unreferenced_table_with_no_capture_page_is_appended_at_the_very_end(rows, stub_body, tmp_path):
    """Last resort: wrong position beats losing the table entirely."""
    stub_body({"4": [content_row("4", "no mention")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 0, {}, {"3.1": b"img"}, {}
    )
    assert out[-1]["text"] == "Table 3.1"
    assert out[-1]["section_num"] == "4"


def test_a_table_captured_outside_the_selected_range_is_dropped_by_the_flush(rows, stub_body, tmp_path):
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=8), TOC, 0, 0, {}, {"3.1": b"img"}, {},
        media_first_page={"table:3.1": 7},
    )
    assert [r["text"] for r in out] == ["4 General requirements"]


def test_a_table_the_body_already_emitted_is_not_flushed(rows, stub_body, tmp_path):
    stub_body(on_call=lambda kw: kw["emitted"].add("table:3.1"))
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 0, {}, {"3.1": b"img"}, {}
    )
    assert len(out) == 1


def test_a_flushed_multi_page_table_keeps_one_inline_image_per_page_slice(rows, stub_body, tmp_path):
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "t.pdf", pages=4), TOC, 0, 0, {}, {"3.1": [b"a", b"b"]}, {}
    )
    assert [i["data"] for i in out[-1]["inline_images"]] == [b"a", b"b"]
    assert all(i["caption"] == "" for i in out[-1]["inline_images"])


# ── extract_all_rows: figure flush ───────────────────────────────────────────


BIG = b"x" * 2500


def test_an_unemitted_figure_is_placed_by_the_page_it_was_captured_on(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "four body")], "5": [content_row("5", "five body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 1, {2: BIG}, {}, {},
        media_first_page={"figure:2": 0},
    )
    assert [r["text"] for r in out].index("Figure 2") == 2


def test_a_figure_with_no_capture_page_falls_back_to_its_first_text_reference(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "no mention"), content_row("4", "shown in Fig. 2 above")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {2: BIG}, {}, {}
    )
    assert out[-1]["text"] == "Figure 2"
    assert out[2]["text"] == "shown in Fig. 2 above"


def test_an_unplaceable_positive_figure_is_appended_rather_than_dropped(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "no mention at all")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {2: BIG}, {}, {}
    )
    assert out[-1]["text"] == "Figure 2"
    assert out[-1]["inline_images"][0]["number"] == 2


def test_an_image_under_two_kilobytes_is_treated_as_noise_and_skipped(rows, stub_body, tmp_path):
    """Rules, logos and separator hairlines are extracted as images on nearly every page."""
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {2: b"tiny"}, {}, {},
        media_first_page={"figure:2": 0},
    )
    assert len(out) == 1


def test_the_size_floor_is_measured_on_disk_when_the_figure_is_a_file(rows, stub_body, tmp_path):
    """The persisted path stores figures as files, so `len()` would measure the filename."""
    small = tmp_path / "small.png"
    small.write_bytes(b"tiny")
    big = tmp_path / "big.png"
    big.write_bytes(BIG)
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0,
        {1: str(small), 2: str(big)}, {}, {},
        media_first_page={"figure:1": 0, "figure:2": 0},
    )
    assert [r["text"] for r in out if r["type"] == "content"] == ["Figure 2"]


def test_a_figure_captured_outside_the_selected_range_is_dropped_by_the_flush(rows, stub_body, tmp_path):
    stub_body()
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=8), TOC, 0, 0, {2: BIG}, {}, {},
        media_first_page={"figure:2": 7},
    )
    assert len(out) == 1


def test_a_negatively_keyed_figure_is_placed_only_when_its_page_is_known(rows, stub_body, tmp_path):
    """Negative keys are caption-less captures: there is no reference text to search for."""
    stub_body({"4": [content_row("4", "four body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {-3: BIG}, {}, {},
        media_first_page={"figure:-3": 0},
    )
    assert out[-1]["text"] == "Figure 3"
    assert out[-1]["inline_images"][0]["number"] == -3


def test_a_negatively_keyed_figure_with_no_page_is_skipped_not_appended(rows, stub_body, tmp_path):
    """Appending it would invent a "Figure 3" at the end of a report that has no figure 3."""
    stub_body({"4": [content_row("4", "four body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {-3: BIG}, {}, {}
    )
    assert [r["text"] for r in out] == ["4 General requirements", "four body"]


def test_a_figure_the_body_already_emitted_is_not_flushed(rows, stub_body, tmp_path):
    stub_body(on_call=lambda kw: kw["emitted"].add("figure:2"))
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "f.pdf", pages=4), TOC, 0, 0, {2: BIG}, {}, {}
    )
    assert len(out) == 1


def test_tables_are_flushed_before_figures_so_a_table_never_splits_a_figure_row(rows, stub_body, tmp_path):
    stub_body({"4": [content_row("4", "four body")]})
    out = rows.extract_all_rows(
        build_pdf(tmp_path / "o.pdf", pages=4), TOC, 0, 0, {2: BIG}, {"3.1": b"t"}, {},
        media_first_page={"figure:2": 0, "table:3.1": 0},
    )
    assert [r["text"] for r in out][-2:] == ["Table 3.1", "Figure 2"]


# ── _annex_letter ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("base", ["Annex B", "annex b", "Annex\xa0B", "Annex B (informative)"])
def test_an_annex_heading_yields_its_letter_in_upper_case(rows, base):
    """The non-breaking space is what ISO's own PDFs use, so both spellings must work."""
    assert rows._annex_letter(base) == "B"


@pytest.mark.parametrize("base", ["7", "7.4", "", None, "Appendix B", "AnnexB"])
def test_anything_that_is_not_an_annex_heading_yields_no_letter(rows, base):
    assert rows._annex_letter(base) is None


# ── _is_descendant_num ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "new_num,base_num",
    [("B.1", "Annex B"), ("B.7.2.1", "Annex B"), ("7.32.1", "7"), ("7.32.1", "7.4")],
)
def test_a_deeper_number_under_the_same_clause_is_a_descendant(rows, new_num, base_num):
    assert rows._is_descendant_num(new_num, base_num) is True


@pytest.mark.parametrize(
    "new_num,base_num",
    [("C.1", "Annex B"), ("7.32.1", "8"), ("B.1", "7"), ("8", "7"), ("7", "7")],
)
def test_a_sibling_or_a_bare_clause_number_is_not_a_descendant(rows, new_num, base_num):
    """"7" under "7" matters: the section's own number is not an inline subsection of itself."""
    assert rows._is_descendant_num(new_num, base_num) is False


@pytest.mark.parametrize("args", [("", "7"), ("7.1", ""), (None, "7"), ("7.1", None)])
def test_a_missing_number_on_either_side_is_never_a_descendant(rows, args):
    assert rows._is_descendant_num(*args) is False


# ── _detect_inline_subsection ────────────────────────────────────────────────


def test_a_short_numbered_heading_inside_an_annex_body_is_detected(rows):
    """The TOC often has one "Annex B" entry covering B.1..B.n, so col0 comes from here."""
    assert rows._detect_inline_subsection("B.1 Shape and dimensions", "Annex B") == "B.1"


def test_a_deeply_numbered_inline_heading_is_detected(rows):
    assert rows._detect_inline_subsection("B.7.2.1 Tolerances", "Annex B") == "B.7.2.1"


def test_a_numbered_heading_split_over_two_lines_is_detected(rows):
    assert rows._detect_inline_subsection("B.1\nShape and dimensions", "Annex B") == "B.1"


def test_a_bare_number_with_no_title_is_still_a_heading(rows):
    assert rows._detect_inline_subsection("7.4.1", "7") == "7.4.1"


def test_a_heading_number_from_a_different_annex_is_not_this_sections_subsection(rows):
    assert rows._detect_inline_subsection("C.1 Elsewhere", "Annex B") is None


def test_text_that_does_not_start_with_a_number_is_not_an_inline_heading(rows):
    assert rows._detect_inline_subsection("Shape and dimensions", "Annex B") is None


def test_a_block_of_more_than_four_lines_is_body_text_not_a_heading(rows):
    """Headings are one or two lines; five means the number began a paragraph."""
    text = "B.1 Title\nline two\nline three\nline four\nline five"
    assert rows._detect_inline_subsection(text, "Annex B") is None


def test_a_block_longer_than_two_hundred_characters_is_body_text(rows):
    assert rows._detect_inline_subsection("B.1 " + "word " * 60, "Annex B") is None


def test_a_cross_reference_opening_a_long_sentence_is_not_mistaken_for_a_heading(rows):
    """"7.4.5 specifies the requirements ..." is the classic false positive."""
    text = "7.4.5 specifies the requirements for the material and its surface finish here."
    assert rows._detect_inline_subsection(text, "7") is None


def test_a_short_sentence_ending_in_a_full_stop_is_still_accepted_as_a_heading(rows):
    """The terminal-punctuation rule only fires past 60 characters, by design."""
    assert rows._detect_inline_subsection("B.1 General.", "Annex B") == "B.1"


def test_empty_text_is_not_an_inline_heading(rows):
    assert rows._detect_inline_subsection("", "Annex B") is None
    assert rows._detect_inline_subsection(None, "Annex B") is None


def test_leading_whitespace_before_the_number_is_tolerated(rows):
    assert rows._detect_inline_subsection("   B.1 Title", "Annex B") == "B.1"


# ── _is_stop_heading ─────────────────────────────────────────────────────────


def test_a_numbered_heading_that_starts_a_later_section_stops_the_current_one(rows):
    assert rows._is_stop_heading("8.3.1 Surface finish", {"8.3.1"}) is True


def test_a_bare_section_number_on_its_own_line_stops_the_section(rows):
    """Some PDFs put the number and title in separate blocks entirely."""
    assert rows._is_stop_heading("8.3.1", {"8.3.1"}) is True


def test_an_annex_heading_stops_the_section(rows):
    assert rows._is_stop_heading("Annex A Test methods", {"Annex A"}) is True


def test_a_number_that_belongs_to_no_later_section_does_not_stop_anything(rows):
    """Otherwise a cross-reference at the top of a block would truncate the section."""
    assert rows._is_stop_heading("8.3.1 Surface finish", {"9"}) is False


def test_an_unnumbered_heading_is_matched_against_the_full_toc_titles(rows):
    """ISO front matter ("Scope", "Classification") has no number to match on."""
    assert rows._is_stop_heading("body line\nClassification", set(), {"Classification"}) is True


def test_a_number_and_title_split_across_two_lines_are_joined_before_matching(rows):
    assert rows._is_stop_heading("1\nScope", set(), {"1 Scope"}) is True


def test_a_two_line_heading_is_caught_by_joining_the_first_two_lines(rows):
    """The line-break position is a typesetting accident and cannot be predicted."""
    assert rows._is_stop_heading("Shape and\ndimensions", set(), {"Shape and dimensions"}) is True


def test_a_heading_broken_over_three_lines_is_matched_only_after_full_collapsing(rows):
    """The two-line join cannot reach this; only collapsing every whitespace run does.

    Long titles in ISO tables of contents wrap twice on the page, so this is the case that
    makes check 4 more than a duplicate of check 3.
    """
    assert (
        rows._is_stop_heading(
            "Shape and\ndimensions of\nthe finished part",
            set(),
            {"Shape and dimensions of the finished part"},
        )
        is True
    )


def test_a_collapsed_heading_matches_after_its_leading_number_is_stripped(rows):
    """The TOC title may omit the number that the page body carries, or the reverse."""
    assert rows._is_stop_heading("3.1 General\nrequirements", set(), {"General requirements"}) is True


def test_a_title_that_matches_nothing_does_not_stop_the_section(rows):
    assert rows._is_stop_heading("Some ordinary sentence.", set(), {"Scope"}) is False


def test_a_blank_block_is_never_a_stop_heading(rows):
    """Guards the `lines[0]` index; blank blocks reach here from every page."""
    assert rows._is_stop_heading("   \n\n  ", {"8"}) is False


def test_stop_titles_are_optional(rows):
    assert rows._is_stop_heading("Scope", {"8"}) is False
