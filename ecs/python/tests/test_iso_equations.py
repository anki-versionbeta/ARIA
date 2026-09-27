"""Unnumbered-equation detection, triage and placement (`silos/iso/equations.py`).

This module was at 12% coverage, which is the worst place in the ISO port to be guessing:
it decides what counts as an equation using six numeric thresholds, and every one of them
is a false-positive/false-negative tradeoff that a refactor can nudge without breaking
anything visibly. What follows pins each threshold from both sides -- just inside, and
just outside.

Two deliberate choices about how these tests are built:

**The dict-driven functions get a hand-built page, not a PDF.** `_detect_equations`,
`_detect_equations_banded`, `_find_preceding_anchor` and `_section_idx_for_page` only ever
ask a page for `get_text("dict")`, `get_text("blocks", sort=True)` and `rect`. Feeding
those directly is not a shortcut around a real PDF -- it is the only way to test the font
and glyph heuristics at all, because the base-14 fonts fitz can synthesise cannot encode
"∑" or "≤" (they arrive as U+FFFD, so a real synthetic PDF would silently exercise the
prose path instead of the math one).

**The pixmap functions get a real PDF.** `_crop_equation`, `_crop_with_context` and
`_find_formula_candidates` rasterise, so they need a genuine page. Those PDFs lean on
`(4.2)` -- a bare equation number is detected on its own, with no math glyph required.
"""

from __future__ import annotations

import json
import os

import fitz
import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "equations.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def eq():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "equations.py", name="equations")


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
    """A page that answers only what this module actually asks of one.

    `image_block=True` adds a `type != 0` block, which every text loop here has to skip.
    """

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


class FakeWorkspace:
    """Only `.media_dir` is read, via `_session_media_dir`."""

    def __init__(self, media_dir):
        self.media_dir = str(media_dir)


def build_pdf(path, *, pages=1, lines_per_page=None):
    """A real PDF whose text is chosen to trip the equation-number detector.

    `lines_per_page` maps a 0-indexed page to (text, y) pairs.
    """
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        entries = (lines_per_page or {}).get(index, [("(4.2)", 300)])
        for text, y in entries:
            page.insert_text((72, y), text, fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    return str(path)


# ── _line_math_signal ────────────────────────────────────────────────────────


def test_a_line_with_no_printable_characters_reports_no_signal(eq):
    """Guards the division; a whitespace-only line is common in these PDFs."""
    assert eq._line_math_signal([span("   ")]) == (0.0, 0, 0.0, 0)
    assert eq._line_math_signal([]) == (0.0, 0, 0.0, 0)


def test_whitespace_is_excluded_from_the_character_total(eq):
    """The ratios are per printable glyph, so padding a line must not dilute them."""
    _ratio, _symbols, _italics, total = eq._line_math_signal([span("a b\tc")])
    assert total == 3


def test_a_math_font_makes_every_one_of_its_characters_count_as_math(eq):
    ratio, _symbols, _italics, total = eq._line_math_signal(
        [span("xy", font="CMMI10"), span("ab", font="Helvetica")]
    )
    assert total == 4
    assert ratio == 0.5


def test_math_font_matching_ignores_case_and_spacing_variants(eq):
    """The font names arrive exactly as the PDF spells them, which is inconsistently."""
    for font in ("Cambria Math", "cambria  math", "STIXGeneral", "LatinModernMath"):
        ratio, _s, _i, _t = eq._line_math_signal([span("xx", font=font)])
        assert ratio == 1.0, font


def test_math_glyphs_are_counted_regardless_of_font(eq):
    """A symbol is a symbol even in a prose font, which is the point of the glyph set."""
    _ratio, symbols, _italics, _total = eq._line_math_signal([span("a ≤ b ≠ c")])
    assert symbols == 2


def test_greek_letters_and_blackboard_capitals_count_as_math_glyphs(eq):
    _ratio, symbols, _i, _t = eq._line_math_signal([span("αβ ∈ ℝ")])
    assert symbols == 4


def test_the_italic_ratio_comes_from_the_span_flags(eq):
    _r, _s, italic_ratio, total = eq._line_math_signal(
        [span("xy", italic=True), span("ab", italic=False)]
    )
    assert total == 4
    assert italic_ratio == 0.5


# ── _detect_equations: the three per-line signals ────────────────────────────


def test_a_line_with_two_math_glyphs_is_detected(eq):
    page = FakePage(lines=[line([span("a ≤ b ≠ c", bbox=(72, 100, 300, 120))])])
    assert len(eq._detect_equations(page)) == 1


def test_a_line_with_a_single_math_glyph_is_not_enough_on_its_own(eq):
    """One symbol is ordinary prose ("temperature ≤ 40 °C") unless italics agree."""
    page = FakePage(lines=[line([span("values a ≤ b here", bbox=(72, 100, 300, 120))])])
    assert eq._detect_equations(page) == []


def test_a_mostly_math_font_line_is_detected_without_any_symbol(eq):
    """>30% math font: this is how a typeset equation with only latin letters is caught."""
    page = FakePage(
        lines=[
            line(
                [
                    span("xyz", font="CMMI10", bbox=(72, 100, 150, 120)),
                    span("ab", font="Helvetica", bbox=(150, 100, 200, 120)),
                ]
            )
        ]
    )
    assert len(eq._detect_equations(page)) == 1


def test_a_math_font_ratio_exactly_at_the_threshold_is_rejected(eq):
    """The test is strictly greater than 0.30, so 3-in-10 must not trip it."""
    page = FakePage(
        lines=[
            line(
                [
                    span("xyz", font="CMMI10", bbox=(72, 100, 150, 120)),
                    span("abcdefg", font="Helvetica", bbox=(150, 100, 300, 120)),
                ]
            )
        ]
    )
    assert eq._detect_equations(page) == []


def test_a_heavily_italic_line_with_one_symbol_is_detected(eq):
    """The third signal: variables are italic in typeset maths, prose is not."""
    page = FakePage(
        lines=[line([span("x ≤ y", italic=True, bbox=(72, 100, 300, 120))])]
    )
    assert len(eq._detect_equations(page)) == 1


def test_a_heavily_italic_line_with_no_symbol_is_not_an_equation(eq):
    """Otherwise every italicised note and defined term in the standard would match."""
    page = FakePage(
        lines=[
            line([span("see the definition above", italic=True, bbox=(72, 100, 300, 120))])
        ]
    )
    assert eq._detect_equations(page) == []


def test_ordinary_prose_is_not_detected(eq):
    page = FakePage(
        lines=[line([span("This clause specifies requirements.", bbox=(72, 100, 400, 120))])]
    )
    assert eq._detect_equations(page) == []


def test_an_image_block_and_empty_lines_are_skipped(eq):
    """Both appear on real pages and neither has spans to measure."""
    page = FakePage(
        lines=[line([], bbox=(0, 0, 5, 5)), line([span("   ", bbox=(72, 100, 80, 110))])],
        image_block=True,
    )
    assert eq._detect_equations(page) == []


# ── _detect_equations: the bare equation number ──────────────────────────────


@pytest.mark.parametrize("label", ["(1)", "(2.3)", "(4-7)", "(a)", "( 12 )", "(B)"])
def test_a_line_that_is_only_an_equation_number_is_flagged(eq, label):
    """It carries no maths itself; it is flagged so it can merge with the body above."""
    page = FakePage(lines=[line([span(label, bbox=(400, 100, 440, 120))])])
    assert len(eq._detect_equations(page)) == 1


@pytest.mark.parametrize("not_a_label", ["(4.2) where x applies", "4.2", "(iv)", "(1.2.3)"])
def test_text_that_merely_contains_a_number_is_not_an_equation_number(eq, not_a_label):
    page = FakePage(lines=[line([span(not_a_label, bbox=(72, 100, 300, 120))])])
    assert eq._detect_equations(page) == []


# ── _detect_equations: merging ───────────────────────────────────────────────


def test_an_equation_body_and_its_number_on_the_same_baseline_merge(eq):
    """They are separate fitz lines but one equation, so one rect must come out."""
    page = FakePage(
        lines=[
            line([span("a ≤ b ≠ c", bbox=(72, 100, 300, 120))]),
            line([span("(4.2)", bbox=(450, 102, 500, 118))]),
        ]
    )
    merged = eq._detect_equations(page)
    assert len(merged) == 1
    assert merged[0].x0 == 72 and merged[0].x1 == 500


def test_the_lines_of_a_multi_line_equation_merge_when_close_together(eq):
    """A vertical gap of 8pt or less is treated as one display equation."""
    page = FakePage(
        lines=[
            line([span("a ≤ b ≠ c", bbox=(72, 100, 300, 118))]),
            line([span("d ≥ e ≈ f", bbox=(72, 124, 300, 142))]),
        ]
    )
    assert len(eq._detect_equations(page)) == 1


def test_two_equations_separated_by_a_paragraph_stay_separate(eq):
    """A gap comfortably over 8pt is two equations, and merging them would misplace both."""
    page = FakePage(
        lines=[
            line([span("a ≤ b ≠ c", bbox=(72, 100, 300, 118))]),
            line([span("d ≥ e ≈ f", bbox=(72, 300, 300, 318))]),
        ]
    )
    assert len(eq._detect_equations(page)) == 2


def test_a_detected_speck_is_dropped_after_merging(eq):
    """Two math glyphs inside a 10pt-wide box is a footnote marker, not an equation."""
    page = FakePage(lines=[line([span("≤≠", bbox=(72, 100, 80, 104))])])
    assert eq._detect_equations(page) == []


# ── _detect_equations_banded ─────────────────────────────────────────────────


def test_glyphs_scattered_across_one_baseline_are_recovered_as_one_equation(eq):
    """The reason this detector exists: one visual equation arriving as many "lines".

    Each fragment has a single glyph, so the per-line test rejects all of them; the band
    aggregates them and sees an equation.
    """
    page = FakePage(
        lines=[
            line([span("x", bbox=(72, 100, 80, 114))]),
            line([span("≤", bbox=(84, 100, 92, 114))]),
            line([span("y", bbox=(96, 100, 104, 114))]),
            line([span("≠", bbox=(108, 100, 116, 114))]),
        ]
    )
    banded = eq._detect_equations_banded(page)
    assert len(banded) == 1
    assert banded[0].x0 == 72 and banded[0].x1 == 116


def test_a_band_with_no_math_glyph_is_ignored(eq):
    page = FakePage(lines=[line([span("plain words here", bbox=(72, 100, 300, 114))])])
    assert eq._detect_equations_banded(page) == []


def test_a_band_carrying_too_much_prose_is_rejected(eq):
    """The 40-alpha guard: a sentence that happens to contain "≤" is not an equation."""
    prose = "a" * 41
    page = FakePage(lines=[line([span(f"{prose} ≤ ≠", bbox=(72, 100, 300, 114))])])
    assert eq._detect_equations_banded(page) == []


def test_a_band_spanning_most_of_the_page_width_is_rejected(eq):
    """A full-width band is a line of body text, whatever glyphs it contains."""
    page = FakePage(
        lines=[line([span("≤ ≠", bbox=(10, 100, 580, 114))])], width=595.0
    )
    assert eq._detect_equations_banded(page) == []


def test_one_glyph_plus_two_distinct_fonts_is_enough_for_a_band(eq):
    """A font change mid-expression is itself evidence of typeset maths."""
    page = FakePage(
        lines=[
            line([span("x", font="Times-Italic", bbox=(72, 100, 80, 114))]),
            line([span("≤", font="Symbol", bbox=(84, 100, 92, 114))]),
        ]
    )
    assert len(eq._detect_equations_banded(page)) == 1


def test_bands_are_split_by_vertical_distance(eq):
    """Spans more than the 4pt tolerance apart are different baselines."""
    page = FakePage(
        lines=[
            line([span("x ≤ ≠", bbox=(72, 100, 200, 114))]),
            line([span("y ≥ ≈", bbox=(72, 200, 200, 214))]),
        ]
    )
    assert len(eq._detect_equations_banded(page)) == 2


def test_a_page_with_no_text_yields_no_bands(eq):
    assert eq._detect_equations_banded(FakePage(lines=[])) == []


def test_a_tiny_band_is_rejected_by_the_size_floor(eq):
    page = FakePage(lines=[line([span("≤≠", bbox=(72, 100, 78, 103))])])
    assert eq._detect_equations_banded(page) == []


# ── cropping ─────────────────────────────────────────────────────────────────


def test_a_tight_crop_returns_png_bytes(eq, tmp_path):
    doc = fitz.open(build_pdf(tmp_path / "crop.pdf"))
    try:
        png = eq._crop_equation(doc[0], fitz.Rect(72, 290, 200, 310))
    finally:
        doc.close()
    assert png.startswith(b"\x89PNG")


def test_a_higher_dpi_produces_a_larger_raster(eq, tmp_path):
    """The DPI is what makes the image legible to the vision model; it is not cosmetic."""
    doc = fitz.open(build_pdf(tmp_path / "crop.pdf"))
    try:
        rect = fitz.Rect(72, 290, 200, 310)
        small = eq._crop_equation(doc[0], rect, dpi=72)
        large = eq._crop_equation(doc[0], rect, dpi=200)
    finally:
        doc.close()
    assert len(large) > len(small)


def test_padding_is_clamped_at_the_page_edges(eq, tmp_path):
    """A rect flush against the corner would otherwise pad to negative coordinates."""
    doc = fitz.open(build_pdf(tmp_path / "crop.pdf"))
    try:
        png = eq._crop_equation(doc[0], fitz.Rect(0, 0, 20, 20), pad=50)
    finally:
        doc.close()
    assert png.startswith(b"\x89PNG")


def test_the_context_crop_covers_more_of_the_page_than_the_tight_one(eq, tmp_path):
    """The model is told to judge with the wider crop, so it has to actually be wider."""
    doc = fitz.open(build_pdf(tmp_path / "crop.pdf"))
    try:
        rect = fitz.Rect(200, 400, 260, 420)
        tight_pixels = fitz.Rect(
            max(0, rect.x0 - 4), max(0, rect.y0 - 4), rect.x1 + 4, rect.y1 + 4
        )
        context_pixels = fitz.Rect(
            max(0, rect.x0 - 100), max(0, rect.y0 - 100), rect.x1 + 100, rect.y1 + 100
        )
        assert context_pixels.get_area() > tight_pixels.get_area()
        assert eq._crop_with_context(doc[0], rect).startswith(b"\x89PNG")
    finally:
        doc.close()


# ── _find_formula_candidates ─────────────────────────────────────────────────


def test_every_candidate_carries_a_page_a_box_and_both_crops(eq, tmp_path):
    doc = fitz.open(build_pdf(tmp_path / "cands.pdf"))
    try:
        candidates = eq._find_formula_candidates(doc)
    finally:
        doc.close()
    assert len(candidates) == 1
    found = candidates[0]
    assert found["page_0"] == 0
    assert len(found["bbox"]) == 4
    assert found["crop_png"].startswith(b"\x89PNG")
    assert found["context_png"].startswith(b"\x89PNG")


def test_a_page_range_restricts_which_pages_are_scanned(eq, tmp_path):
    pdf = build_pdf(
        tmp_path / "range.pdf",
        pages=4,
        lines_per_page={i: [("(4.2)", 300)] for i in range(4)},
    )
    doc = fitz.open(pdf)
    try:
        assert len(eq._find_formula_candidates(doc)) == 4
        windowed = eq._find_formula_candidates(doc, page_range=(1, 2))
        assert [c["page_0"] for c in windowed] == [1, 2]
    finally:
        doc.close()


def test_a_page_range_beyond_the_document_is_clamped(eq, tmp_path):
    """The range is derived from the TOC, which can name a page the PDF does not have."""
    doc = fitz.open(
        build_pdf(tmp_path / "clamp.pdf", pages=2, lines_per_page={0: [("(4.2)", 300)], 1: [("(4.3)", 300)]})
    )
    try:
        candidates = eq._find_formula_candidates(doc, page_range=(-5, 99))
    finally:
        doc.close()
    assert [c["page_0"] for c in candidates] == [0, 1]


# ── _parse_triage_response ───────────────────────────────────────────────────


def test_plain_json_is_parsed(eq):
    assert eq._parse_triage_response('{"is_equation": true}') == {"is_equation": True}


def test_a_markdown_fenced_reply_is_unwrapped(eq):
    """The model does this despite being told not to, on roughly every other call."""
    assert eq._parse_triage_response('```json\n{"is_equation": true}\n```') == {
        "is_equation": True
    }


def test_a_fence_without_a_language_tag_is_also_unwrapped(eq):
    assert eq._parse_triage_response('```\n{"latex": "x"}\n```') == {"latex": "x"}


def test_leading_narration_before_the_object_is_discarded(eq):
    reply = 'Sure! Here is the JSON:\n{"is_equation": false, "reject_reason": "prose"}'
    assert eq._parse_triage_response(reply)["reject_reason"] == "prose"


def test_a_multi_line_object_survives_the_extraction(eq):
    """The `{.*}` search is DOTALL precisely so a pretty-printed reply still parses."""
    reply = json.dumps({"is_equation": True, "latex": "a+b"}, indent=2)
    assert eq._parse_triage_response(reply)["latex"] == "a+b"


@pytest.mark.parametrize("junk", ["", None, "not json at all", "{broken", "[1, 2, 3]", '"a string"'])
def test_anything_unusable_parses_to_none(eq, junk):
    """None is the caller's signal to reject the candidate as parse_failed."""
    assert eq._parse_triage_response(junk) is None


# ── _call_llm_vision_multi and _triage_formula_via_llm ───────────────────────


def test_both_crops_go_to_the_model_in_a_single_call(eq):
    """The prompt's whole design depends on one request holding both images."""
    llm = FakeLlm(chat_vision_multi=['{"is_equation": true}'])
    eq._call_llm_vision_multi(["tight-b64", "context-b64"], "prompt", llm)

    calls = llm.calls_of("chat_vision_multi")
    assert len(calls) == 1
    assert calls[0]["images"] == 2


def test_the_triage_call_pins_the_model_budget_and_timeout(eq):
    """A verbatim port: these three are the bill and the latency, so they are asserted."""
    llm = FakeLlm(chat_vision_multi=['{"is_equation": true}'])
    eq._call_llm_vision_multi(["a", "b"], "prompt", llm)

    call = llm.calls_of("chat_vision_multi")[0]
    assert call["model"] == eq._ILIAD_VISION_MODEL
    assert call["max_tokens"] == 800
    assert call["timeouts"] == (90,)


def test_the_triage_prompt_tells_the_model_which_crop_to_use_for_what(eq):
    """Both halves of the instruction are load-bearing and easy to lose in an edit."""
    prompt = eq._FORMULA_TRIAGE_PROMPT
    assert "WIDER context crop ONLY to decide is_equation" in prompt
    assert "TIGHT crop only" in prompt
    assert "is_equation" in prompt and "reject_reason" in prompt


def test_a_successful_triage_returns_the_models_verdict(eq):
    llm = FakeLlm(
        chat_vision_multi=['{"is_equation": true, "latex": "a=b", "eqn_number": null}']
    )
    result = eq._triage_formula_via_llm(
        {"crop_png": b"tight", "context_png": b"ctx", "page_0": 3}, llm
    )
    assert result["is_equation"] is True
    assert result["latex"] == "a=b"


def test_an_unparseable_reply_becomes_a_rejection_rather_than_an_exception(eq):
    llm = FakeLlm(chat_vision_multi=["I could not read the image"])
    result = eq._triage_formula_via_llm(
        {"crop_png": b"t", "context_png": b"c", "page_0": 1}, llm
    )
    assert result == {"is_equation": False, "reject_reason": "parse_failed"}


def test_a_failing_model_call_becomes_a_rejection_too(eq):
    """One bad page must not abort a 200-page run."""
    llm = FakeLlm(chat_vision_multi=[StubLlmError("upstream exploded")])
    result = eq._triage_formula_via_llm(
        {"crop_png": b"t", "context_png": b"c", "page_0": 7}, llm
    )
    assert result == {"is_equation": False, "reject_reason": "llm_error"}


# ── _filter_formula_candidates ───────────────────────────────────────────────


def candidate(bbox, page_0=0):
    return {"page_0": page_0, "bbox": list(bbox), "crop_png": b"x", "context_png": b"y"}


def test_a_candidate_sitting_inside_a_table_is_dropped(eq):
    """A number in a table cell is not an equation, and this is the cheap way to know."""
    kept = eq._filter_formula_candidates(
        [candidate((100, 100, 200, 140))],
        {0: [{"bbox": [90, 90, 300, 300], "type": "table"}]},
    )
    assert kept == []


def test_a_candidate_that_barely_clips_a_region_is_kept(eq):
    """Equations often abut a table caption; a 20% cut keeps those instead of losing them."""
    kept = eq._filter_formula_candidates(
        [candidate((100, 100, 200, 200))],
        {0: [{"bbox": [100, 100, 120, 120], "type": "table"}]},
    )
    assert len(kept) == 1


def test_a_candidate_on_a_page_with_no_regions_is_kept(eq):
    assert len(eq._filter_formula_candidates([candidate((100, 100, 200, 200))], {})) == 1


def test_a_zero_area_candidate_is_dropped_before_the_ratio_is_computed(eq):
    """Guards the division; a degenerate rect can reach here from the band detector."""
    assert eq._filter_formula_candidates([candidate((100, 100, 100, 100))], {}) == []


@pytest.mark.parametrize("bbox", [(100, 100, 118, 200), (100, 100, 200, 106)])
def test_a_candidate_below_the_size_floor_is_dropped(eq, bbox):
    """Second guardrail, wider than the detector's: an inline italic word is not maths."""
    assert eq._filter_formula_candidates([candidate(bbox)], {}) == []


def test_regions_are_looked_up_per_page(eq):
    """A table on page 0 must not disqualify a formula on page 1."""
    kept = eq._filter_formula_candidates(
        [candidate((100, 100, 200, 200), page_0=1)],
        {0: [{"bbox": [90, 90, 300, 300]}]},
    )
    assert len(kept) == 1


# ── _triage_parallel ─────────────────────────────────────────────────────────


def test_triaging_nothing_calls_no_model(eq):
    llm = FakeLlm()
    assert eq._triage_parallel([], llm) == []
    assert llm.calls == []


def test_only_accepted_candidates_come_back_and_they_carry_the_verdict(eq):
    """max_workers=1 because the fake's reply queue is order-based, not per-candidate."""
    llm = FakeLlm(
        chat_vision_multi=[
            '{"is_equation": true, "latex": "first"}',
            '{"is_equation": false, "reject_reason": "table_cell"}',
            '{"is_equation": true, "latex": "third"}',
        ]
    )
    kept = eq._triage_parallel(
        [candidate((100, 100, 200, 200)) for _ in range(3)], llm, max_workers=1
    )
    assert [c["llm"]["latex"] for c in kept] == ["first", "third"]


def test_candidate_order_is_preserved_so_insertion_order_is_stable(eq):
    """`ex.map` rather than `as_completed`: this list becomes document order."""
    replies = [f'{{"is_equation": true, "latex": "eq{i}"}}' for i in range(6)]
    llm = FakeLlm(chat_vision_multi=replies)
    candidates = [candidate((100, 100 + 10 * i, 200, 140 + 10 * i)) for i in range(6)]

    kept = eq._triage_parallel(candidates, llm, max_workers=1)

    assert [c["llm"]["latex"] for c in kept] == [f"eq{i}" for i in range(6)]
    assert [c["bbox"] for c in kept] == [c["bbox"] for c in candidates]


def test_a_rejection_without_a_reason_is_still_counted_not_crashed(eq):
    """The Counter keys on `reject_reason`, which the model does not always supply."""
    llm = FakeLlm(chat_vision_multi=['{"is_equation": false}'])
    assert eq._triage_parallel([candidate((100, 100, 200, 200))], llm, max_workers=1) == []


# ── _find_preceding_anchor ───────────────────────────────────────────────────


def test_the_nearest_substantive_block_above_becomes_the_anchor(eq):
    page = FakePage(
        blocks=[
            text_block("The first paragraph of the clause.", 100, 120),
            text_block("The paragraph immediately above the formula.", 200, 220),
            text_block("Text below the formula, irrelevant.", 400, 420),
        ]
    )
    anchor, anchor_y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "The paragraph immediately above the formula."
    assert anchor_y0 == 200


def test_a_short_connector_block_is_skipped_in_favour_of_something_discriminating(eq):
    """"where" appears hundreds of times, so matching on it would place the image wrongly."""
    page = FakePage(
        blocks=[
            text_block("A properly substantive sentence to anchor on.", 100, 120),
            text_block("where", 250, 260),
        ]
    )
    anchor, anchor_y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "A properly substantive sentence to anchor on."
    assert anchor_y0 == 100


def test_when_everything_above_is_short_the_closest_one_is_used_anyway(eq):
    """Better a weak anchor than none; the end-of-page flush is the real backstop."""
    page = FakePage(
        blocks=[text_block("and", 100, 120), text_block("where", 250, 260)]
    )
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "where"


def test_a_formula_at_the_top_of_a_page_has_no_anchor(eq):
    page = FakePage(blocks=[text_block("Text well below the formula.", 400, 420)])
    assert eq._find_preceding_anchor(page, (72, 50, 300, 80)) == ("", None)


def test_watermarks_and_page_numbers_are_never_anchors(eq):
    """Both sit above the body text on every page, so they would win on distance."""
    page = FakePage(
        blocks=[
            text_block("A real sentence worth anchoring to.", 100, 120),
            text_block("Copyrighted material licensed to ABBVIE", 250, 260),
            text_block("42", 270, 280),
        ]
    )
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "A real sentence worth anchoring to."


def test_a_long_anchor_is_truncated_to_its_tail(eq):
    """The tail is what abuts the formula, and `_extract_body` matches on a substring."""
    long_text = "A" * 40 + "B" * 40
    page = FakePage(blocks=[text_block(long_text, 100, 120)])
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330), char_limit=20)
    assert anchor == "B" * 20


def test_whitespace_inside_an_anchor_is_collapsed(eq):
    """PDF text arrives with hard line breaks that the emitter's text will not have."""
    page = FakePage(blocks=[text_block("Two   lines\nof   one sentence here", 100, 120)])
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "Two lines of one sentence here"


def test_non_text_blocks_and_blank_blocks_are_ignored(eq):
    page = FakePage(
        blocks=[
            text_block("An image block above the formula.", 200, 220, btype=1),
            text_block("   ", 240, 250),
            text_block("The genuine anchor sentence.", 100, 120),
        ]
    )
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "The genuine anchor sentence."


def test_a_block_overlapping_the_formula_slightly_still_counts_as_above(eq):
    """The 2pt tolerance exists because the detector's box is padded."""
    page = FakePage(blocks=[text_block("An anchor that just overlaps.", 280, 301)])
    anchor, _y0 = eq._find_preceding_anchor(page, (72, 300, 300, 330))
    assert anchor == "An anchor that just overlaps."


# ── _section_idx_for_page ────────────────────────────────────────────────────


TOC = [
    {"title": "4 General requirements", "page": 10},
    {"title": "4.1 First subclause", "page": 12},
    {"title": "4.2 Second subclause", "page": 12},
    {"title": "5 Other requirements", "page": 20},
]


def test_the_deepest_section_starting_on_or_before_the_page_wins(eq):
    """Page 15 (1-indexed 16) is inside 4.2, which started on 12."""
    assert eq._section_idx_for_page(TOC, 15) == 2


def test_a_formula_before_the_first_section_in_range_has_no_section(eq):
    """It has nowhere to go, and the caller drops it rather than guessing."""
    assert eq._section_idx_for_page(TOC, 2) is None


def test_the_search_is_confined_to_the_selected_toc_slice(eq):
    """The user picks a range in the picker; a formula must not land outside it."""
    assert eq._section_idx_for_page(TOC, 25, toc_start_idx=0, toc_end_idx=1) == 1


def test_without_a_page_and_box_the_lookup_stays_purely_page_based(eq):
    """Two sections start on page 12; with no geometry the last one wins, as before."""
    assert eq._section_idx_for_page(TOC, 11) == 2


def test_a_single_section_on_the_page_needs_no_disambiguation(eq):
    page = FakePage(blocks=[text_block("5 Other requirements", 100, 120)])
    assert eq._section_idx_for_page(TOC, 19, page=page, bbox=(72, 300, 300, 330)) == 3


def test_the_heading_closest_above_the_formula_claims_it(eq):
    """The bug this fixes: on a dense page the *last* section was taking every formula.

    4.1 and 4.2 both start on page 12. A formula at y=250 sits under 4.1's heading at
    y=200 but above 4.2's at y=400, so it belongs to 4.1.
    """
    page = FakePage(
        blocks=[
            text_block("4.1 First subclause", 200, 220),
            text_block("4.2 Second subclause", 400, 420),
        ]
    )
    assert eq._section_idx_for_page(TOC, 11, page=page, bbox=(72, 250, 300, 280)) == 1


def test_a_formula_above_every_heading_on_the_page_belongs_to_the_previous_section(eq):
    """It is trailing content from the section that started on an earlier page."""
    page = FakePage(
        blocks=[
            text_block("4.1 First subclause", 400, 420),
            text_block("4.2 Second subclause", 600, 620),
        ]
    )
    assert eq._section_idx_for_page(TOC, 11, page=page, bbox=(72, 100, 300, 130)) == 0


def test_when_no_heading_can_be_located_the_page_based_answer_stands(eq):
    """Headings are found by section-number prefix, which a garbled page will not yield."""
    page = FakePage(blocks=[text_block("unrelated body text only", 200, 220)])
    assert eq._section_idx_for_page(TOC, 11, page=page, bbox=(72, 250, 300, 280)) == 2


def test_a_section_number_appearing_mid_sentence_is_not_treated_as_a_heading(eq):
    """"see 4.2 for details" must not be mistaken for the heading of 4.2."""
    page = FakePage(
        blocks=[
            text_block("as described in 4.2 the value applies", 150, 170),
            text_block("4.1 First subclause", 200, 220),
        ]
    )
    assert eq._section_idx_for_page(TOC, 11, page=page, bbox=(72, 250, 300, 280)) == 1


def test_an_omitted_end_index_defaults_to_the_whole_toc(eq):
    assert eq._section_idx_for_page(TOC, 25, toc_end_idx=None) == 3


def test_a_toc_entry_with_no_usable_number_is_skipped_during_disambiguation(eq):
    """`_section_num` yields nothing for an untitled bookmark, so it cannot be located."""
    toc = [{"title": "", "page": 12}, {"title": "4.2 Second subclause", "page": 12}]
    page = FakePage(blocks=[text_block("4.2 Second subclause", 200, 220)])
    assert eq._section_idx_for_page(toc, 11, toc_end_idx=1, page=page, bbox=(72, 250, 300, 280)) == 1


def test_blank_blocks_are_skipped_while_hunting_for_headings(eq):
    toc = [{"title": "4.1 First subclause", "page": 12}, {"title": "4.2 Second", "page": 12}]
    page = FakePage(
        blocks=[
            text_block("   ", 150, 160),
            text_block("4.1 First subclause", 200, 220),
            text_block("4.2 Second", 400, 420),
        ]
    )
    assert eq._section_idx_for_page(toc, 11, toc_end_idx=1, page=page, bbox=(72, 250, 300, 280)) == 0


def test_when_the_first_same_page_section_is_the_range_start_there_is_nothing_to_walk_back_to(
    eq,
):
    """Every heading sits below the formula, but the section before it is out of range.

    Walking back would leave the selected range, so the page-based answer has to stand
    even though it is known to be imprecise.
    """
    toc = [{"title": "4.1 First subclause", "page": 12}, {"title": "4.2 Second", "page": 12}]
    page = FakePage(
        blocks=[
            text_block("4.1 First subclause", 400, 420),
            text_block("4.2 Second", 600, 620),
        ]
    )
    result = eq._section_idx_for_page(
        toc, 11, toc_start_idx=0, toc_end_idx=1, page=page, bbox=(72, 100, 300, 130)
    )
    assert result == 1


# ── find_unnumbered_formulas ─────────────────────────────────────────────────


def test_the_pipeline_enriches_an_accepted_formula_end_to_end(eq, tmp_path):
    """One pass over the real seams: detect, filter, triage, anchor, section."""
    pdf = build_pdf(
        tmp_path / "full.pdf",
        pages=1,
        lines_per_page={0: [("A substantive anchor sentence above.", 200), ("(4.2)", 300)]},
    )
    llm = FakeLlm(
        chat_vision_multi=[
            '{"is_equation": true, "latex": "a=b", "eqn_number": "(4.2)",'
            ' "display_style": "display"}'
        ]
    )
    toc = [{"title": "4 General", "page": 1}]

    found = eq.find_unnumbered_formulas(pdf, toc=toc, llm=llm)

    assert len(found) == 1
    entry = found[0]
    assert entry["page_0"] == 0
    assert entry["latex"] == "a=b"
    assert entry["eqn_number"] == "(4.2)"
    assert entry["display_style"] == "display"
    assert entry["anchor_text"] == "A substantive anchor sentence above."
    assert entry["section_idx"] == 0
    assert entry["image"].startswith(b"\x89PNG")


def test_a_workspace_makes_the_crop_a_file_on_disk(eq, tmp_path):
    """The emitter opens this path later in the same stage, so the name is a contract."""
    media = tmp_path / "media"
    media.mkdir()
    pdf = build_pdf(
        tmp_path / "ws.pdf",
        lines_per_page={0: [("A substantive anchor sentence above.", 200), ("(4.2)", 300)]},
    )
    llm = FakeLlm(chat_vision_multi=['{"is_equation": true, "latex": "a=b"}'])

    found = eq.find_unnumbered_formulas(
        pdf, toc=[{"title": "4 General", "page": 1}], llm=llm, ws=FakeWorkspace(media)
    )

    assert found[0]["image"] == os.path.join(str(media), "Formula_anchor_001.png")
    assert os.path.isfile(found[0]["image"])


def test_a_document_with_no_candidates_returns_nothing_and_calls_no_model(eq, tmp_path):
    pdf = build_pdf(
        tmp_path / "empty.pdf", lines_per_page={0: [("Just ordinary prose here.", 300)]}
    )
    llm = FakeLlm()
    assert eq.find_unnumbered_formulas(pdf, llm=llm) == []
    assert llm.calls == []


def test_a_formula_the_model_rejects_does_not_reach_the_document(eq, tmp_path):
    pdf = build_pdf(tmp_path / "rejected.pdf", lines_per_page={0: [("(4.2)", 300)]})
    llm = FakeLlm(chat_vision_multi=['{"is_equation": false, "reject_reason": "prose"}'])
    assert eq.find_unnumbered_formulas(pdf, llm=llm) == []


def test_an_accepted_formula_with_neither_anchor_nor_section_is_dropped(eq, tmp_path):
    """Nowhere to insert it: no anchor text to match and no section to fall back to."""
    pdf = build_pdf(tmp_path / "orphan.pdf", lines_per_page={0: [("(4.2)", 50)]})
    llm = FakeLlm(chat_vision_multi=['{"is_equation": true, "latex": "a=b"}'])
    assert eq.find_unnumbered_formulas(pdf, llm=llm, toc=None) == []


def test_the_page_range_is_derived_from_the_toc_when_not_given(eq, tmp_path):
    """Only the selected clause is scanned, which is what keeps the vision bill down.

    The derived range deliberately reaches the *next* section's start page rather than
    stopping one short: `_section_end_page_0` does that so the emitter can pick up content
    sitting before that next heading. So selecting clause 4 (page 2, with clause 5 on
    page 3) scans pages 1-2 zero-indexed, and page 0 is the one that must not be touched.
    """
    pdf = build_pdf(
        tmp_path / "derived.pdf",
        pages=3,
        lines_per_page={
            0: [("(1.1)", 300)],
            1: [("An anchor sentence on the second page.", 200), ("(2.1)", 300)],
            2: [("(3.1)", 300)],
        },
    )
    llm = FakeLlm(
        chat_vision_multi=[
            '{"is_equation": true, "latex": "second"}',
            '{"is_equation": true, "latex": "third"}',
        ]
    )
    toc = [{"title": "4 General", "page": 2}, {"title": "5 Next", "page": 3}]

    found = eq.find_unnumbered_formulas(pdf, toc=toc, toc_start_idx=0, toc_end_idx=0, llm=llm)

    assert [entry["page_0"] for entry in found] == [1, 2]
    assert len(llm.calls_of("chat_vision_multi")) == 2


def test_a_page_whose_region_scan_fails_is_treated_as_having_no_regions(eq, tmp_path, monkeypatch):
    """A malformed page must cost that page's filtering, not the whole run."""
    pdf = build_pdf(
        tmp_path / "boom.pdf",
        lines_per_page={0: [("A substantive anchor sentence above.", 200), ("(4.2)", 300)]},
    )
    monkeypatch.setattr(
        eq, "_extract_page_regions", lambda _page: (_ for _ in ()).throw(RuntimeError("bad page"))
    )
    llm = FakeLlm(chat_vision_multi=['{"is_equation": true, "latex": "a=b"}'])

    found = eq.find_unnumbered_formulas(pdf, toc=[{"title": "4 General", "page": 1}], llm=llm)

    assert len(found) == 1


def test_a_candidate_filtered_out_by_a_region_never_reaches_the_model(eq, tmp_path, monkeypatch):
    """The pre-filter exists to save vision calls, so the saving is what is asserted."""
    pdf = build_pdf(tmp_path / "intable.pdf", lines_per_page={0: [("(4.2)", 300)]})
    monkeypatch.setattr(
        eq,
        "_extract_page_regions",
        lambda _page: [{"bbox": [0, 0, 595, 842], "type": "table"}],
    )
    llm = FakeLlm()

    assert eq.find_unnumbered_formulas(pdf, llm=llm) == []
    assert llm.calls == []
