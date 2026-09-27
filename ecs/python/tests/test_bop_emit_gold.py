"""The parts of `silos/bop/emit.py` and `silos/bop/gold.py` nothing else reaches.

`tests/test_bop.py`, `tests/test_bop_gold.py` and `tests/test_bop_section_order.py`
already cover the happy paths of both modules; this file deliberately adds only what
they miss, so that every test here corresponds to a line that was previously
unexercised. Three clusters account for almost all of it:

**The gold document parser.** `load_gold_doc` walks the body XML of a real .docx, and
the existing gold tests bypass it entirely by handing `resolve_gold_section` and
`compute_gold_bounds` a plain dict. Nothing therefore checked that the walker turns a
Word document into that dict shape in the first place — the one step that breaks if the
gold is re-authored. These tests build genuine documents in memory with python-docx
rather than mocking, because the H1/H2 grouping is read off `w:pStyle` and the row
grouping off real table geometry; a mock would only assert that we call python-docx.

**The sub-block lookups in `resolve_gold_section`.** Every entry in `GOLD_SECTION_MAP`
that carries a `sub` key funnels through the header-list selection, and two of those
lists interact badly with the abbreviated `sub` prefixes the map actually uses. That is
pinned as a characterization test below.

**The emitter's inline-formatting and coercion branches.** Bold/italic/underline runs,
`<br>`, sibling `<p>`s, unknown tags, non-string list items, HTML inside bullets and
table cells, and the tuple/dict shapes of the troubleshooting and abbreviation rows.
Following `tests/test_bop.py`, the emitter runs against a blank python-docx document —
the named styles it needs exist in the default template too — and every assertion is
made against the document read back out of a `BytesIO`, since reopening it is what
proves the result is valid OOXML rather than merely that a method was called.
"""

from __future__ import annotations

import io

import pytest
from docx import Document

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "emit.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def bop():
    module = _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import emit, gold, schema

    return {"silo": module, "emit": emit, "gold": gold, "schema": schema}


@pytest.fixture(autouse=True)
def _clear_bounds_cache(bop):
    bop["gold"].reset_cache()
    yield
    bop["gold"].reset_cache()


# ── helpers ───────────────────────────────────────────────────────────────────


def blank_template() -> bytes:
    buffer = io.BytesIO()
    Document().save(buffer)
    return buffer.getvalue()


def save(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def reopen(payload: bytes):
    """Round-tripping through a file is the check that catches invalid OOXML."""
    return Document(io.BytesIO(payload))


def all_text(document) -> str:
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def headings_at(document, level: int) -> list[str]:
    prefix = f"Heading {level}"
    return [
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.style.name.startswith(prefix)
    ]


def runs_of(document, text: str):
    for paragraph in document.paragraphs:
        if paragraph.text == text:
            return paragraph.runs
    raise AssertionError(f"no paragraph reads {text!r}")


def visible_runs(paragraph):
    """Runs that carry text.

    `_add_table` blanks each cell with `cell.text = ""`, and python-docx implements that
    by writing a run holding the empty string, so the run the emitter then adds is never
    index 0. Harmless in the rendered document, but it makes indexing by 0 wrong.
    """
    return [run for run in paragraph.runs if run.text]


def body_paragraph(bop, html: str):
    """One `_add_body` paragraph rendered from `html`, read back from a saved file."""
    document = Document()
    bop["emit"]._add_body(document, html, parent_level=1)
    return reopen(save(document)).paragraphs[0]


# ═══════════════════════════════════════════════════════════════════════════════
# emit.py — inline formatting
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "tag,attribute",
    [
        ("b", "bold"),
        ("strong", "bold"),
        ("i", "italic"),
        ("em", "italic"),
        ("u", "underline"),
    ],
)
def test_an_inline_tag_sets_its_run_property(bop, tag, attribute):
    """The reviewer's emphasis is content, not decoration: it survives to the .docx."""
    paragraph = body_paragraph(bop, f"<{tag}>marked</{tag}>")

    assert paragraph.text == "marked"
    assert getattr(paragraph.runs[0], attribute) is True


def test_nested_inline_tags_combine_rather_than_replace(bop):
    """The format stack merges frames, so bold inside italic is both."""
    paragraph = body_paragraph(bop, "<i><b>both</b></i>")

    run = paragraph.runs[0]
    assert run.bold is True
    assert run.italic is True


def test_only_the_marked_span_is_emphasised(bop):
    """Popping the stack on the closing tag is what stops emphasis leaking rightwards."""
    paragraph = body_paragraph(bop, "plain <b>bold</b> plain again")

    marked = [run.text for run in paragraph.runs if run.bold]
    assert marked == ["bold"]


def test_empty_text_adds_no_run_at_all(bop):
    """Otherwise a stray empty <w:r> would be written for every zero-length data chunk."""
    document = Document()
    paragraph = document.add_paragraph()
    emitter = bop["emit"]._DocxEmitter(paragraph)

    emitter._emit("")

    assert paragraph.runs == []


def test_a_leading_break_is_dropped(bop):
    """A break before any content would open the paragraph with a blank line."""
    paragraph = body_paragraph(bop, "<br>after the break")

    assert paragraph.text == "after the break"
    assert not paragraph.text.startswith("\n")


def test_a_break_between_words_is_kept(bop):
    paragraph = body_paragraph(bop, "before<br>after")

    assert "before" in paragraph.text
    assert "after" in paragraph.text
    assert len(paragraph.runs) >= 3  # before, the break run, after


def test_sibling_paragraphs_are_separated_by_a_break(bop):
    """Everything is emitted into ONE docx paragraph, so <p> boundaries need a break run."""
    paragraph = body_paragraph(bop, "<p>first</p><p>second</p>")

    assert "first" in paragraph.text
    assert "second" in paragraph.text
    # A <w:br> in the middle, not two separate docx paragraphs.
    assert len(paragraph._p.xpath(".//w:br")) == 1


def test_a_leading_paragraph_tag_does_not_add_a_break(bop):
    paragraph = body_paragraph(bop, "<p>only one</p>")

    assert paragraph._p.xpath(".//w:br") == []


def test_a_div_behaves_as_a_paragraph(bop):
    paragraph = body_paragraph(bop, "<div>first</div><div>second</div>")

    assert len(paragraph._p.xpath(".//w:br")) == 1


def test_an_unknown_tag_contributes_its_text_and_no_formatting(bop):
    """Editors emit <span>/<font> wrappers; dropping their text would lose content."""
    paragraph = body_paragraph(bop, '<span style="color:red">kept</span>')

    assert paragraph.text == "kept"
    assert paragraph.runs[0].bold is None


def test_an_unbalanced_closing_tag_does_not_raise(bop):
    """Pasted HTML is routinely malformed; the build must not fail on it."""
    paragraph = body_paragraph(bop, "</b></b>text")

    assert paragraph.text == "text"


def test_a_data_list_bullet_wins_over_its_ordered_parent(bop):
    """Editors emit every list as <ol> and mark bullets per-<li>; honouring it stops an
    edited bullet list rendering as "1. 2. 3."."""
    paragraph = body_paragraph(
        bop, '<ol><li data-list="bullet">First</li><li data-list="bullet">Second</li></ol>'
    )

    assert "• First" in paragraph.text
    assert "1." not in paragraph.text


def test_an_ordered_item_outside_any_list_numbers_itself_one(bop):
    """A fragment pasted without its <ol> wrapper still has to render as a step."""
    paragraph = body_paragraph(bop, '<li data-list="ordered">Orphan step</li>')

    assert "1. Orphan step" in paragraph.text


def test_nested_lists_are_indented_by_depth(bop):
    paragraph = body_paragraph(
        bop, "<ul><li>Outer</li><ul><li>Inner</li></ul></ul>"
    )

    assert "• Outer" in paragraph.text
    assert "  • Inner" in paragraph.text


def test_each_ordered_list_counts_from_one_again(bop):
    """The counter is pushed and popped per list, so a second <ol> restarts."""
    paragraph = body_paragraph(
        bop, "<ol><li>A</li><li>B</li></ol><ol><li>C</li></ol>"
    )

    assert "1. A" in paragraph.text
    assert "2. B" in paragraph.text
    assert paragraph.text.count("1. ") == 2


def test_no_html_at_all_leaves_the_paragraph_untouched(bop):
    """`html_to_runs(None)` is reachable from any absent optional field."""
    document = Document()
    paragraph = document.add_paragraph()

    bop["emit"].html_to_runs(paragraph, None)

    assert paragraph.runs == []


# ═══════════════════════════════════════════════════════════════════════════════
# emit.py — building blocks
# ═══════════════════════════════════════════════════════════════════════════════


def test_the_templates_own_content_is_stripped_before_anything_is_appended(bop):
    """The template is opened for its named styles only; its boilerplate must not ship."""
    template = Document()
    template.add_paragraph("Template boilerplate that must not survive.")
    table = template.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Template table"

    built = reopen(
        bop["emit"].build_docx(bop["schema"].empty_document(), save(template))
    )

    assert "Template boilerplate" not in all_text(built)
    assert built.tables == []


def test_plain_text_is_emitted_verbatim_rather_than_parsed_as_html(bop):
    """Generated sections are not always HTML; the plain branch must not lose them."""
    document = bop["schema"].empty_document()
    document["purpose"] = "Angle-free prose about the unit."

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    assert "Angle-free prose about the unit." in all_text(built)


def test_an_empty_body_adds_no_paragraph(bop):
    document = Document()

    assert bop["emit"]._add_body(document, "", parent_level=1) is None
    assert document.paragraphs == []


@pytest.mark.parametrize("junk", [None, "", "   ", "\n\t "])
def test_a_blank_list_entry_is_dropped(bop, junk):
    """Empty entries survive editing; each would otherwise render as a lone bullet."""
    document = Document()

    bop["emit"]._add_bullets(document, ["Real entry", junk])

    assert [p.text for p in reopen(save(document)).paragraphs] == ["Real entry"]


def test_a_non_string_list_entry_is_coerced(bop):
    """The LLM occasionally returns numbers or dicts where a list of strings was asked for."""
    document = Document()

    bop["emit"]._add_bullets(document, [7, {"part": "valve"}])

    text = all_text(reopen(save(document)))
    assert "7" in text
    assert "valve" in text


def test_markup_inside_a_bullet_is_rendered_as_formatting(bop):
    """Bullets come from the same rich-text editor as the prose fields."""
    document = bop["schema"].empty_document()
    document["description"]["tools"] = ["<b>Wrench</b> set cat. # 7750"]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    runs = runs_of(built, "Wrench set cat. # 7750")
    assert runs[0].bold is True


def test_bullets_use_the_list_paragraph_style(bop):
    document = Document()

    bop["emit"]._add_bullets(document, ["An entry"])

    assert reopen(save(document)).paragraphs[0].style.name == "List Paragraph"


def test_a_table_with_no_rows_is_not_created(bop):
    """An empty grid under a heading looks like a rendering failure."""
    document = Document()

    bop["emit"]._add_table(document, ["Issue", "Resolution"], [])

    assert document.tables == []


def test_a_template_without_table_grid_still_produces_a_table(bop):
    """The style is cosmetic; losing it must not lose the troubleshooting content."""
    template = Document()
    template.styles["Table Grid"].delete()
    document = bop["schema"].empty_document()
    document["operating_procedure"]["troubleshooting"] = [
        {"issue": "No pressure", "resolution": "Check the seal"}
    ]

    built = reopen(bop["emit"].build_docx(document, save(template)))

    cells = [cell.text for cell in built.tables[0].rows[1].cells]
    assert cells == ["No pressure", "Check the seal"]


def test_markup_inside_a_table_cell_is_rendered_as_formatting(bop):
    document = bop["schema"].empty_document()
    document["operating_procedure"]["troubleshooting"] = [
        {"issue": "<b>No pressure</b>", "resolution": "Check the seal"}
    ]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    cell = built.tables[0].rows[1].cells[0]
    assert cell.text == "No pressure"
    assert visible_runs(cell.paragraphs[0])[0].bold is True


def test_a_none_table_value_becomes_an_empty_cell(bop):
    """`cell.text = None` raises in python-docx, so the coercion is load-bearing."""
    document = Document()

    bop["emit"]._add_table(document, ["A", "B"], [["kept", None]])

    built = reopen(save(document))
    assert [cell.text for cell in built.tables[0].rows[1].cells] == ["kept", ""]


def test_table_headers_are_bold(bop):
    document = Document()

    bop["emit"]._add_table(document, ["Issue", "Resolution"], [["a", "b"]])

    header = reopen(save(document)).tables[0].rows[0].cells[0]
    assert visible_runs(header.paragraphs[0])[0].bold is True


# ── _phase_groups ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phase,expected",
    [
        ({"equipment": ["e"], "software": ["s"]}, (["e"], ["s"])),
        ({"equipment": None, "software": None}, ([], [])),
        # A scalar where a list was asked for still has to render.
        ({"equipment": "one step", "software": "one click"}, (["one step"], ["one click"])),
        (["bare", "list"], (["bare", "list"])),
        ("a single narrative step", (["a single narrative step"], [])),
        ("   ", ([], [])),
        ("", ([], [])),
        (None, ([], [])),
        (42, ([], [])),
    ],
)
def test_a_phase_is_normalised_into_equipment_and_software(bop, phase, expected):
    """The generator has returned every one of these shapes for a phase."""
    equipment, software = bop["emit"]._phase_groups(phase)

    if isinstance(expected, tuple) and len(expected) == 2:
        assert (equipment, software) == expected
    else:
        assert equipment == expected


def test_a_bare_list_phase_renders_as_equipment(bop):
    """Older runs stored each phase as a flat list; those documents still have to build."""
    document = bop["schema"].empty_document()
    document["operating_procedure"]["setup"] = ["Insert the wool", "Seat the lid"]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    assert "Insert the wool" in all_text(built)
    assert "Equipment" in headings_at(built, 3)


def test_a_phase_with_software_emits_a_software_heading_and_its_steps(bop):
    document = bop["schema"].empty_document()
    document["operating_procedure"]["startup"] = {
        "equipment": ["Open the valve"],
        "software": ["Press Run", "Confirm the recipe"],
    }

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    assert headings_at(built, 3).count("Software") == 1
    text = all_text(built)
    assert "Press Run" in text
    assert "Confirm the recipe" in text


# ── row shapes ────────────────────────────────────────────────────────────────


def test_a_troubleshooting_row_may_be_a_pair_instead_of_a_dict(bop):
    """The generator returns both shapes depending on how the prompt lands."""
    document = bop["schema"].empty_document()
    document["operating_procedure"]["troubleshooting"] = [
        ("Leaking seal", "Replace the gasket"),
        ["Overheating", "Check the chiller"],
    ]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    rows = [[cell.text for cell in row.cells] for row in built.tables[0].rows[1:]]
    assert rows == [
        ["Leaking seal", "Replace the gasket"],
        ["Overheating", "Check the chiller"],
    ]


@pytest.mark.parametrize(
    "entry", [("bad",), "a string", 17, None, {"issue": "only an issue"}]
)
def test_an_unusable_troubleshooting_entry_is_skipped_not_fatal(bop, entry):
    document = bop["schema"].empty_document()
    document["operating_procedure"]["troubleshooting"] = [entry]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    if isinstance(entry, dict):
        assert [c.text for c in built.tables[0].rows[1].cells] == ["only an issue", ""]
    else:
        # No usable rows means no table is created at all, and an empty document has no
        # abbreviations either, so the built file carries none.
        assert built.tables == []


def test_an_abbreviation_row_may_be_a_dict_instead_of_a_pair(bop):
    document = bop["schema"].empty_document()
    document["abbreviations"] = [
        {"acronym": "CIP", "definition": "Clean In Place"},
        ["SFE", "Supercritical Fluid Extraction"],
    ]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    rows = [[cell.text for cell in row.cells] for row in built.tables[-1].rows[1:]]
    assert rows == [
        ["CIP", "Clean In Place"],
        ["SFE", "Supercritical Fluid Extraction"],
    ]


def test_an_abbreviation_dict_missing_its_keys_yields_empty_cells(bop):
    document = bop["schema"].empty_document()
    document["abbreviations"] = [{"unexpected": "shape"}]

    built = reopen(bop["emit"].build_docx(document, blank_template()))

    assert [cell.text for cell in built.tables[-1].rows[1].cells] == ["", ""]


# ═══════════════════════════════════════════════════════════════════════════════
# gold.py — load_gold_doc, which the existing gold tests bypass entirely
# ═══════════════════════════════════════════════════════════════════════════════


class GoldAssets:
    """`read` is the only method `load_gold_doc` touches. `payload` may be an exception."""

    def __init__(self, payload):
        self.payload = payload
        self.reads: list[tuple[str, bool]] = []

    def read(self, relative: str, cache: bool = True) -> bytes:
        self.reads.append((relative, cache))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class GoldCtx:
    def __init__(self, payload):
        self.assets = GoldAssets(payload)


def gold_docx(build) -> bytes:
    document = Document()
    build(document)
    return save(document)


def add_rows(document, rows: list[list[str]]) -> None:
    table = document.add_table(rows=len(rows), cols=max(len(row) for row in rows))
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            table.cell(row_index, column_index).text = value


def realistic_gold() -> bytes:
    """A miniature of the real gold: numbered H1s over two-column label/body tables."""

    def build(document):
        document.add_heading("1.0 General Information", level=1)
        add_rows(
            document,
            [
                ["1.1 Purpose", "This document describes the unit."],
                ["1.2 Scope", "Applies to the extraction skid. It excludes utilities."],
                [
                    "1.7 Safety",
                    "Hazards | pressurisation | Engineering Controls | relief valve "
                    "| PPE | gloves and goggles",
                ],
                ["1.8 Equipment", "Wrench set cat. # 7750 | Spanner | Pressure gauge"],
            ],
        )
        document.add_heading("3.0 Operation", level=1)
        add_rows(
            document,
            [
                ["3.1 Set up", "Seat the vessel and connect the lines."],
                # Newline-delimited, as the real gold writes it.
                ["3.2 Start up", "Start up\nEnergise the panel\nOperation\nHold pressure"],
                [
                    "3.3 Shut down",
                    "Normal Shut down | vent slowly | Emergency shut down | strike ESD",
                ],
                ["3.4 Maintenance", "Grease the bearings quarterly."],
            ],
        )
        document.add_heading("4.0 Related Documents", level=1)
        add_rows(
            document,
            [["SOP-1", "Cleaning procedure"], ["SOP-2", "Calibration procedure"]],
        )

    return gold_docx(build)


def test_a_read_failure_falls_back_to_no_gold(bop):
    """Storage is remote; an unavailable gold must degrade to static bounds, not error."""
    assert bop["gold"].load_gold_doc(GoldCtx(RuntimeError("no such key"))) is None


def test_the_gold_is_read_with_caching_disabled(bop):
    """It is ~25 MB — caching the document rather than the derived bounds is the bug."""
    ctx = GoldCtx(realistic_gold())

    bop["gold"].load_gold_doc(ctx)

    assert ctx.assets.reads == [(bop["gold"].GOLD_ASSET, False)]


def test_an_unparseable_gold_falls_back_to_no_gold(bop):
    """A truncated or replaced-by-HTML object reaches python-docx as garbage."""
    assert bop["gold"].load_gold_doc(GoldCtx(b"this is not a zip archive")) is None


def test_a_heading_one_opens_a_group(bop):
    parsed = bop["gold"].load_gold_doc(
        GoldCtx(gold_docx(lambda d: d.add_heading("1.0 General", level=1)))
    )

    assert parsed == {"1.0 General": {}}


def test_a_blank_heading_does_not_open_a_group(bop):
    """Word documents carry styled-but-empty spacer headings."""
    parsed = bop["gold"].load_gold_doc(
        GoldCtx(gold_docx(lambda d: d.add_heading("   ", level=1)))
    )

    assert parsed == {}


def test_a_heading_two_becomes_a_labelled_empty_row(bop):
    """H2s are sub-section markers; the label has to exist even with no body cell."""

    def build(document):
        document.add_heading("3.0 Operation", level=1)
        document.add_heading("Start up", level=2)

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed == {"3.0 Operation": {"Start up": ""}}


def test_a_heading_two_before_any_heading_one_is_dropped(bop):
    """There is nothing to hang it from, and guessing would misattribute its body."""
    parsed = bop["gold"].load_gold_doc(
        GoldCtx(gold_docx(lambda d: d.add_heading("Orphan", level=2)))
    )

    assert parsed == {}


def test_ordinary_paragraphs_are_ignored(bop):
    """Only headings and table rows carry structure; prose between them is boilerplate."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        document.add_paragraph("Uncontrolled when printed.")

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed == {"1.0 General": {}}


def test_a_table_row_becomes_a_label_to_body_mapping(bop):
    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.1 Purpose", "The purpose text."]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"]["1.1 Purpose"] == "The purpose text."


def test_extra_columns_are_joined_with_pipes(bop):
    """Pipe-joining is what lets `split_inline_subblocks` treat cells uniformly."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.7 Safety", "Hazards", "Controls", "PPE"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"]["1.7 Safety"] == "Hazards | Controls | PPE"


def test_cell_text_is_stripped(bop):
    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["  1.1 Purpose  ", "\n The purpose text. \n"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"]["1.1 Purpose"] == "The purpose text."


def test_a_table_before_any_heading_is_dropped(bop):
    """Cover-page tables arrive first and belong to no section."""

    def build(document):
        add_rows(document, [["Document number", "SOP-0001"]])
        document.add_heading("1.0 General", level=1)

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed == {"1.0 General": {}}


def test_a_single_column_row_is_dropped(bop):
    """A one-column row is a banner, not a label/body pair."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["Section 1 banner"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"] == {}


def test_a_row_with_no_label_is_dropped(bop):
    """Continuation rows have an empty first cell; keying on "" would collide."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["", "orphaned body text"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"] == {}


def test_a_row_with_no_body_is_dropped(bop):
    """A label with nothing beside it would derive a "0 character" bound."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.1 Purpose", ""]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"] == {}


def test_rows_are_grouped_under_the_most_recent_heading(bop):
    """Document order, not table order, is what decides ownership."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.1 Purpose", "first"]])
        document.add_heading("3.0 Operation", level=1)
        add_rows(document, [["3.1 Set up", "second"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed == {
        "1.0 General": {"1.1 Purpose": "first"},
        "3.0 Operation": {"3.1 Set up": "second"},
    }


def test_a_row_with_no_cells_at_all_is_tolerated(bop):
    """A zero-column table is degenerate but real: Word writes them for layout frames."""

    def build(document):
        document.add_heading("1.0 General", level=1)
        document.add_table(rows=1, cols=0)

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed == {"1.0 General": {}}


def test_a_paragraph_iterator_that_runs_short_loses_content_rather_than_raising(
    bop, monkeypatch
):
    """The mis-alignment the module docstring warns about, forced.

    The walker advances two independent iterators (`.paragraphs`, `.tables`) alongside a
    walk of the body XML, so anything that makes python-docx report fewer paragraphs than
    the body contains desynchronises them. That cannot happen with a plain document, so
    it is provoked with a shim; the point is that the guard degrades to dropped headings
    instead of a StopIteration escaping into the generate stage.
    """
    import docx

    payload = gold_docx(
        lambda d: [d.add_heading("1.0 General", level=1), d.add_heading("3.0 Op", level=1)]
    )
    real = docx.Document

    class ShortParagraphs:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.element = wrapped.element
            self.paragraphs = wrapped.paragraphs[:1]
            self.tables = wrapped.tables

    monkeypatch.setattr(docx, "Document", lambda stream: ShortParagraphs(real(stream)))

    parsed = bop["gold"].load_gold_doc(GoldCtx(payload))

    assert parsed == {"1.0 General": {}}


def test_a_table_iterator_that_runs_short_loses_the_table_rather_than_raising(
    bop, monkeypatch
):
    import docx

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.1 Purpose", "kept"]])
        add_rows(document, [["1.2 Scope", "lost"]])

    payload = gold_docx(build)
    real = docx.Document

    class ShortTables:
        def __init__(self, wrapped):
            self.element = wrapped.element
            self.paragraphs = wrapped.paragraphs
            self.tables = wrapped.tables[:1]

    monkeypatch.setattr(docx, "Document", lambda stream: ShortTables(real(stream)))

    parsed = bop["gold"].load_gold_doc(GoldCtx(payload))

    assert parsed == {"1.0 General": {"1.1 Purpose": "kept"}}


def test_a_later_row_with_the_same_label_wins(bop):
    """Characterization: labels are a dict key, so a duplicated label overwrites.

    The gold has no duplicates today, so this pins the behaviour rather than endorsing
    it; a gold that repeats "1.1 Purpose" under one H1 would silently lose the first.
    """

    def build(document):
        document.add_heading("1.0 General", level=1)
        add_rows(document, [["1.1 Purpose", "first"], ["1.1 Purpose", "second"]])

    parsed = bop["gold"].load_gold_doc(GoldCtx(gold_docx(build)))

    assert parsed["1.0 General"]["1.1 Purpose"] == "second"


# ═══════════════════════════════════════════════════════════════════════════════
# gold.py — resolving a parsed gold, end to end
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def parsed_gold(bop):
    return bop["gold"].load_gold_doc(GoldCtx(realistic_gold()))


@pytest.mark.parametrize(
    "leaf,expected",
    [
        ("purpose", "This document describes the unit."),
        ("description", "Wrench set cat. # 7750 | Spanner | Pressure gauge"),
        ("safety.hazards", "pressurisation"),
        ("safety.engineering_controls", "relief valve"),
        ("safety.ppe", "gloves and goggles"),
        ("operating_procedure.setup", "Seat the vessel and connect the lines."),
        ("operating_procedure.maintenance", "Grease the bearings quarterly."),
    ],
)
def test_a_mapped_leaf_resolves_out_of_a_parsed_gold(bop, parsed_gold, leaf, expected):
    assert bop["gold"].resolve_gold_section(parsed_gold, leaf) == expected


def test_a_whole_heading_leaf_joins_every_body_cell_under_it(bop, parsed_gold):
    """`related_documents` has no label, so 4.0's rows are concatenated."""
    resolved = bop["gold"].resolve_gold_section(parsed_gold, "related_documents")

    assert resolved == "Cleaning procedure | Calibration procedure"


def test_a_whole_heading_leaf_resolves_to_empty_when_the_heading_is_bare(bop):
    gold = bop["gold"]
    document = {"4.0 Related Documents": {"A label": ""}}

    assert gold.resolve_gold_section(document, "related_documents") == ""


def test_a_newline_delimited_cell_is_normalised_before_splitting(bop, parsed_gold):
    """The real gold writes sub-blocks one per line; the splitter only knows pipes."""
    gold = bop["gold"]

    assert gold.resolve_gold_section(parsed_gold, "operating_procedure.startup") == (
        "Energise the panel"
    )
    assert gold.resolve_gold_section(parsed_gold, "operating_procedure.operation") == (
        "Hold pressure"
    )


def test_an_absent_label_resolves_to_empty(bop):
    """A gold that renames a row must not raise; the static bound takes over."""
    gold = bop["gold"]
    document = {"1.0 General": {"9.9 Something Else": "body"}}

    assert gold.resolve_gold_section(document, "purpose") == ""


def test_a_non_string_cell_is_not_matched(bop):
    """The walker only ever writes strings, but the resolver is defensive about it."""
    gold = bop["gold"]
    document = {"1.0 General": {"1.1 Purpose": ["not", "a", "string"]}}

    assert gold.resolve_gold_section(document, "purpose") == ""


def test_the_emergency_shutdown_sub_block_resolves_on_its_own(bop, parsed_gold):
    resolved = bop["gold"].resolve_gold_section(
        parsed_gold, "operating_procedure.emergency_shutdown"
    )

    assert resolved == "strike ESD"


def test_the_normal_shutdown_sub_block_swallows_the_emergency_text(bop, parsed_gold):
    """SUSPECTED DEFECT — gold.py:249,254 and gold.py:56-60.

    `shutdown_headers` is spelled ["Normal Shut down", "Emergency shut down"], but
    `GOLD_SECTION_MAP["operating_procedure.shutdown"]` abbreviates its `sub` to
    "Normal Shut". "Normal Shut" is not a member of `shutdown_headers`, so the
    `elif sub in shutdown_headers` arm at gold.py:254 never fires for the real map and
    the `else` at gold.py:257 hands `split_inline_subblocks` a single-header list. With
    only "Normal Shut" as a header, "Emergency shut down" is no longer recognised as a
    boundary and is buffered into the Normal Shutdown body.

    Correct behaviour would be `sub == "Normal Shut down"` in the map (or prefix
    matching against `shutdown_headers`), yielding just "vent slowly". `shutdown_headers`
    is dead code as written.
    """
    resolved = bop["gold"].resolve_gold_section(
        parsed_gold, "operating_procedure.shutdown"
    )

    assert resolved == "vent slowly | Emergency shut down | strike ESD"


def test_a_fully_spelled_shutdown_sub_splits_cleanly(bop, parsed_gold, monkeypatch):
    """The counterpart to the defect above: the shutdown_headers arm works when reached.

    Monkeypatching the map to the full spelling shows the bug is in the map entry, not
    in the splitter.
    """
    gold = bop["gold"]
    monkeypatch.setitem(
        gold.GOLD_SECTION_MAP,
        "operating_procedure.shutdown",
        {"h1": "3.0", "label": "3.3 Shut", "sub": "Normal Shut down"},
    )

    resolved = gold.resolve_gold_section(parsed_gold, "operating_procedure.shutdown")

    assert resolved == "vent slowly"


# ═══════════════════════════════════════════════════════════════════════════════
# gold.py — compute_gold_bounds over a real gold
# ═══════════════════════════════════════════════════════════════════════════════


def test_the_bounds_derived_from_a_realistic_gold(bop, parsed_gold):
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    assert set(bounds) == {
        "purpose",
        "scope",
        "related_documents",
        "description",
        "safety",
        "operating_procedure",
    }


def test_the_abbreviations_bound_is_never_derived(bop, parsed_gold):
    """Deliberate: the gold's 1.3 Definitions cell is near-empty and would derive a
    "1 sentence" bound that contradicts the [acronym, definition] pair schema."""
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    assert "abbreviations" not in bounds
    assert "abbreviations" in bop["gold"].SECTION_BOUNDS


def test_the_description_bound_falls_back_to_the_equipment_cell(bop, parsed_gold):
    """SUSPECTED DEFECT — gold.py:336-341.

    `TOP_TO_LEAVES["description"]` lists description.tools / spare_parts / consumables /
    materials, and `GOLD_SECTION_MAP` maps none of them. So the loop at gold.py:336
    can never append and the "Tools" fallback at gold.py:345-348 is the only path that
    ever runs — gold.py:340-341 is unreachable as shipped. The fallback happens to
    produce the right shape, which is why this has gone unnoticed.
    """
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    # Three entries in the 1.8 Equipment cell.
    assert "Tools: concise list of approximately 3±2 entries" in bounds["description"]
    assert "Spare Parts" not in bounds["description"]


def test_a_mapped_description_leaf_would_replace_the_fallback(bop, parsed_gold, monkeypatch):
    """The counterpart to the defect above: the per-leaf loop works once a leaf is mapped."""
    gold = bop["gold"]
    monkeypatch.setitem(
        gold.GOLD_SECTION_MAP,
        "description.tools",
        {"h1": "1.0", "label": "1.8 Equipment"},
    )

    bounds = gold.compute_gold_bounds(parsed_gold)

    assert "Tools: concise list of approximately 3±2 entries" in bounds["description"]
    # The fallback would not have added the verbatim-numbers note.
    assert "include it verbatim" in bounds["description"]


def test_the_description_bound_demands_verbatim_identifiers(bop, parsed_gold):
    """Part and catalog numbers are the whole value of the Tools list to a technician."""
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    assert "include it verbatim" in bounds["description"]
    assert "EXACTLY" in bounds["description"]


def test_no_description_text_derives_no_description_bound(bop):
    """Neither the per-leaf loop nor the fallback resolves, so the static bound stands."""
    gold = bop["gold"]
    document = {"1.0 General": {"1.1 Purpose": "Only a purpose."}}

    assert "description" not in gold.compute_gold_bounds(document)


def test_the_safety_bound_names_every_resolvable_sub_block(bop, parsed_gold):
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    for label in ("Hazards", "Engineering Controls", "PPE"):
        assert f"{label}: one short paragraph per category" in bounds["safety"]


def test_only_the_resolvable_safety_sub_blocks_appear(bop):
    """A gold that omits PPE must not emit a bound for it."""
    gold = bop["gold"]
    document = {"1.0 General": {"1.7 Safety": "Hazards | pressurisation"}}

    bounds = gold.compute_gold_bounds(document)

    assert "Hazards" in bounds["safety"]
    assert "PPE:" not in bounds["safety"]


def test_no_safety_text_derives_no_safety_bound(bop):
    gold = bop["gold"]

    assert "safety" not in gold.compute_gold_bounds({"1.0 General": {"1.1 Purpose": "x"}})


def test_the_operating_procedure_bound_is_the_flat_narrative_rule(bop, parsed_gold):
    """Emitted ONCE regardless of how many phases resolve: per-leaf emission bloated the
    prompt, and a character target there made the model truncate at max_tokens."""
    gold = bop["gold"]
    bounds = gold.compute_gold_bounds(parsed_gold)

    assert bounds["operating_procedure"] == gold.OP_BOUND


def test_one_resolvable_phase_is_enough_to_derive_the_procedure_bound(bop):
    gold = bop["gold"]
    document = {"3.0 Operation": {"3.1 Set up": "Seat the vessel."}}

    assert gold.compute_gold_bounds(document)["operating_procedure"] == gold.OP_BOUND


def test_no_resolvable_phase_derives_no_procedure_bound(bop):
    gold = bop["gold"]

    assert "operating_procedure" not in gold.compute_gold_bounds(
        {"1.0 General": {"1.1 Purpose": "x"}}
    )


def test_the_scope_bound_counts_the_sentences_it_found(bop, parsed_gold):
    """The count is what keeps a generated scope as short as the human-authored one."""
    bounds = bop["gold"].compute_gold_bounds(parsed_gold)

    assert "1–2 sentences" in bounds["scope"]


# ═══════════════════════════════════════════════════════════════════════════════
# gold.py — get_section_bounds over a real gold
# ═══════════════════════════════════════════════════════════════════════════════


def test_a_real_gold_layers_derived_bounds_over_the_static_ones(bop):
    """The whole point of the module: derived where measured, static everywhere else."""
    gold = bop["gold"]

    bounds = gold.get_section_bounds(GoldCtx(realistic_gold()))

    assert set(gold.SECTION_BOUNDS).issubset(set(bounds))
    assert bounds["purpose"] != gold.SECTION_BOUNDS["purpose"]
    assert bounds["abbreviations"] == gold.SECTION_BOUNDS["abbreviations"]


def test_the_gold_is_read_once_across_many_section_lookups(bop):
    """A 25 MB read per section is the cost this cache exists to avoid."""
    gold = bop["gold"]
    ctx = GoldCtx(realistic_gold())

    for _ in range(5):
        gold.get_section_bounds(ctx)

    assert len(ctx.assets.reads) == 1
