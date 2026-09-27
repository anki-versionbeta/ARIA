"""Rendering: the assessment form's cells, and the cap-recommendation export's swatches.

Both modules produce a .docx that a person signs, so the tests read the produced document back
rather than asserting on intermediate state.

Two decisions in here were made because the first version looked right and was not, and both are
pinned:

  * **The ballot boxes carry the template's own font.** `BOX_RFONTS` is copied verbatim from the
    blank template's four `☐` runs — MS Gothic with `w:hint="eastAsia"`, which is what tells Word
    to resolve U+2610/U+2612 through the eastAsia font rather than the Latin one. Without it the
    glyph renders as a box-with-a-question-mark on the approved form.

  * **A cap swatch is a PNG chip, not cell shading** (commit 1af6adb). Cell shading cannot show a
    white or transparent cap at all — a white fill is invisible against white paper — and a
    missing hex used to become flat #EEEEEE, which reads as a real grey cap rather than "unknown".
    The chip carries a border, and no-hex draws the web's 45° hatch.

The `_risk_box` helper returning None for anything other than yes/no is the third thing worth
holding onto: an unrecognised decision leaves the cell as the template had it, so a form never
shows a ticked box that no assessment produced.
"""

from __future__ import annotations

import dataclasses
import io

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import asset_store, cap_export, config, populate_template

    return {"template": populate_template, "export": cap_export,
            "asset_store": asset_store, "config": config}


@pytest.fixture(autouse=True)
def restore_config(psa):
    before = psa["config"]._INJECTED
    yield
    psa["config"]._INJECTED = before


@pytest.fixture
def workspace(psa, tmp_path):
    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(),
        work_dir=str(tmp_path),
        db_path=str(tmp_path / "Database" / "psa.db"),
        output_dir=str(tmp_path / "Output_Files"),
        uploads_dir=str(tmp_path / "uploads"),
        storage_dir=str(tmp_path / "store"),
    ))
    return tmp_path


# ── the site-code cell ────────────────────────────────────────────────────────


def test_a_single_site_code_is_kept(psa):
    assert psa["template"]._codes("AP16") == ["AP16"]


@pytest.mark.parametrize("cell", ["AP16, LU", "AP16\nLU"])
def test_multiple_site_codes_are_split(psa, cell):
    """Each site gets its own Part E block, so a missed split drops a site from a signed form."""
    assert psa["template"]._codes(cell) == ["AP16", "LU"]


@pytest.mark.parametrize("placeholder", ["", "TBD", "TBC", "N/A", "TO BE ADDED",
                                         "N/A - not yet selected"])
def test_a_placeholder_site_is_dropped(psa, placeholder):
    """A Part E block headed 'TBD' would ask a site that does not exist for a signature."""
    assert psa["template"]._codes(placeholder) == []


def test_a_placeholder_among_real_codes_is_dropped(psa):
    assert psa["template"]._codes("AP16, TBD, LU") == ["AP16", "LU"]


def test_no_cell_yields_no_codes(psa):
    assert psa["template"]._codes(None) == []


def test_the_known_sites_have_readable_names(psa):
    """A site CODE means nothing to an external reviewer, so the form shows the full name."""
    assert psa["template"].SITE_NAMES["AP16"] == "AbbVie US, AP16"
    assert psa["template"].SITE_NAMES["LU"] == "AbbVie DE, LU"


# ── the product family label ──────────────────────────────────────────────────


def test_a_lyophilised_form_gets_the_lyophilised_family(psa):
    assert psa["template"]._family("Lyo Powder") == "Lyophilized drug products"


def test_the_family_match_is_case_insensitive(psa):
    """The sheet's spelling of the form is hand-entered."""
    assert psa["template"]._family("LYOPHILISED CAKE") == "Lyophilized drug products"


def test_another_form_is_described_by_its_own_name(psa):
    assert psa["template"]._family("Liquid") == "Liquid drug products"


@pytest.mark.parametrize("empty", ["", None])
def test_an_unknown_form_is_marked_for_attention(psa, empty):
    """TBD prompts the assessor. A blank cell in a signed form reads as a considered answer."""
    assert psa["template"]._family(empty) == "TBD"


# ── the similarity-risk box ───────────────────────────────────────────────────


def test_a_yes_decision_ticks_yes(psa):
    box = psa["template"]._risk_box("Yes")

    assert box.index("☒") < box.index("☐"), "the tick must be on Yes"


def test_a_no_decision_ticks_no(psa):
    box = psa["template"]._risk_box("No")

    assert box.index("☐") < box.index("☒"), "the tick must be on No"


@pytest.mark.parametrize("written", ["yes", "YES", "  Yes  "])
def test_the_decision_is_read_case_insensitively(psa, written):
    assert psa["template"]._risk_box(written) == psa["template"]._risk_box("Yes")


def test_exactly_one_box_is_ticked(psa):
    """`verify.py` checks for a `☒`, and two would mean the form asserts both answers."""
    for decision in ("Yes", "No"):
        assert psa["template"]._risk_box(decision).count("☒") == 1


@pytest.mark.parametrize("unknown", ["", None, "maybe", "TBD", "N/A"])
def test_an_unrecognised_decision_leaves_the_cell_alone(psa, unknown):
    """None means "write nothing", so the template's own unticked boxes survive. A form must
    never show a ticked box that no assessment produced."""
    assert psa["template"]._risk_box(unknown) is None


def test_the_box_glyphs_are_the_documented_codepoints(psa):
    """U+2612 and U+2610. Pinned because the whole font workaround below is about these two
    characters specifically."""
    assert psa["template"].BOX_GLYPHS == ("☒", "☐")


def test_the_box_font_is_the_templates_own(psa):
    """Copied verbatim from the blank template's four `☐` runs, which is why the boxes render
    exactly as they do on the approved form. MS Gothic contains U+2610/U+2612, and
    `w:hint="eastAsia"` is what tells Word to resolve these through the eastAsia font rather
    than the Latin one — without it the glyph renders as a missing-character box."""
    rfonts = psa["template"].BOX_RFONTS

    assert rfonts["w:eastAsia"] == "MS Gothic"
    assert rfonts["w:hint"] == "eastAsia"


def test_the_shipped_template_really_uses_that_font_for_its_boxes(psa):
    """The claim the constant rests on, checked against the asset rather than trusted.

    A .docx is a zip, so the document XML has to be inflated before the font name is
    searchable. If the template is ever re-issued with a different font, the generated boxes
    would silently stop matching the rest of the form.
    """
    import zipfile

    with zipfile.ZipFile(io.BytesIO(psa["asset_store"].read("PSA_template.docx"))) as bundle:
        xml = bundle.read("word/document.xml").decode("utf-8")

    assert "MS Gothic" in xml
    assert 'w:hint="eastAsia"' in xml
    assert "☐" in xml, "the unticked boxes the generator ticks"


# ── writing into a cell ───────────────────────────────────────────────────────


@pytest.fixture
def document(psa):
    """A one-cell table to write into, in a real docx."""
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=1, cols=1)
    return doc, table.rows[0].cells[0]


def test_text_written_to_a_cell_can_be_read_back(psa, document):
    _doc, cell = document

    psa["template"]._set_cell_text(cell, "AbbVie US, AP16")

    assert cell.text == "AbbVie US, AP16"


def test_writing_replaces_rather_than_appends(psa, document):
    """The template's cells are not empty — they hold labels and placeholder text — so a
    generator that appended would produce "Product Name Product Name Etentamig"."""
    _doc, cell = document
    psa["template"]._set_cell_text(cell, "first")

    psa["template"]._set_cell_text(cell, "second")

    assert cell.text == "second"


def test_a_multi_line_value_keeps_its_lines(psa, document):
    """Site lists and comments are multi-line, and collapsing them would run an address into
    the next site's name."""
    _doc, cell = document

    psa["template"]._set_cell_text(cell, "AbbVie US, AP16\nAbbVie DE, LU")

    assert cell.text.count("\n") == 1


def test_a_ticked_box_survives_the_round_trip(psa, document):
    """The end-to-end version of the font workaround: the glyph has to still be there, in its
    own run, after being written."""
    _doc, cell = document

    psa["template"]._set_cell_text(cell, psa["template"]._risk_box("Yes"))

    assert "☒" in cell.text
    assert "☐" in cell.text


def test_the_box_run_carries_the_font_override(psa, document):
    """Checked in the XML, because this is invisible in the text and is exactly what broke
    when the boxes were first written as plain text."""
    _doc, cell = document

    psa["template"]._set_cell_text(cell, psa["template"]._risk_box("No"))

    xml = cell._tc.xml
    assert "MS Gothic" in xml
    assert 'w:hint="eastAsia"' in xml


def test_a_value_with_no_box_needs_no_font_override(psa, document):
    """The override is applied per-run to the box glyphs, so ordinary text is untouched and
    keeps the template's own styling."""
    _doc, cell = document

    psa["template"]._set_cell_text(cell, "Liquid drug products")

    assert "MS Gothic" not in cell._tc.xml


# ── the cap swatch chip ───────────────────────────────────────────────────────


def test_a_hex_becomes_rgb_fractions(psa):
    assert psa["export"]._rgb01("#ff0000") == (1.0, 0.0, 0.0)


def test_a_hex_without_its_hash_is_accepted(psa):
    """The palette CSV and the sheet disagree about the leading '#'."""
    assert psa["export"]._rgb01("00ff00") == (0.0, 1.0, 0.0)


@pytest.mark.parametrize("bad", ["", None, "#fff", "#gggggg", "not a colour"])
def test_a_value_that_is_not_a_solid_hex_has_no_rgb(psa, bad):
    """Which is what selects the hatch instead of a fill — never a default grey."""
    assert psa["export"]._rgb01(bad) is None


def test_a_chip_is_a_png(psa):
    """`\\x89PNG` is the magic number. The chip is embedded as an image, so a malformed one
    would fail inside python-docx rather than render badly."""
    chip = psa["export"]._chip_png("#0000ff", psa["export"]._CHIP_IN)

    assert chip[:4] == b"\x89PNG"


def test_a_chip_is_produced_for_a_colour_with_no_hex(psa):
    """The case cell shading could not express at all: an unknown cap draws the web's 45°
    hatch, so it reads as "unknown" rather than as a real grey cap."""
    chip = psa["export"]._chip_png("", psa["export"]._CHIP_IN)

    assert chip[:4] == b"\x89PNG"


def test_a_white_cap_still_produces_a_visible_chip(psa):
    """The other case shading could not express: a white fill is invisible against white
    paper, which is why every chip carries a 1px container border."""
    white = psa["export"]._chip_png("#ffffff", psa["export"]._CHIP_IN)
    blue = psa["export"]._chip_png("#0000ff", psa["export"]._CHIP_IN)

    assert white[:4] == b"\x89PNG"
    assert white != blue


def test_different_colours_give_different_chips(psa):
    """The chips are cached on (hex, size), so a cache keyed too loosely would render every
    cap the same colour."""
    assert psa["export"]._chip_png("#ff0000", 0.36) != \
        psa["export"]._chip_png("#0000ff", 0.36)


def test_the_same_colour_and_size_is_cached(psa):
    """`lru_cache` — a recommendation table renders the same few colours repeatedly."""
    first = psa["export"]._chip_png("#123456", 0.36)
    second = psa["export"]._chip_png("#123456", 0.36)

    assert first is second


def test_the_chip_size_distinguishes_the_two_grids(psa):
    """The recommended grid uses the larger chip, matching the web's `large` swatch."""
    assert psa["export"]._CHIP_IN_LARGE > psa["export"]._CHIP_IN
    assert psa["export"]._chip_png("#123456", psa["export"]._CHIP_IN) != \
        psa["export"]._chip_png("#123456", psa["export"]._CHIP_IN_LARGE)


def test_a_chip_is_rendered_at_print_resolution(psa):
    """72 dpi would look ragged in a printed governance document."""
    assert psa["export"]._CHIP_DPI >= 200


def test_the_risk_colours_are_the_design_systems(psa):
    """The export mirrors the on-screen view, so the same three risk colours are used and a
    reviewer reading the file sees what the assessor saw."""
    assert set(psa["export"]._RISK_RGB) == {"High", "Medium", "Low"}


# ── the whole assessment document ─────────────────────────────────────────────


def test_the_template_opens_from_the_shipped_asset(psa):
    """Everything below depends on this, and a template that does not open is a report-time
    failure rather than a start-up one."""
    from docx import Document

    doc = Document(io.BytesIO(psa["asset_store"].read("PSA_template.docx")))

    assert len(doc.tables) >= 4


def test_the_template_has_a_part_e_table_to_clone_per_site(psa):
    """QPP11-04-001-G004 §3: each AbbVie manufacturing site completes a separate Part E, and
    the single shipped table is what gets cloned."""
    from docx import Document

    doc = Document(io.BytesIO(psa["asset_store"].read("PSA_template.docx")))

    assert len(doc.tables) >= 4, "tables 0-3 are Parts A/B, C, D and the first Part E"


def test_a_pending_tag_names_what_is_missing_and_where_to_find_it(psa):
    """A bare "TBD" tells the assessor nothing. These say which document supplies the value,
    and `verify.py` accepts a PENDING tag in the photo cell for the same reason."""
    assert "PDD" in psa["template"].PDD_ADDR
    assert "PENDING" in psa["template"].PDD_ADDR
    assert "PENDING" in psa["template"].STRENGTH_REVIEW


def test_the_skipped_site_tokens_match_the_ingest_rules(psa):
    """Two modules decide what counts as "no site". If they disagreed, a site dropped at
    ingest could reappear as a Part E block here, or the reverse."""
    from da_silos.psa.ingest_smartsheet import SKIP_SITE as ingest_skip

    assert psa["template"].SKIP_SITE <= ingest_skip | {""}
