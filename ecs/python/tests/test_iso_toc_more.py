"""The structure extractor's unexercised branches (`silos/iso/toc.py`).

Companion to `test_iso_toc.py`, which pins the entry contract, the two happy fallbacks
that a synthetic ISO-shaped PDF can reach (bookmarks, dot leaders), and the dedup passes.
This file adds only what that file leaves untouched:

* the **precedence** of all five fallbacks, asserted level by level with the chain
  stubbed, including the guarantee that a level which answers stops the ones after it —
  a synthetic PDF cannot get past level 2, so precedence is otherwise unverifiable;
* `_toc_from_text`, effectively uncovered before this file: both passes (bold scan, then
  median-font-size scan) and every filter in the second one;
* the noise filters of `_toc_from_section_patterns` (block type, header/footer band,
  watermarks, table rows, street numbers, lowercase body text);
* the dot-leader line machinery (`_v_reflow_lines`, `_v_find_contents_blocks`,
  `_v_find_page_by_title`) at the level of single lines, where the wrap-merging rules
  and the two-stage page search are legible.

Two statements in the module are unreachable and are deliberately left uncovered:
`toc.py:260` (the `break` when a page yields no collect point *and* the block has already
started — line 253 sets a collect point in exactly that case) and `toc.py:587` (a
`return None` after `_TOC_ID_RE` matched, when one of its four groups must have matched).
Both are harmless, and removing them is a source change.

`fitz` is shimmed inside the module for the font and pattern scans — the same technique
`test_iso_prescan_more.py` uses — because those two functions read only
`page.rect.height` and `page.get_text(...)`, and hand-built spans are the only way to
pin a font-size threshold, a block type or a header-band boundary exactly. Real PDFs are
used where the code opens the file for itself.
"""

from __future__ import annotations

import types

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "toc.py").is_file(), reason="the ISO silo is not present"
)

PAGE_H = 842.0
BODY_SIZE = 10.0
HEADING_SIZE = 13.0     # BODY + 1.5 is the threshold, so this is a heading
BOLD = 16               # span flag bit the bold scan looks for


@pytest.fixture(scope="module")
def toc():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "toc.py", name="toc")


# ── fitz shim ─────────────────────────────────────────────────────────────────


def span(text, *, size=BODY_SIZE, flags=0):
    return {"text": text, "size": size, "flags": flags}


def text_block(*spans, y0=100.0, y1=120.0, btype=0):
    """A `get_text("dict")` block holding one line of spans."""
    return {"type": btype, "bbox": (72.0, y0, 523.0, y1), "lines": [{"spans": list(spans)}]}


def raw_block(text, *, y0=100.0, y1=120.0, btype=0):
    """A `get_text("blocks", sort=True)` tuple: (x0, y0, x1, y1, text, no, type)."""
    return (72.0, y0, 523.0, y1, text, 0, btype)


class FakePage:
    """Answers only `rect.height` and `get_text`, which is all either scan asks for."""

    def __init__(self, dict_blocks=(), raw_blocks=(), height=PAGE_H):
        self.rect = types.SimpleNamespace(height=height)
        self._dict_blocks = list(dict_blocks)
        self._raw_blocks = list(raw_blocks)

    def get_text(self, kind="text", flags=None, sort=False):
        if kind == "dict":
            return {"blocks": self._dict_blocks}
        return list(self._raw_blocks)


class FakeDoc:
    def __init__(self, pages):
        self._pages = list(pages)
        self.closed = False

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, index):
        return self._pages[index]

    def close(self):
        self.closed = True


def shim_fitz(monkeypatch, toc, pages):
    doc = FakeDoc(pages)
    monkeypatch.setattr(toc, "fitz", types.SimpleNamespace(open=lambda _p: doc))
    return doc


def body_page(*, size=BODY_SIZE):
    """A page of ordinary prose, which is what sets the median body font size."""
    return FakePage(
        dict_blocks=[
            text_block(span("This International Standard specifies requirements.", size=size)),
            text_block(span("The test shall be performed as given in Table 3.", size=size),
                       y0=140.0, y1=160.0),
        ]
    )


def bare_pdf(tmp_path, name="bare.pdf", pages=2):
    """A real, bookmark-free PDF for the code that opens the path itself."""
    import fitz

    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=595, height=842)
    path = tmp_path / name
    document.save(str(path))
    document.close()
    return str(path)


# ── fallback precedence ───────────────────────────────────────────────────────


def stub_chain(monkeypatch, toc, *, pypdf=(), font=(), pattern=(), llm=()):
    """Replace levels 2-5 with recorders returning the given entries.

    Returns the call log, so a test can assert that a level was never reached — which is
    the actual contract of a fallback chain.
    """
    called: list[str] = []

    def recorder(name, result):
        def call(_path, **_kwargs):
            called.append(name)
            return [dict(entry) for entry in result]

        return call

    monkeypatch.setattr(toc, "_build_toc_from_pypdf", recorder("pypdf", pypdf))
    monkeypatch.setattr(toc, "_toc_from_text", recorder("font", font))
    monkeypatch.setattr(toc, "_toc_from_section_patterns", recorder("pattern", pattern))
    monkeypatch.setattr(toc, "_toc_from_llm", recorder("llm", llm))
    return called


def marker(name):
    return [{"level": 1, "title": f"1 {name}", "page": 2}]


@pytest.mark.parametrize(
    "level,expected_calls",
    [
        ("pypdf", ["pypdf"]),
        ("font", ["pypdf", "font"]),
        ("pattern", ["pypdf", "font", "pattern"]),
        ("llm", ["pypdf", "font", "pattern", "llm"]),
    ],
)
def test_the_first_fallback_that_answers_stops_the_chain(
    toc, tmp_path, monkeypatch, level, expected_calls
):
    """Each level runs only because the one before it returned nothing, and the levels
    after the answering one must never be consulted — level 5 in particular costs ten
    page renders and a vision request."""
    called = stub_chain(monkeypatch, toc, **{level: marker(level)})

    entries = toc.extract_toc(bare_pdf(tmp_path), llm=FakeLlm())

    assert entries == marker(level)
    assert called == expected_calls


def test_three_bookmarks_outrank_every_local_scan(toc, tmp_path):
    """Level 1 is trusted at three entries, so nothing below it may run."""
    import fitz

    document = fitz.open()
    for _ in range(4):
        document.new_page(width=595, height=842)
    document.set_toc([[1, "1 Scope", 2], [1, "2 Refs", 3], [2, "4.1 Materials", 4]])
    path = tmp_path / "bm.pdf"
    document.save(str(path))
    document.close()

    assert [entry["title"] for entry in toc.extract_toc(str(path))] == [
        "1 Scope",
        "2 Refs",
        "4.1 Materials",
    ]


def test_a_bookmark_without_a_page_is_dropped_before_the_count_is_taken(
    toc, tmp_path, monkeypatch
):
    """`page > 0` is checked before the "at least three" rule, so a tree of two real
    bookmarks and a page-less one falls through to the scans rather than being used."""
    import fitz

    called = stub_chain(monkeypatch, toc, pypdf=marker("pypdf"))

    document = fitz.open()
    for _ in range(3):
        document.new_page(width=595, height=842)
    document.set_toc([[1, "1 Scope", 2], [1, "2 Refs", 3], [1, "Nowhere", -1]])
    path = tmp_path / "partial.pdf"
    document.save(str(path))
    document.close()

    assert toc.extract_toc(str(path)) == marker("pypdf")
    assert called == ["pypdf"]


def test_the_model_is_not_consulted_when_no_client_was_supplied(toc, tmp_path, monkeypatch):
    """`llm=None` must skip level 5 outright, not call it with None."""
    called = stub_chain(monkeypatch, toc)
    assert toc.extract_toc(bare_pdf(tmp_path), llm=None) == []
    assert called == ["pypdf", "font", "pattern"]


# ── the bold heading scan (pass A of the font scan) ───────────────────────────


def test_the_bold_scan_keeps_numbered_and_annex_headings_and_nothing_else(
    toc, monkeypatch
):
    """Pass A trusts bold + a section number. Everything else on the page is noise: the
    running header and footer bands, image blocks, two-character fragments, body prose at
    the same size, and small print.
    """
    page = FakePage(
        dict_blocks=[
            # A picture block: no spans to read at all.
            {"type": 1, "bbox": (72.0, 100.0, 523.0, 200.0), "lines": []},
            # Running header (ends above 65pt) and footer (starts below page_h - 65).
            text_block(span("4.9 Header ghost", flags=BOLD), y0=20.0, y1=40.0),
            text_block(span("4.8 Footer ghost", flags=BOLD), y0=800.0, y1=820.0),
            text_block(span("4.1 Materials", size=11.0, flags=BOLD)),
            # Repeated on the same page: only the first occurrence is kept.
            text_block(span("4.1 Materials", size=11.0, flags=BOLD), y0=200.0, y1=220.0),
            text_block(span("Annex A", size=12.0, flags=BOLD), y0=240.0, y1=260.0),
            # Bold but too small to be a heading, bold but unnumbered, numbered but not
            # bold, and a fragment under three characters.
            text_block(span("4.2 Too small", size=7.0, flags=BOLD), y0=280.0, y1=300.0),
            text_block(span("Introduction", size=11.0, flags=BOLD), y0=320.0, y1=340.0),
            text_block(span("4.3 Not bold", size=11.0), y0=360.0, y1=380.0),
            text_block(span("4.", size=11.0, flags=BOLD), y0=400.0, y1=420.0),
        ]
    )
    doc = shim_fitz(monkeypatch, toc, [page])

    assert toc._toc_from_text("ignored.pdf") == [
        {"level": 2, "title": "4.1 Materials", "page": 1},
        {"level": 1, "title": "Annex A", "page": 1},
    ]
    assert doc.closed, "the document must be closed on the pass-A exit too"


# ── the font-size scan (pass B of the font scan) ──────────────────────────────


def test_the_font_scan_gives_up_when_no_span_is_measurable(toc, monkeypatch):
    """Nothing at 6pt or above and three characters long means no median can be
    computed, so there is no threshold and no heading."""
    page = FakePage(dict_blocks=[text_block(span("x", size=4.0))])
    doc = shim_fitz(monkeypatch, toc, [page, page])

    assert toc._toc_from_text("ignored.pdf") == []
    assert doc.closed


def font_scan_pages(heading_spans):
    """Six pages: three of prose, then the two pages pass B actually looks at, then a
    back cover. Pass B skips the first three pages and the last one.
    """
    heading_page = FakePage(
        dict_blocks=[
            # A picture block: it has no spans, so it can contribute neither to the
            # median nor to a heading.
            {"type": 1, "bbox": (72.0, 400.0, 523.0, 600.0), "lines": []},
        ]
        + [
            text_block(span(text, size=size, flags=flags), y0=100.0 + 30 * i, y1=118.0 + 30 * i)
            for i, (text, size, flags) in enumerate(heading_spans)
        ]
    )
    return [body_page(), body_page(), body_page(), heading_page, body_page(), body_page()]


def test_the_font_scan_reads_headings_above_the_median_by_one_and_a_half_points(
    toc, monkeypatch
):
    """Pass B's whole premise: a span 1.5pt over the median body size is a heading, and
    the level comes from the section number, "Annex" or, failing both, 1."""
    pages = font_scan_pages(
        [
            ("4.1.2 Materials of construction", HEADING_SIZE, 0),
            ("Annex B (informative) Test methods", HEADING_SIZE, 0),
            ("Foreword", HEADING_SIZE, 0),
            # Exactly at the median + 1.4: under the threshold, so not a heading.
            ("5 Nearly big enough", BODY_SIZE + 1.4, 0),
        ]
    )
    shim_fitz(monkeypatch, toc, pages)

    assert toc._toc_from_text("ignored.pdf") == [
        {"level": 3, "title": "4.1.2 Materials of construction", "page": 4},
        {"level": 1, "title": "Annex B (informative) Test methods", "page": 4},
        {"level": 1, "title": "Foreword", "page": 4},
    ]


@pytest.mark.parametrize(
    "text",
    [
        "BS ISO 11040-2:2011",
        "IEC 60601-1 Medical electrical equipment",
        "INTERNATIONAL STANDARD",
        "EUROPEAN STANDARD",
        "BSI Standards Publication",
        "British Standards Institution",
        "12345678",
        "Copyrighted material licensed to Acme",
        "No further reproduction or distribution",
        "All rights reserved",
        "(normative) Requirements",
        "(informative) Guidance",
        "licensed to ABBVIE under a licence",
        "lowercase continuation of a sentence",
        "-- a dash is neither a letter nor a digit",
    ],
    ids=[
        "bs-iso-number", "iec-number", "international-standard", "european-standard",
        "standards-body", "bsi-long-form", "bare-number", "copyright-watermark",
        "reproduction-notice", "rights-notice", "orphan-normative", "orphan-informative",
        "abbvie-watermark", "lowercase-start", "punctuation-start",
    ],
)
def test_the_font_scan_rejects_boilerplate_set_at_heading_size(toc, monkeypatch, text):
    """Cover pages, watermarks and orphaned annex labels are all typeset large. Left in,
    each one becomes a phantom section in the range picker."""
    pages = font_scan_pages([(text, HEADING_SIZE, 0)])
    shim_fitz(monkeypatch, toc, pages)

    assert toc._toc_from_text("ignored.pdf") == []


def test_the_font_scan_rejects_a_heading_longer_than_eighty_characters(toc, monkeypatch):
    """Real headings are short; a long line at heading size is a pull quote or a title
    block."""
    short = "4.1 Materials"
    long_title = (
        "4.2 Requirements for the materials of construction of the device "
        "and its accessories"
    )
    assert len(long_title) > 80

    pages = font_scan_pages([(long_title, HEADING_SIZE, 0), (short, HEADING_SIZE, 0)])
    shim_fitz(monkeypatch, toc, pages)

    assert [entry["title"] for entry in toc._toc_from_text("ignored.pdf")] == [short]


def test_the_font_scan_reports_a_repeated_heading_once(toc, monkeypatch):
    """A running section title reprinted on every page would otherwise produce one entry
    per page."""
    repeated = ("4.1 Materials", HEADING_SIZE, 0)
    pages = font_scan_pages([repeated, repeated])
    shim_fitz(monkeypatch, toc, pages)

    assert len(toc._toc_from_text("ignored.pdf")) == 1


def test_the_font_scan_ignores_the_first_three_pages_and_the_last(toc, monkeypatch):
    """The cover, title page and back cover carry large type that is not structure."""
    heading = FakePage(dict_blocks=[text_block(span("4.1 Materials", size=HEADING_SIZE))])
    pages = [heading, heading, heading, body_page(), body_page(), heading]
    shim_fitz(monkeypatch, toc, pages)

    assert toc._toc_from_text("ignored.pdf") == []


def test_the_font_scan_ignores_the_header_and_footer_bands(toc, monkeypatch):
    """65pt from either edge is the running-header band; a standard number sits there on
    every page of a real document."""
    banded = FakePage(
        dict_blocks=[
            text_block(span("4.1 Header band", size=HEADING_SIZE), y0=20.0, y1=64.0),
            text_block(span("4.2 Footer band", size=HEADING_SIZE), y0=778.0, y1=800.0),
        ]
    )
    pages = [body_page(), body_page(), body_page(), banded, body_page(), body_page()]
    shim_fitz(monkeypatch, toc, pages)

    assert toc._toc_from_text("ignored.pdf") == []


# ── the section-number pattern scan ───────────────────────────────────────────


def test_the_pattern_scan_filters_table_rows_addresses_and_body_text(toc, monkeypatch):
    """Everything rejected here looks like "<number> <text>" but is not a heading. The
    street-number cap and the uppercase rule for single-level numbers are what keep the
    scan usable on documents whose bibliography carries addresses.
    """
    page = FakePage(
        raw_blocks=[
            # A picture block, and blocks inside the header / footer bands.
            raw_block("4.9 Image caption", btype=1),
            raw_block("4.8 Header ghost", y0=20.0, y1=60.0),
            raw_block("4.7 Footer ghost", y0=790.0, y1=820.0),
            raw_block("Copyrighted material licensed to Acme\n4.6 Watermark ghost"),
            raw_block(
                "\n".join(
                    [
                        "1 Scope",
                        "2 Normative references",
                        "4 General requirements",
                        "4.1 Materials",
                        "Annex A (informative) Test methods",
                        # A printed contents line, owned by fallback 2.
                        "5 Marking ....................... 12",
                        # A table row: everything after the number is numeric.
                        "4.2 12,5 (3.0) %",
                        # A street number: top level over the cap of 30.
                        "901 N. Glebe Road",
                        # Body prose starting with a bare number.
                        "4 general requirements continue here",
                        # Too long to be a top-level heading.
                        "6 " + "Requirements " * 7,
                        # Below the three-character floor.
                        "7",
                        # A duplicate section number.
                        "4.1 Materials",
                    ]
                ),
                y0=100.0,
                y1=700.0,
            ),
        ]
    )
    doc = shim_fitz(monkeypatch, toc, [page])

    entries = toc._toc_from_section_patterns("ignored.pdf")

    assert [entry["title"] for entry in entries] == [
        "1 Scope",
        "2 Normative references",
        "4 General requirements",
        "4.1 Materials",
        "Annex A (informative) Test methods",
    ]
    assert [entry["level"] for entry in entries] == [1, 1, 1, 2, 1]
    assert all(entry["page"] == 1 for entry in entries)
    assert doc.closed


def test_the_pattern_scan_discards_a_result_that_fails_hierarchy_validation(
    toc, monkeypatch
):
    """Three subsections whose parents are absent are reference numbers scraped out of
    prose, not a contents tree — so the scan reports nothing and level 5 gets its turn."""
    page = FakePage(
        raw_blocks=[
            raw_block("\n".join([f"{n}.1.1 Orphan requirement" for n in range(1, 8)]),
                      y0=100.0, y1=400.0)
        ]
    )
    shim_fitz(monkeypatch, toc, [page])

    assert toc._toc_from_section_patterns("ignored.pdf") == []


# ── the dot-leader line machinery ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "line,discarded",
    [
        ("", True),
        ("   ", True),
        ("Copyrighted material licensed to Acme", True),
        ("No further reproduction or distribution permitted", True),
        ("© ISO 2026", True),
        ("Â© ISO 2026", True),
        ("Copyright by the International Organization", True),
        ("BS EN ISO 11608-1:2022", True),
        ("ISO 11608-1:2022(en)", True),
        ("Verpackungen (Deutschen)", True),
        ("1 Scope ....... 3", False),
    ],
)
def test_contents_page_noise_is_discarded_line_by_line(toc, line, discarded):
    """The `(en)` suffix rule exists for the bilingual title lines that ISO prints above
    the contents block; they otherwise parse as entries."""
    assert toc._v_should_discard(line) is discarded


@pytest.mark.parametrize(
    "line,has_token",
    [
        ("", False),
        ("1 Scope 3", True),
        ("Foreword iv", True),
        ("4.1 Materials", False),
    ],
)
def test_a_trailing_page_token_is_a_number_or_a_roman_numeral(toc, line, has_token):
    """Front matter is numbered in roman, so both forms have to count as a page."""
    assert toc._v_has_trailing_page_token(line) is has_token


@pytest.mark.parametrize(
    "lines,expected",
    [
        # Already complete on its own: the next line is a separate entry.
        (["4.1 Materials 5", "4.2 Coatings 6"], ["4.1 Materials 5", "4.2 Coatings 6"]),
        # Six dots and a roman page number: a dot run plus a trailing token is complete
        # even though the seven-dot leader pattern does not match it, so the entry below
        # is not pulled into it.
        (
            ["Foreword ...... iv", "1 Scope ....... 3"],
            ["Foreword ...... iv", "1 Scope ....... 3"],
        ),
        # The page number wrapped onto its own line: merging completes the entry.
        (["4.1 Materials", "5"], ["4.1 Materials 5"]),
        # A wrapped title with no page number anywhere: merged on the dot-run rule.
        (["Scope ......", "and requirements"], ["Scope ...... and requirements"]),
        # Complete-looking but unparseable, followed by an unrelated line: no merge.
        (["Foreword 3", "Scope now"], ["Foreword 3", "Scope now"]),
    ],
    ids=["complete-index-line", "six-dot-leader", "wrapped-page-number",
         "wrapped-title", "no-merge"],
)
def test_wrapped_contents_lines_are_merged_into_one_entry(toc, lines, expected):
    """A two-column contents page wraps long titles, and the page number can land on the
    following line; without the merge each half becomes a junk entry."""
    assert toc._v_reflow_lines(lines) == expected


def test_a_dot_leader_page_without_a_contents_header_is_still_the_contents_page(toc):
    """Continuation pages of a long contents block carry no header of their own."""
    pages = [
        ["1 Scope ........................ 3", "2 Normative references ......... 3"],
    ]
    toc_lines, start, end = toc._v_find_contents_blocks(pages)

    assert [line for _page, line in toc_lines] == [
        "1 Scope ........................ 3",
        "2 Normative references ......... 3",
    ]
    assert (start, end) == (0, 0)


def test_noise_above_the_contents_header_does_not_hide_it(toc):
    pages = [
        [
            "",
            "© ISO 2026 - All rights reserved",
            "Contents",
            "1 Scope ........................ 3",
        ]
    ]
    toc_lines, start, _end = toc._v_find_contents_blocks(pages)

    assert [line for _page, line in toc_lines] == ["1 Scope ........................ 3"]
    assert start == 0


def test_a_watermark_that_only_becomes_recognisable_after_normalisation_is_dropped(toc):
    """The line is kept through reflow because its doubled space stops the prefix from
    matching, then dropped once spaces are collapsed — which is why the discard test runs
    twice, before and after normalisation."""
    pages = [
        [
            "Contents",
            "1 Scope ........................ 3",
            "Copyrighted  material  licensed  to Acme Corp",
        ]
    ]
    toc_lines, _start, _end = toc._v_find_contents_blocks(pages)

    assert [line for _page, line in toc_lines] == ["1 Scope ........................ 3"]


def test_the_contents_block_ends_at_the_first_page_without_entries(toc):
    """Prose after the contents block ends it, and nothing later is looked at again —
    otherwise a printed index at the back of the standard would be parsed too."""
    pages = [
        ["Contents", "1 Scope ........................ 3"],
        ["This document specifies requirements for the device."],
        ["2 Normative references ......... 4"],
    ]
    toc_lines, start, end = toc._v_find_contents_blocks(pages)

    assert [page for page, _line in toc_lines] == [0]
    assert (start, end) == (0, 0)


def test_a_body_page_opening_with_a_section_heading_is_absorbed_into_the_contents_block(
    toc,
):
    """Characterization, not endorsement. The module docstring says body pages are never
    scanned for headings, and that holds for the page that *ends* the block — but the
    page immediately after the contents page is collected from line 0, so if it opens
    with a numbered heading it is reflowed and parsed as a contents line. Here the
    heading and the first sentence of section 1 merge into a single bogus entry.

    Suspected defect: `_v_find_contents_blocks` (toc.py:253-254) should require a page to
    be leader-heavy, not merely to follow one, before collecting it. Correct behaviour
    would be one entry, from page 0 only.
    """
    pages = [
        ["Contents", "1 Scope ........................ 3"],
        ["1 Scope", "This document specifies requirements for the device."],
    ]
    toc_lines, _start, end = toc._v_find_contents_blocks(pages)

    assert [page for page, _line in toc_lines] == [0, 1]
    assert toc_lines[1][1] == "1 Scope This document specifies requirements for the device."
    assert end == 1


# ── resolving an entry to a page ──────────────────────────────────────────────


def virtual(toc, *texts):
    return toc.VirtualPdfReader([{"text": text} for text in texts])


def test_a_virtual_reader_reports_its_page_count(toc):
    reader = virtual(toc, "one", "two", "three")
    assert len(reader) == 3
    assert len(reader.pages) == 3


def test_reading_a_page_that_does_not_exist_yields_empty_text(toc):
    """The flatten pass indexes by page number from the printed contents, which can
    exceed the document's own page count."""
    assert toc._v_extract_page_text(virtual(toc, "only page"), 5) == ""


@pytest.mark.parametrize("title", ["", "   ", None])
def test_a_blank_title_resolves_to_no_page(toc, title):
    """An empty pattern would match every page at offset 0 and drag the whole outline to
    page 1."""
    assert toc._v_build_title_patterns(title) == []
    assert toc._v_find_page_by_title(virtual(toc, "1 Scope"), title) is None


def test_a_heading_is_matched_on_its_own_line_before_a_mid_paragraph_mention(toc):
    """Pattern order matters: the whole-line form wins, so a cross-reference to "4.1
    Materials" in an earlier paragraph cannot claim the section."""
    reader = virtual(
        toc,
        "cover",
        "as described in 4.1 Materials the device shall be inert",
        "4.1 Materials\nThe materials shall be inert.",
    )
    assert toc._v_find_page_by_title(reader, "4.1 Materials") == 2


def test_a_heading_below_the_first_sixty_lines_is_found_by_the_full_page_fallback(toc):
    """The head-of-page search is what keeps a heading from being matched against a
    footnote, but a dense page can push a real heading past line 60."""
    filler = "\n".join(f"line {n} of body prose" for n in range(70))
    reader = virtual(toc, "cover", filler + "\n4.7 Sterilization")

    assert toc._v_find_page_by_title(reader, "4.7 Sterilization") == 1


def test_an_absent_heading_resolves_to_no_page(toc):
    assert toc._v_find_page_by_title(virtual(toc, "cover", "body"), "9 Marking") is None


def test_an_unresolvable_entry_is_placed_by_the_median_printed_page_offset(toc):
    """The printed contents numbers ignore the unnumbered front matter, so the offset
    between "printed page 3" and "PDF page index 4" has to be learnt from the entries
    that did resolve. The median is used because one bad match must not move the rest.
    """
    reader = virtual(
        toc,
        "cover",
        "Contents",
        "front matter",
        "1 Scope\nThis document specifies requirements.",
        "2 Normative references\nThe following documents are referred to.",
        "body",
        "body",
        "body",
    )
    entries = [
        {"index": "1", "title": "Scope", "level": "1", "toc_page": 1},
        {"index": "2", "title": "Normative references", "level": "1", "toc_page": 2},
        # Never printed in the body, so only the offset can place it.
        {"index": "9", "title": "Marking", "level": "1", "toc_page": 4},
    ]

    flat = toc._v_flatten_outlines(entries, reader, toc_start=1, toc_end=1)

    # "1 Scope" is on index 3 and printed as page 1, so the offset is 3.
    assert [item["page_idx"] for item in flat] == [3, 4, 6]


def test_the_offset_is_zero_when_nothing_resolved(toc):
    """With no sample there is no offset, so the printed page number is used as-is and
    clamped into the document."""
    reader = virtual(toc, "cover", "Contents", "body", "body")
    entries = [{"index": "9", "title": "Marking", "level": "1", "toc_page": 99}]

    flat = toc._v_flatten_outlines(entries, reader, toc_start=1, toc_end=1)

    assert flat == [{"level": 1, "title": "9 Marking", "page_idx": 3}]


# ── structural identifiers and dedup keys ─────────────────────────────────────


@pytest.mark.parametrize(
    "title,identifier",
    [
        ("4.3.1 general", "sec:4.3.1"),
        ("annex a (informative)", "annex:a"),
        ("figure 9 - the device", "figure:9"),
        ("figure a.8 - the needle", "figure:a.8"),
        ("table 11 - creepage", "table:11"),
        ("table a.6 - values", "table:a.6"),
        ("foreword", None),
        ("", None),
    ],
)
def test_a_structural_identifier_survives_a_reworded_title(toc, title, identifier):
    """Pass 2 of the dedup depends on this: the same section renamed between editions
    still has to collide."""
    assert toc._extract_toc_id(title) == identifier


def test_an_entry_whose_title_is_pure_noise_is_dropped(toc):
    """A bookmark of bullets and dashes normalises to an empty key, which cannot be
    deduplicated against anything — so it is discarded rather than kept as a blank row in
    the range picker."""
    entries = [
        {"level": 1, "title": "1 Scope", "page": 3},
        {"level": 1, "title": "* – •", "page": 4},
        {"level": 1, "title": "2 Normative references", "page": 5},
    ]

    assert [entry["title"] for entry in toc.deduplicate_toc(entries)] == [
        "1 Scope",
        "2 Normative references",
    ]


# ── the model fallback's entry conversion ─────────────────────────────────────


def test_the_model_fallback_keeps_a_section_number_that_arrived_without_a_title(
    toc, tmp_path
):
    """A number alone is still a usable outline row; an entry with neither number nor
    title is not, and an unparseable page number must not raise out of the whole call."""
    import json

    reply = json.dumps(
        [
            {"section_number": "", "title": "", "page_number": 3},
            {"section_number": "4.1", "title": "", "page_number": 5},
            {"section_number": "", "title": "Foreword", "page_number": "not a number"},
            {"section_number": "Annex A", "title": "Test methods", "page_number": None},
        ]
    )
    entries = toc._toc_from_llm(bare_pdf(tmp_path), llm=FakeLlm(chat_vision_multi=[reply]))

    assert [(entry["level"], entry["title"], entry["page"]) for entry in entries] == [
        (2, "4.1", 5),
        (1, "Foreword", 1),
        (1, "Annex A Test methods", 1),
    ]
