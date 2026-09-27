"""Getting usable text out of a manual (`silos/bop/ingest.py`).

At 15% covered this was the least-tested module in BOP, which matters because it is the
front door: every later stage reads `raw_text`, so a silent regression here degrades the
whole report rather than failing loudly.

Three things shape how these tests are built.

**Real files, not mocks.** The DOCX walk reads `w:pStyle` out of the XML and the PDF path
goes through pdfplumber, so both are exercised against genuine documents built in-memory.
Mocking either would assert that we call a library, not that we understand its output.

**The 500-character threshold is the whole design.** It is what decides between a cheap
text extraction and an expensive per-page vision call, so it is pinned from both sides.

**Vision OCR is concurrent and lossy on purpose.** Pages are transcribed in a thread pool
and a page that fails becomes an empty string rather than an exception, because losing one
page of a manual is better than losing the manual. Tests that care about ordering pin the
result by index, not by completion order.
"""

from __future__ import annotations

import base64
import io

import fitz
import pytest
from docx import Document

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "ingest.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def ingest():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import ingest as module

    return module


# ── test doubles ─────────────────────────────────────────────────────────────


class FakeVisionLlm:
    """Answers `chat_vision` from a queue, or raises a queued exception.

    Keyed by call order rather than by page, because the pool decides who calls first.
    `replies` may hold a callable, which receives the decoded image and returns the text --
    that is how a test pins "this page produced this transcript" deterministically.
    """

    def __init__(self, replies=None, per_image=None):
        self._replies = list(replies or [])
        self._per_image = dict(per_image or {})
        self.calls = []

    def chat_vision(self, *, image_b64, prompt, **kwargs):
        self.calls.append({"image_b64": image_b64, "prompt": prompt, **kwargs})
        if self._per_image:
            # Keyed by the exact rendered image, so the answer for a page is the same
            # whichever worker thread happens to ask for it.
            return self._per_image.get(image_b64, "")
        if not self._replies:
            return ""
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeCtx:
    """Only `llm` and `progress` are reached from this module."""

    def __init__(self, llm=None):
        self.llm = llm or FakeVisionLlm()
        self.progress_calls = []

    def progress(self, message, pct=None):
        self.progress_calls.append((message, pct))


# ── document builders ────────────────────────────────────────────────────────


def docx_bytes(build) -> bytes:
    document = Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pdf_bytes(pages_text, *, blank_pages=0) -> bytes:
    """A real PDF. `pages_text` is a list of strings, one per page."""
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page(width=595, height=842)
        for index, chunk in enumerate(text.split("\n")):
            page.insert_text((60, 80 + 14 * index), chunk, fontname="helv", fontsize=10)
    for _ in range(blank_pages):
        doc.new_page(width=595, height=842)
    buffer = doc.tobytes()
    doc.close()
    return buffer


def wordy_page(marker="Alpha") -> str:
    """A page comfortably over MIN_PDF_TEXT_THRESHOLD once extracted."""
    return "\n".join(f"{marker} line {index} of the operating manual body text." for index in range(20))


def pdf_with_ruled_table() -> bytes:
    """A PDF holding a stroked 2x3 grid plus enough prose to clear the text threshold."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    left, top, col_w, row_h, cols, rows = 60, 80, 160, 30, 2, 3
    right, bottom = left + cols * col_w, top + rows * row_h
    for index in range(cols + 1):
        x = left + index * col_w
        page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=1)
    for index in range(rows + 1):
        y = top + index * row_h
        page.draw_line((left, y), (right, y), color=(0, 0, 0), width=1)

    cells = [["Step", "Action"], ["1", "Open the valve"], ["2", "Close the valve"]]
    for row_index, row in enumerate(cells):
        for col_index, value in enumerate(row):
            page.insert_text(
                (left + col_index * col_w + 6, top + row_index * row_h + 20),
                value,
                fontname="helv",
                fontsize=10,
            )

    for index in range(20):
        page.insert_text(
            (60, bottom + 30 + 14 * index),
            f"Surrounding prose line {index} to clear the text threshold.",
            fontname="helv",
            fontsize=10,
        )

    payload = doc.tobytes()
    doc.close()
    return payload


# ── _docx_table_data ─────────────────────────────────────────────────────────


def test_table_cell_text_is_stripped(ingest):
    """Word pads cells with the paragraph mark's whitespace, which would reach the LLM."""
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "  Step  "
    table.cell(0, 1).text = "\nAction\n"
    assert ingest._docx_table_data(table) == [["Step", "Action"]]


# ── extract_from_docx: hierarchy ─────────────────────────────────────────────


def test_a_heading_one_opens_a_section(ingest):
    result = ingest.extract_from_docx(
        docx_bytes(lambda d: d.add_heading("Preparation", level=1))
    )
    assert result["format"] == "docx"
    assert [s["heading"] for s in result["sections"]] == ["Preparation"]
    assert result["sections"][0]["level"] == 1


def test_body_text_lands_in_the_open_section(ingest):
    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_paragraph("Wear gloves.")
        document.add_paragraph("Check the seal.")

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["content"] == ["Wear gloves.", "Check the seal."]


@pytest.mark.parametrize("level,expected", [(2, 2), (3, 3)])
def test_a_heading_two_or_three_opens_a_subsection(ingest, level, expected):
    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_heading("Sub step", level=level)

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert [s["heading"] for s in section["subsections"]] == ["Sub step"]
    assert section["subsections"][0]["level"] == expected


def test_text_after_a_subsection_heading_belongs_to_the_subsection(ingest):
    """The nearest open container wins, which is what makes the hierarchy useful."""

    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_paragraph("Section level note.")
        document.add_heading("Sub step", level=2)
        document.add_paragraph("Subsection level note.")

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["content"] == ["Section level note."]
    assert section["subsections"][0]["content"] == ["Subsection level note."]


def test_a_new_heading_one_closes_the_open_subsection(ingest):
    """Otherwise the next section's body text would keep landing in the old subsection."""

    def build(document):
        document.add_heading("First", level=1)
        document.add_heading("Sub of first", level=2)
        document.add_heading("Second", level=1)
        document.add_paragraph("Belongs to Second.")

    sections = ingest.extract_from_docx(docx_bytes(build))["sections"]
    assert sections[1]["content"] == ["Belongs to Second."]
    assert sections[1]["subsections"] == []


def test_a_subsection_before_any_section_is_not_attached_anywhere(ingest):
    """A manual that opens on a Heading 2 has nothing to hang it from; it is dropped."""
    result = ingest.extract_from_docx(
        docx_bytes(lambda d: d.add_heading("Orphan sub", level=2))
    )
    assert result["sections"] == []


def test_text_before_any_heading_is_discarded(ingest):
    """Cover pages and legal boilerplate arrive here and are not part of any procedure."""

    def build(document):
        document.add_paragraph("Cover page boilerplate.")
        document.add_heading("Preparation", level=1)

    result = ingest.extract_from_docx(docx_bytes(build))
    assert result["sections"][0]["content"] == []


def test_blank_paragraphs_are_skipped(ingest):
    """Word documents are full of spacer paragraphs; each would become an empty line."""

    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_paragraph("")
        document.add_paragraph("   ")
        document.add_paragraph("Real content.")

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["content"] == ["Real content."]


def test_an_empty_heading_still_opens_a_section(ingest):
    """A styled-but-empty heading is a real structural break, unlike a blank paragraph."""

    def build(document):
        document.add_heading("", level=1)
        document.add_paragraph("Content under a nameless heading.")

    sections = ingest.extract_from_docx(docx_bytes(build))["sections"]
    assert len(sections) == 1
    assert sections[0]["heading"] == ""
    assert sections[0]["content"] == ["Content under a nameless heading."]


# ── extract_from_docx: tables ────────────────────────────────────────────────


def test_a_table_attaches_to_the_open_section(ingest):
    def build(document):
        document.add_heading("Preparation", level=1)
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Step"
        table.cell(0, 1).text = "Action"

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["tables"] == [[["Step", "Action"]]]


def test_a_table_attaches_to_the_open_subsection_in_preference(ingest):
    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_heading("Sub step", level=2)
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "In the subsection"

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["tables"] == []
    assert section["subsections"][0]["tables"] == [[["In the subsection"]]]


def test_a_table_before_any_heading_is_dropped(ingest):
    def build(document):
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "Orphan"

    assert ingest.extract_from_docx(docx_bytes(build))["sections"] == []


def test_several_tables_keep_their_document_order(ingest):
    """They are matched to the body by a running index, so an off-by-one would swap them."""

    def build(document):
        document.add_heading("Preparation", level=1)
        for label in ("first", "second", "third"):
            table = document.add_table(rows=1, cols=1)
            table.cell(0, 0).text = label

    section = ingest.extract_from_docx(docx_bytes(build))["sections"][0]
    assert section["tables"] == [[["first"]], [["second"]], [["third"]]]


# ── _sections_to_text ────────────────────────────────────────────────────────


def test_the_flattened_text_marks_headings_with_hashes(ingest):
    sections = [
        {"heading": "Preparation", "level": 1, "content": ["Wear gloves."], "tables": [],
         "subsections": [
             {"heading": "Sub two", "level": 2, "content": ["Two."], "tables": []},
             {"heading": "Sub three", "level": 3, "content": ["Three."], "tables": []},
         ]}
    ]
    text = ingest._sections_to_text(sections)
    assert text.splitlines() == [
        "# Preparation",
        "Wear gloves.",
        "## Sub two",
        "Two.",
        "### Sub three",
        "Three.",
    ]


def test_tables_are_delimited_so_the_model_can_see_where_they_end(ingest):
    """Without the markers a pipe-joined row is indistinguishable from prose."""
    sections = [
        {"heading": "Preparation", "level": 1, "content": [], "subsections": [],
         "tables": [[["Step", "Action"], ["1", "Open"]]]}
    ]
    assert ingest._sections_to_text(sections).splitlines() == [
        "# Preparation",
        "[TABLE]",
        "Step | Action",
        "1 | Open",
        "[/TABLE]",
    ]


def test_a_subsection_table_is_delimited_too(ingest):
    sections = [
        {"heading": "Preparation", "level": 1, "content": [], "tables": [],
         "subsections": [
             {"heading": "Sub", "level": 2, "content": [], "tables": [[["a", "b"]]]}
         ]}
    ]
    assert ingest._sections_to_text(sections).splitlines() == [
        "# Preparation",
        "## Sub",
        "[TABLE]",
        "a | b",
        "[/TABLE]",
    ]


def test_a_section_with_no_subsections_key_is_tolerated(ingest):
    """`.get("subsections", [])` -- a subsection dict has no such key of its own."""
    assert ingest._sections_to_text(
        [{"heading": "Only", "level": 1, "content": [], "tables": []}]
    ) == "# Only"


def test_flattening_nothing_produces_an_empty_string(ingest):
    assert ingest._sections_to_text([]) == ""


def test_the_docx_result_carries_the_flattened_text(ingest):
    """`raw_text` is what every later stage actually reads."""

    def build(document):
        document.add_heading("Preparation", level=1)
        document.add_paragraph("Wear gloves.")

    result = ingest.extract_from_docx(docx_bytes(build))
    assert result["raw_text"] == "# Preparation\nWear gloves."


# ── extract_from_pdf ─────────────────────────────────────────────────────────


def test_a_text_pdf_is_extracted_without_vision(ingest):
    result = ingest.extract_from_pdf(pdf_bytes([wordy_page()]))
    assert result["format"] == "pdf"
    assert "operating manual body text" in result["raw_text"]


def test_every_page_contributes_to_the_raw_text(ingest):
    result = ingest.extract_from_pdf(pdf_bytes([wordy_page("Alpha"), wordy_page("Beta")]))
    assert "Alpha line 0" in result["raw_text"]
    assert "Beta line 0" in result["raw_text"]


def test_a_page_with_no_text_layer_contributes_an_empty_string(ingest):
    """`page.extract_text()` returns None for a blank page, which must not concatenate."""
    result = ingest.extract_from_pdf(pdf_bytes([wordy_page()], blank_pages=1))
    assert "operating manual body text" in result["raw_text"]


def test_a_pdf_below_the_text_threshold_asks_for_vision(ingest):
    """This is the branch that decides whether a run costs one call or one call per page."""
    with pytest.raises(ingest.NeedsVisionFallback) as raised:
        ingest.extract_from_pdf(pdf_bytes(["Tiny."]))
    assert "characters" in str(raised.value)


def test_the_threshold_message_names_the_character_count_it_found(ingest):
    """It is logged, and "why did this go to vision" is the question it has to answer."""
    with pytest.raises(ingest.NeedsVisionFallback) as raised:
        ingest.extract_from_pdf(pdf_bytes(["Tiny."]))
    assert str(len("Tiny.")) in str(raised.value)


def test_a_pdf_just_over_the_threshold_is_kept_as_text(ingest):
    """Pinned from the other side so the comparison cannot silently become `<=`."""
    filler = "\n".join("x" * 60 for _ in range(12))  # ~700 chars extracted
    result = ingest.extract_from_pdf(pdf_bytes([filler]))
    assert len(result["raw_text"].strip()) >= ingest.MIN_PDF_TEXT_THRESHOLD


def test_tables_found_in_a_pdf_are_returned_alongside_the_text(ingest):
    """pdfplumber finds ruled tables; the key must exist even when there are none."""
    result = ingest.extract_from_pdf(pdf_bytes([wordy_page()]))
    assert result["tables"] == []


def test_a_ruled_table_in_a_pdf_is_extracted_as_rows(ingest):
    """Needs real ruling lines: pdfplumber infers a grid from strokes, not from spacing.

    Worth the trouble of drawing one, because the tables list is a separate output from
    `raw_text` and nothing else in these tests would notice if it stopped being populated.
    """
    result = ingest.extract_from_pdf(pdf_with_ruled_table())

    assert result["tables"], "pdfplumber found no table in a ruled grid"
    flattened = [cell for row in result["tables"][0] for cell in row if cell]
    assert "Step" in flattened
    assert "Action" in flattened


# ── _render_page_b64 ─────────────────────────────────────────────────────────


def test_a_rendered_page_is_base64_encoded_png(ingest):
    encoded = ingest._render_page_b64(pdf_bytes([wordy_page()]), 0)
    assert base64.b64decode(encoded).startswith(b"\x89PNG")


def test_a_higher_dpi_renders_a_bigger_image(ingest):
    """The DPI is what makes small print legible to the model, so it is not cosmetic."""
    source = pdf_bytes([wordy_page()])
    assert len(ingest._render_page_b64(source, 0, dpi=300)) > len(
        ingest._render_page_b64(source, 0, dpi=72)
    )


def test_each_page_renders_independently(ingest):
    """Reopened per call so pool workers never share a PyMuPDF handle."""
    source = pdf_bytes([wordy_page("Alpha"), wordy_page("Beta")])
    assert ingest._render_page_b64(source, 0) != ingest._render_page_b64(source, 1)


# ── extract_from_pdf_vision ──────────────────────────────────────────────────


def test_vision_transcribes_one_call_per_page(ingest):
    llm = FakeVisionLlm(["page one text", "page two text"])
    ctx = FakeCtx(llm)

    result = ingest.extract_from_pdf_vision(pdf_bytes([wordy_page(), wordy_page("Beta")]), ctx)

    assert len(llm.calls) == 2
    assert "page one text" in result["raw_text"]
    assert "page two text" in result["raw_text"]


def test_the_transcription_prompt_forbids_summarising(ingest):
    """A summary here would silently shorten the manual the whole report is built from."""
    llm = FakeVisionLlm(["text"])
    ingest.extract_from_pdf_vision(pdf_bytes([wordy_page()]), FakeCtx(llm))

    prompt = llm.calls[0]["prompt"]
    assert "Do not summarize" in prompt or "not summarize" in prompt
    assert "ONLY the transcribed" in prompt


def test_pages_are_joined_in_page_order_not_completion_order(ingest):
    """The pool finishes out of order; the transcript has to read like the document.

    The reply is keyed by the rendered image rather than by call order, because a queue
    popped in call order would attach transcripts to whichever worker won the race -- and
    then this test would pass or fail on thread scheduling instead of on the code under
    test. Each page is pre-rendered here so the expected mapping is exact.
    """
    source = pdf_bytes([wordy_page(f"Marker{index}") for index in range(4)])
    per_image = {
        ingest._render_page_b64(source, index): f"transcript {index}" for index in range(4)
    }
    llm = FakeVisionLlm(per_image=per_image)

    result = ingest.extract_from_pdf_vision(source, FakeCtx(llm))

    assert result["raw_text"].splitlines() == [
        "transcript 0",
        "",
        "transcript 1",
        "",
        "transcript 2",
        "",
        "transcript 3",
    ]


def test_a_page_that_fails_becomes_empty_rather_than_losing_the_manual(ingest):
    llm = FakeVisionLlm(["good page", RuntimeError("vision exploded"), "another good page"])
    pages = [wordy_page(f"M{index}") for index in range(3)]

    result = ingest.extract_from_pdf_vision(pdf_bytes(pages), FakeCtx(llm))

    assert "good page" in result["raw_text"]
    assert "another good page" in result["raw_text"]
    assert "vision exploded" not in result["raw_text"]


def test_empty_transcripts_are_left_out_of_the_joined_text(ingest):
    """A blank page returning "" must not produce a run of empty separator lines."""
    llm = FakeVisionLlm(["real text", "", "more text"])
    pages = [wordy_page(f"M{index}") for index in range(3)]

    result = ingest.extract_from_pdf_vision(pdf_bytes(pages), FakeCtx(llm))

    assert result["raw_text"] == "real text\n\nmore text"


def test_vision_reports_progress_for_every_page(ingest):
    """A long OCR run is silent otherwise, and silence gets the run re-queued by the reaper."""
    pages = [wordy_page(f"M{index}") for index in range(3)]
    ctx = FakeCtx(FakeVisionLlm(["a", "b", "c"]))

    ingest.extract_from_pdf_vision(pdf_bytes(pages), ctx)

    assert len(ctx.progress_calls) == 3
    percentages = [pct for _message, pct in ctx.progress_calls]
    assert percentages == sorted(percentages)
    assert all(10 <= pct <= 20 for pct in percentages)


def test_the_progress_message_counts_pages_out_of_the_total(ingest):
    ctx = FakeCtx(FakeVisionLlm(["a", "b"]))
    ingest.extract_from_pdf_vision(pdf_bytes([wordy_page(), wordy_page("B")]), ctx)
    assert "of 2" in ctx.progress_calls[-1][0]


def test_a_pdf_with_no_pages_returns_nothing_and_calls_no_model(ingest, monkeypatch):
    """Guarded because `min(workers, 0)` is an invalid pool size, not merely useless."""

    class EmptyPdf:
        page_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(fitz, "open", lambda *args, **kwargs: EmptyPdf())
    llm = FakeVisionLlm()

    result = ingest.extract_from_pdf_vision(b"irrelevant", FakeCtx(llm))

    assert result == {"format": "pdf", "raw_text": "", "tables": []}
    assert llm.calls == []


def test_the_worker_count_never_exceeds_the_page_count(ingest):
    """A one-page manual must not open four threads to read it."""
    assert min(ingest.VISION_MAX_WORKERS, 1) == 1
    llm = FakeVisionLlm(["only page"])
    result = ingest.extract_from_pdf_vision(pdf_bytes([wordy_page()]), FakeCtx(llm))
    assert result["raw_text"] == "only page"


# ── extract: strategy selection ──────────────────────────────────────────────


@pytest.mark.parametrize("filename", ["manual.docx", "MANUAL.DOCX", "Manual.DocX"])
def test_a_docx_is_dispatched_by_extension_whatever_its_case(ingest, filename):
    payload = docx_bytes(lambda d: d.add_heading("Preparation", level=1))
    result = ingest.extract(filename, payload, FakeCtx())
    assert result["format"] == "docx"


def test_a_text_pdf_never_reaches_the_vision_path(ingest):
    """The saving is the point: one text extraction instead of one call per page."""
    llm = FakeVisionLlm()
    result = ingest.extract("manual.pdf", pdf_bytes([wordy_page()]), FakeCtx(llm))
    assert result["format"] == "pdf"
    assert llm.calls == []


def test_a_scanned_pdf_falls_through_to_vision(ingest):
    llm = FakeVisionLlm(["transcribed by vision"])
    ctx = FakeCtx(llm)

    result = ingest.extract("scan.pdf", pdf_bytes(["Tiny."]), ctx)

    assert result["raw_text"] == "transcribed by vision"
    assert len(llm.calls) == 1


def test_the_fallback_tells_the_user_why_the_run_slowed_down(ingest):
    """The first progress line is the only explanation a waiting user gets."""
    ctx = FakeCtx(FakeVisionLlm(["text"]))
    ingest.extract("scan.pdf", pdf_bytes(["Tiny."]), ctx)

    first_message, first_pct = ctx.progress_calls[0]
    assert "no text layer" in first_message.lower()
    assert first_pct == 10
