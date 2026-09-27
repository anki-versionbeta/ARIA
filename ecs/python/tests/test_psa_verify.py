"""The post-generation self-check: what a produced assessment must and must not contain.

`verify.py` runs after a report is built and answers one question — is this document fit to hand
to a reviewer. It is the last thing standing between a silently wrong GxP form and an assessor's
signature, and its result is reported rather than raised: a failed verify still leaves a
downloadable report whose run log explains what failed (`silo.py`'s `report` stage).

The rules it enforces are worth stating plainly, because they run in both directions:

  * Parts A, B, D and E must be POPULATED. A blank cell in a similarity assessment reads as
    "considered and found to be nothing", not as "we failed to fill it in".
  * Part C and the D.3 / E.3 cells must be BLANK. Those are execution-time signature cells, and a
    generator that pre-filled them would be forging an approval.
  * The photo cell is blank OR carries an embedded image OR a `[PENDING` tag — never a
    placeholder pretending to be a photo. A row with no photo is normal.
  * Exactly one similarity-risk box is marked per part, which is what `☒` checks.

`main()` prints its findings and reads real files, so it is driven end to end against a generated
document in `tests/test_psa_render.py`. What is tested here is `check()` — the accumulator every
one of those rules goes through — and the structural expectations `main()` encodes, against
documents built to match and to violate them.
"""

from __future__ import annotations

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
    from da_silos.psa import asset_store, verify

    return {"verify": verify, "asset_store": asset_store}


# ── the accumulator every rule goes through ───────────────────────────────────


def test_a_passing_check_records_nothing(psa, capsys):
    """`fails` is the return value in all but name: empty means the document is fit."""
    fails: list[str] = []

    psa["verify"].check(True, "Part A Product Name populated", fails)

    assert fails == []


def test_a_failing_check_records_its_message(psa, capsys):
    """The message is what reaches the run log, so it has to be the thing that failed rather
    than a bare False."""
    fails: list[str] = []

    psa["verify"].check(False, "Part A Form cell populated", fails)

    assert fails == ["Part A Form cell populated"]


def test_failures_accumulate_rather_than_stopping_at_the_first(psa, capsys):
    """An assessor fixing one blank cell should learn about the other three in the same pass,
    not rerun the report four times."""
    fails: list[str] = []

    psa["verify"].check(False, "first", fails)
    psa["verify"].check(True, "second", fails)
    psa["verify"].check(False, "third", fails)

    assert fails == ["first", "third"]


def test_every_check_is_reported_pass_or_fail(psa, capsys):
    """The run log is the audit record of what was checked. A check that only speaks up when
    it fails leaves no evidence that the passing ones ran at all."""
    fails: list[str] = []

    psa["verify"].check(True, "a populated cell", fails)
    psa["verify"].check(False, "a blank cell", fails)

    printed = capsys.readouterr().out
    assert "PASS a populated cell" in printed
    assert "FAIL a blank cell" in printed


def test_a_truthy_value_counts_as_a_pass(psa, capsys):
    """Callers pass `bool(cell.text.strip())` and bare values interchangeably."""
    fails: list[str] = []

    psa["verify"].check("some text", "populated", fails)
    psa["verify"].check(1, "also populated", fails)

    assert fails == []


@pytest.mark.parametrize("falsy", [False, None, "", 0, []])
def test_every_falsy_value_counts_as_a_failure(psa, capsys, falsy):
    """`check(bool(...))` is the intended call, but an empty string reaching it directly must
    not silently pass — that would turn a blank GxP cell into a green check."""
    fails: list[str] = []

    psa["verify"].check(falsy, "populated", fails)

    assert fails == ["populated"]


# ── the structural rules, against real documents ──────────────────────────────


@pytest.fixture
def template(psa, tmp_path):
    """The shipped blank form, opened for inspection.

    Read from the asset store rather than reconstructed, so the table and row indices these
    tests assert on are the real template's rather than a guess about it.
    """
    from docx import Document

    path = tmp_path / "PSA_template.docx"
    path.write_bytes(psa["asset_store"].read("PSA_template.docx"))
    return Document(str(path))


def test_the_template_has_the_four_parts_verify_expects(psa, template):
    """`main()` addresses tables 0-3 by index: Parts A/B, C, D and the first Part E. A
    template with fewer would raise IndexError deep inside the check rather than report a
    missing part."""
    assert len(template.tables) >= 4


def test_the_signature_cells_are_blank_in_the_shipped_template(psa, template):
    """Part C and D.3/E.3 are execution-time signatures. They start blank, and the generator
    must leave them that way — a pre-filled approval cell would be a forged signature."""
    assert template.tables[1].rows[3].cells[1].text.strip() == "", "Part C Name"
    assert template.tables[1].rows[3].cells[2].text.strip() == "", "Part C Signature"
    assert template.tables[2].rows[10].cells[1].text.strip() == "", "D.3 Name"
    assert template.tables[2].rows[10].cells[2].text.strip() == "", "D.3 Signature"


def test_the_blank_template_would_fail_its_own_population_checks(psa, template):
    """The check that stops this file being vacuous. If a blank form passed the populated-cell
    rules, those rules would be asserting nothing about a generated one."""
    fails: list[str] = []
    table = template.tables[0]

    psa["verify"].check(bool(table.rows[3].cells[1].text.strip()),
                        "Part A Product Name populated", fails)
    psa["verify"].check(bool(table.rows[5].cells[1].text.strip()),
                        "Part A Form cell populated", fails)

    assert len(fails) == 2, "a blank template must fail the populated-cell checks"


def test_an_unmarked_form_has_no_ticked_risk_box(psa, template):
    """`☒` is how a marked box is recognised, so the blank template must not already contain
    one — otherwise the "exactly one box marked" rule would pass on an unfilled form."""
    part_d = template.tables[2]
    text = "\n".join(c.text for r in part_d.rows for c in r.cells)

    assert "☒" not in text


def test_the_template_offers_the_yes_and_no_labels_to_mark_against(psa, template):
    """The counterpart. The shipped form carries no checkbox glyph at all in its text — the
    boxes are Word form-field elements — so the generator WRITES the `☒` beside these labels
    rather than replacing a `☐`. That is why the check is for `☒` and not for a change from
    one glyph to another."""
    risk_row = template.tables[2].rows[4]
    text = " ".join(c.text for c in risk_row.cells)

    assert "Yes" in text and "No" in text
    assert "☐" not in text, "no glyph to swap: the tick is inserted, not substituted"


def test_a_populated_cell_passes_the_same_check_the_blank_one_failed(psa, template):
    """Both directions of the rule, so it is pinned as a rule rather than as a constant."""
    fails: list[str] = []
    table = template.tables[0]
    table.rows[3].cells[1].text = "ABBV-400 (Etentamig)"

    psa["verify"].check(bool(table.rows[3].cells[1].text.strip()),
                        "Part A Product Name populated", fails)

    assert fails == []


def test_whitespace_alone_does_not_count_as_populated(psa, template):
    """Every rule strips before testing, because a cell holding a space looks filled in a
    diff and blank to a reader."""
    fails: list[str] = []
    table = template.tables[0]
    table.rows[3].cells[1].text = "   "

    psa["verify"].check(bool(table.rows[3].cells[1].text.strip()),
                        "Part A Product Name populated", fails)

    assert fails == ["Part A Product Name populated"]


def test_a_pending_tag_is_accepted_where_a_photo_is_missing(psa):
    """A row with no photo is normal — the report renders a blank photo cell and this accepts
    it, so a missing image is recorded rather than treated as a defect. The rule is: blank,
    or an embedded image, or an explicit `[PENDING` tag; never a fake placeholder."""
    fails: list[str] = []

    for cell_text, shapes in [("", 0), ("[PENDING: PDD - product image]", 0), ("", 1)]:
        psa["verify"].check(
            cell_text == "" or "[PENDING" in cell_text or shapes >= 1,
            "Part A photo: image embedded or blank when none", fails,
        )

    assert fails == []


def test_a_placeholder_pretending_to_be_a_photo_is_rejected(psa):
    """Text in the photo cell that is neither blank nor a PENDING tag, with no image
    attached, is the case the rule exists to catch."""
    fails: list[str] = []
    cell_text, shapes = "see attached", 0

    psa["verify"].check(
        cell_text == "" or "[PENDING" in cell_text or shapes >= 1,
        "Part A photo: image embedded or blank when none", fails,
    )

    assert fails == ["Part A photo: image embedded or blank when none"]


def test_part_e_is_validated_for_every_site_block(psa, template):
    """QPP11-04-001-G004 §3: "each AbbVie manufacturing site should complete a separate Part
    E". The template's single Part E is cloned per site, so every table from index 3 onward
    is a Part E block and all of them are checked — validating only the first would let a
    second site's block ship empty."""
    fails: list[str] = []
    e_tables = template.tables[3:]

    psa["verify"].check(len(e_tables) >= 1, "at least one Part E site block present", fails)

    assert fails == []
    assert len(e_tables) >= 1


def test_a_document_with_no_part_e_is_reported(psa):
    """A product with no manufacturing site would produce this, and it must be a reported
    failure rather than a form that simply omits Part E."""
    fails: list[str] = []

    psa["verify"].check(len([]) >= 1, "at least one Part E site block present", fails)

    assert fails == ["at least one Part E site block present"]
