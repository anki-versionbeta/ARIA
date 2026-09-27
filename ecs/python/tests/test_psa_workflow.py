"""Identifying which product an uploaded document is about.

`workflow.py` wires the other modules together for the document-driven path: given a PDD, a TPP
or a slide deck, work out which catalogue product it describes, then extract, populate and verify.

The identification is the part worth testing hard, because it has already been wrong in
production. A Gate Review deck names half a dozen comparator products in its body text, and
scoring body mentions equally with the filename made those decks resolve to whichever comparator
was mentioned most. The fix is the weighting these tests pin:

    FILENAME_WEIGHT = 100    a hit in the filename counts 100x a body mention
    TITLE_WEIGHT     = 8     a hit on the first page / slide counts 8x
    MIN_SCORE        = 12    body-only confidence floor
    MARGIN           = 1.6   the winner must beat the runner-up by this factor

and the rule stated above them: *"A confident catalogue match must be backed by the SUBJECT's
program code appearing in the filename / title (authoritative), or a clear name match there —
never by comparator names mentioned only in the body."*

The three decisions `identify` can reach are all legitimate, and conflating them is the failure
mode: `smartsheet` (a confident catalogue hit), `new_product` (a real program code that the
catalogue does not know yet) and `undetermined` (ask the human). Guessing in place of
`undetermined` is what produces an assessment of the wrong product.

The scorers take text, so most of this needs no files. `_score_products` and `identify` read the
catalogue, so they get a real SQLite database from the shipped DDL.
"""

from __future__ import annotations

import sqlite3

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
    from da_silos.psa import asset_store, workflow

    return {"workflow": workflow, "asset_store": asset_store}


@pytest.fixture
def con(psa):
    connection = sqlite3.connect(":memory:")
    connection.executescript(psa["asset_store"].read_text("schema.sql"))
    yield connection
    connection.close()


def add_product(con, product_id, program_no, program_name="", **columns):
    row = {"product_id": product_id, "program_no": program_no,
           "program_name": program_name, "batch_type": "Commercial",
           "status": "Commercial", "modality": "Liquid", "form": "Liquid",
           "vial_container_size": "2R", **columns}
    keys = ", ".join(row)
    con.execute(f"INSERT INTO product ({keys}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))
    con.commit()
    return product_id


def docs(filename="deck.pptx", title="", body=""):
    """The read-document shape `_score_products` and `identify` consume.

    Built directly rather than by writing files, because `_read_docs` is the PyMuPDF /
    python-pptx boundary and the scoring is what these tests are about. Both the raw and the
    normalised forms are present: the scorer compares on the normalised ones, while `identify`
    scans the raw ones for program codes with `CODE_RE` (which needs the hyphens).
    """
    return {"raw_names": filename, "raw_titles": title, "raw_body": body,
            "fn": psa_norm(filename), "title": psa_norm(title), "body": psa_norm(body)}


def psa_norm(text):
    import re

    return re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()


# ── classifying an upload ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["CMC TPP for BoNTE.pdf", "tpp_draft.docx",
                                  "Product-TPP-v3.pdf"])
def test_a_tpp_is_recognised_by_its_filename(psa, name):
    """The role decides which extractor runs, and the TPP and PDD extractors look for
    different fields."""
    assert psa["workflow"].classify_role(name) == "TPP"


def test_the_role_is_matched_case_insensitively(psa):
    assert psa["workflow"].classify_role("CMC Tpp.pdf") == "TPP"


@pytest.mark.parametrize("name", ["Product Definition 3 for BoNTE.pdf", "deck.pptx",
                                  "notes.docx", "anything.pdf"])
def test_anything_else_defaults_to_the_pdd_slot(psa, name):
    """A default rather than a rejection: an unrecognised upload should still be read, and
    the PDD extractor is the general one."""
    assert psa["workflow"].classify_role(name) == "PDD"


def test_the_role_ignores_the_directory(psa):
    """A path containing 'tpp' as a folder name must not reclassify the file."""
    assert psa["workflow"].classify_role("/uploads/tpp/Product Definition.pdf") == "PDD"


# ── finding program codes in text ─────────────────────────────────────────────


@pytest.mark.parametrize("code", ["ABBV-400", "ABBV-CLS-123", "AGN-151586", "ABT-199",
                                  "RGX-314"])
def test_the_code_families_the_business_uses_are_recognised(psa, code):
    """Five prefixes appear across the portfolio, and a code the regex misses cannot become a
    `new_product` decision — it would fall through to `undetermined`."""
    assert psa["workflow"]._codes_in(f"Assessment for {code} rev 2") == [code]


def test_a_code_is_upper_cased(psa):
    """So it compares against the normalised catalogue key."""
    assert psa["workflow"]._codes_in("abbv-400") == ["ABBV-400"]


def test_codes_are_returned_in_the_order_they_appear(psa):
    """The first is treated as the subject on the `new_product` path, and a deck names the
    subject before its comparators."""
    assert psa["workflow"]._codes_in("ABBV-400 vs AGN-151586") == ["ABBV-400", "AGN-151586"]


def test_a_repeated_code_appears_once(psa):
    """A code repeated in a header on every page must not look like several products."""
    assert psa["workflow"]._codes_in("ABBV-400 ... ABBV-400 ... ABBV-400") == ["ABBV-400"]


def test_text_with_no_code_yields_none(psa):
    assert psa["workflow"]._codes_in("A deck about vials") == []


def test_no_text_yields_no_codes(psa):
    assert psa["workflow"]._codes_in(None) == []
    assert psa["workflow"]._codes_in("") == []


# ── the matching tokens ───────────────────────────────────────────────────────


def test_the_program_code_is_a_token(psa):
    assert psa["workflow"]._norm("ABBV-400") in psa["workflow"]._tokens("ABBV-400", "")


def test_the_program_name_and_its_words_are_tokens(psa):
    """A document may name the product rather than its code, so each word of the name is a
    candidate on its own."""
    tokens = REDACTED

    assert psa["workflow"]._norm("EtentamigSolution") in tokens
    assert "ETENTAMIG" in tokens


def test_short_tokens_are_discarded(psa):
    """A three-letter token matches inside unrelated words and would score every document
    against every product."""
    tokens = REDACTED

    assert all(len(t) >= 4 for t in tokens)


def test_a_product_with_no_names_has_no_tokens(psa):
    """It can then only be reached by an explicit selection, which is correct — there is
    nothing to match on."""
    assert psa["workflow"]._tokens("", "") == set()


# ── the weighting, which is what fixed the mis-identifications ────────────────


def test_the_weights_are_the_calibrated_ones(psa):
    """Pinned because they encode a production incident: a Gate Review deck names several
    comparator products in its body, and equal weighting made those decks resolve to whichever
    comparator was mentioned most."""
    assert psa["workflow"].FILENAME_WEIGHT == 100
    assert psa["workflow"].TITLE_WEIGHT == 8
    assert psa["workflow"].MIN_SCORE == 12
    assert psa["workflow"].MARGIN == 1.6


def test_a_filename_hit_outscores_a_body_mention(psa, con):
    """The whole point of the weighting. The subject is named in the filename; a comparator is
    discussed at length inside."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    add_product(con, 2, "AGN-151586", "BoNTE")

    scored = psa["workflow"]._score_products(
        con, docs(filename="ABBV-400 assessment.pptx", body="AGN-151586 " * 20)
    )

    assert scored[0]["program_no"] == "ABBV-400"


def test_a_filename_hit_is_marked_strong(psa, con):
    """`strong` is what lets `identify` accept a match at all; a body-only hit must clear the
    score floor and the margin instead."""
    add_product(con, 1, "ABBV-400", "Etentamig")

    scored = psa["workflow"]._score_products(con, docs(filename="ABBV-400 deck.pptx"))

    assert scored[0]["strong"] is True


def test_a_title_hit_is_marked_strong(psa, con):
    """The first page or slide is the document's own statement of what it is about."""
    add_product(con, 1, "ABBV-400", "Etentamig")

    scored = psa["workflow"]._score_products(
        con, docs(filename="deck.pptx", title="ABBV-400 Gate Review")
    )

    assert scored[0]["strong"] is True


def test_a_body_only_hit_is_not_strong(psa, con):
    """This is the mis-identification, pinned as the rule that prevents it: a comparator named
    only in the body is never authoritative, however often it appears."""
    add_product(con, 1, "ABBV-400", "Etentamig")

    scored = psa["workflow"]._score_products(
        con, docs(filename="deck.pptx", body="ABBV-400 " * 50)
    )

    assert scored[0]["strong"] is False


def test_a_name_match_in_the_filename_is_strong(psa, con):
    """Documents are often named after the product rather than its code."""
    add_product(con, 1, "ABBV-400", "Etentamig")

    scored = psa["workflow"]._score_products(con, docs(filename="Etentamig PDD.pdf"))

    assert scored[0]["strong"] is True


def test_a_product_mentioned_nowhere_is_not_scored(psa, con):
    """An unscored product cannot win, which keeps the alternatives list meaningful."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    add_product(con, 2, "AGN-151586", "BoNTE")

    scored = psa["workflow"]._score_products(con, docs(filename="ABBV-400.pptx"))

    assert [r["program_no"] for r in scored] == ["ABBV-400"]


def test_the_scores_are_ordered_highest_first(psa, con):
    add_product(con, 1, "ABBV-400", "Etentamig")
    add_product(con, 2, "AGN-151586", "BoNTE")

    scored = psa["workflow"]._score_products(
        con, docs(filename="ABBV-400.pptx", body="AGN-151586")
    )

    assert [r["score"] for r in scored] == sorted(
        [r["score"] for r in scored], reverse=True
    )


def test_a_longer_code_scores_higher_than_a_shorter_one(psa, con):
    """Score is weighted by token length, so a specific code beats an incidental short match
    at the same position."""
    add_product(con, 1, "ABBV-CLS-1234", "Long")
    add_product(con, 2, "ABBV-400", "Short")

    scored = psa["workflow"]._score_products(
        con, docs(filename="ABBV-CLS-1234 and ABBV-400.pptx")
    )

    assert scored[0]["program_no"] == "ABBV-CLS-1234"


def test_an_empty_catalogue_scores_nothing(psa, con):
    """Which sends `identify` down the `new_product` or `undetermined` path rather than
    raising — a first run against an unbuilt database reaches this."""
    assert psa["workflow"]._score_products(con, docs(filename="ABBV-400.pptx")) == []


# ── the three decisions ───────────────────────────────────────────────────────


# A body long enough to clear the unreadable guard (20 non-whitespace characters), with no
# program code in it, so it cannot influence which product wins.
READABLE = "This deck describes the vial presentation and its packaging configuration."


def test_a_filename_code_resolves_to_the_catalogue_product(psa, con, monkeypatch):
    """The confident path: the code is in the filename and the catalogue knows it."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="ABBV-400 assessment.pptx", title="ABBV-400",
                       body=READABLE),
    )

    result = psa["workflow"].identify(["ABBV-400 assessment.pptx"], con)

    assert result["decision"] == "smartsheet"
    assert result["program_no"] == "ABBV-400"
    assert result["reason"] == "code-in-filename"


def test_a_code_the_catalogue_does_not_know_is_a_new_product(psa, con, monkeypatch):
    """A real program code that the Smartsheet has not caught up with is not an error and not
    a reason to guess at a neighbour — it is a new product."""
    add_product(con, 1, "AGN-151586", "BoNTE")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="RGX-314 assessment.pptx", title="RGX-314", body=READABLE),
    )

    result = psa["workflow"].identify(["RGX-314 assessment.pptx"], con)

    assert result["decision"] == "new_product"
    assert result["subject_code"] == "RGX-314"


def test_a_code_in_the_body_alone_can_still_be_a_new_product(psa, con, monkeypatch):
    """A code the catalogue does not know is unambiguous wherever it appears — there is no
    comparator it could be confused with, which is what makes this safe when the same
    evidence in the catalogue case is not."""
    add_product(con, 1, "AGN-151586", "BoNTE")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="deck.pptx", body=f"{READABLE} Subject is RGX-314."),
    )

    result = psa["workflow"].identify(["deck.pptx"], con)

    assert result["decision"] == "new_product"
    assert result["subject_code"] == "RGX-314"


def test_an_unreadable_document_is_undetermined_before_anything_is_scored(psa, con,
                                                                         monkeypatch):
    """An image-only PDF or a failed read yields almost no text, and the filename alone is
    not evidence enough — a file called 'ABBV-400.pdf' whose contents could not be read has
    not been shown to be about ABBV-400."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="ABBV-400 assessment.pptx", title="ABBV-400", body=""),
    )

    result = psa["workflow"].identify(["ABBV-400 assessment.pptx"], con)

    assert result["decision"] == "undetermined"
    assert result["reason"] == "unreadable"


def test_a_short_failure_message_is_recognised_as_unreadable(psa, con, monkeypatch):
    """`read_text` returns "(could not read ...)" on failure, which is itself long enough to
    clear the 20-character floor — so the guard strips that marker before measuring."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="ABBV-400.pdf", title="ABBV-400",
                       body="(could not read a.pdf)"),
    )

    result = psa["workflow"].identify(["ABBV-400.pdf"], con)

    assert result["decision"] == "undetermined"
    assert result["reason"] == "unreadable"


def test_a_long_failure_message_still_reaches_the_filename_code(psa, con, monkeypatch):
    """Characterising the edge of that guard rather than asserting an ideal. Only the
    `"(could not read"` prefix is stripped, so the rest of the message — a long path and the
    OS reason — can carry the remainder past the 20-character floor. The run then falls
    through to the authoritative filename code.

    That is a benign outcome here (the filename code and the catalogue agree, and the
    extraction simply finds no fields to add), but it means the unreadable guard is
    path-length dependent. Recorded so a future tightening is a deliberate change with this
    test to update, not a surprise.
    """
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="ABBV-400.pdf", title="ABBV-400",
                       body="(could not read /a/very/long/path/to/ABBV-400.pdf: "
                            "no such file or directory)"),
    )

    result = psa["workflow"].identify(["ABBV-400.pdf"], con)

    assert result["decision"] == "smartsheet"
    assert result["program_no"] == "ABBV-400", "the filename code, which is authoritative"


def test_a_document_naming_nothing_is_undetermined(psa, con, monkeypatch):
    """Asking the human is the right answer. Guessing here is what produces an assessment of
    the wrong product."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="untitled.pptx", body="a deck about vials"),
    )

    result = psa["workflow"].identify(["untitled.pptx"], con)

    assert result["decision"] == "undetermined"


def test_a_body_only_mention_does_not_win_confidently(psa, con, monkeypatch):
    """The regression test for the Gate-Review-deck incident: the only evidence is comparator
    names in the body, so the answer must not be a confident catalogue match."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="gate review deck.pptx", body="ABBV-400 " * 3),
    )

    result = psa["workflow"].identify(["gate review deck.pptx"], con)

    assert result["decision"] == "undetermined"


def test_the_decision_explains_itself(psa, con, monkeypatch):
    """It is shown to whoever has to resolve an `undetermined`, and "could not identify" with
    no reason gives them nothing to act on."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs", lambda d: docs(filename="untitled.pptx")
    )

    result = psa["workflow"].identify(["untitled.pptx"], con)

    assert result["reason"]


def test_the_alternatives_are_offered_for_a_human_to_choose_from(psa, con, monkeypatch):
    """When the answer is uncertain, the ranked runners-up are the useful output."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    add_product(con, 2, "AGN-151586", "BoNTE")
    monkeypatch.setattr(
        psa["workflow"], "_read_docs",
        lambda d: docs(filename="deck.pptx", body="ABBV-400 AGN-151586"),
    )

    result = psa["workflow"].identify(["deck.pptx"], con)

    assert "alternatives" in result


# ── the presentations of an identified program ────────────────────────────────


def test_the_presentations_of_a_program_are_listed(psa, con):
    """A program has several presentations and the assessment is about exactly one, so this
    is what the screen offers when the document does not pin it down."""
    add_product(con, 1, "ABBV-400", "Etentamig", vial_container_size="2R")
    add_product(con, 2, "ABBV-400", "Etentamig", vial_container_size="10R")

    rows = psa["workflow"].list_presentations("ABBV-400", con)

    assert len(rows) == 2


def test_a_clinical_presentation_is_not_offered(psa, con):
    """The same scope rule as everywhere else: a clinical-stage presentation is out of scope,
    so offering it would let an assessment be started on one."""
    add_product(con, 1, "ABBV-400", "Etentamig")
    add_product(con, 2, "ABBV-400", "Etentamig", batch_type="Clinical")

    assert len(psa["workflow"].list_presentations("ABBV-400", con)) == 1


def test_an_unknown_program_has_no_presentations(psa, con):
    assert psa["workflow"].list_presentations("NOPE-000", con) == []


def test_the_product_list_names_each_program(psa, con):
    add_product(con, 1, "ABBV-400", "Etentamig")

    assert ("ABBV-400", "Etentamig") in psa["workflow"].list_products(con)


def presentations():
    """Two presentations in the shape `list_presentations` returns."""
    return [{"product_id": 1, "source_row": 20, "list_number": "NDC-111", "label": "2R"},
            {"product_id": 2, "source_row": 21, "list_number": "NDC-222", "label": "10R"}]


def test_a_presentation_is_matched_by_its_row(psa):
    """`source_row` is the key that survives a database rebuild, unlike `product_id` — which
    is why the selector is the row and the ANSWER is this run's product_id."""
    assert psa["workflow"]._match_presentation(presentations(), 21) == 2


def test_a_presentation_is_matched_by_a_string_row(psa):
    """The screen may send it as a string from a form."""
    assert psa["workflow"]._match_presentation(presentations(), "20") == 1


def test_a_presentation_is_matched_by_its_list_number(psa):
    """The other stable key: an assessor is far more likely to know the list number than our
    internal Smartsheet row."""
    assert psa["workflow"]._match_presentation(presentations(), "NDC-222") == 2


def test_a_list_number_is_matched_regardless_of_punctuation(psa):
    """It is transcribed by hand, so the hyphens are not to be depended on."""
    assert psa["workflow"]._match_presentation(presentations(), "ndc222") == 2


def test_an_unmatched_selection_is_reported_rather_than_guessed(psa):
    """A stale row after a rebuild must not silently assess a different presentation."""
    assert psa["workflow"]._match_presentation(presentations(), 999) is None


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_no_selection_matches_nothing(psa, empty):
    """Which sends the caller to the picker rather than defaulting to the first row."""
    assert psa["workflow"]._match_presentation(presentations(), empty) is None


# ── reading text out of an upload ─────────────────────────────────────────────


def test_a_docx_upload_is_read(psa, tmp_path):
    """Only .pdf, .docx and .pptx are read; anything else is not a document this recognises."""
    from docx import Document

    path = tmp_path / "notes.docx"
    document = Document()
    document.add_paragraph("Dosage Form: Liquid")
    document.save(str(path))

    assert "Dosage Form" in psa["workflow"].read_text(str(path))


def test_a_docx_table_is_read_as_well_as_its_paragraphs(psa, tmp_path):
    """Part A/B fields in a PDD are usually in a table, so a reader that only took paragraphs
    would find nothing in the documents that matter most."""
    from docx import Document

    path = tmp_path / "notes.docx"
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Dosage Form"
    table.rows[0].cells[1].text = "Lyophilised powder"
    document.save(str(path))

    text = psa["workflow"].read_text(str(path))

    assert "Lyophilised powder" in text


def test_an_unreadable_file_reports_the_problem_instead_of_raising(psa, tmp_path):
    """A corrupt or password-protected upload must not fail the run before the Smartsheet
    fields — which are the primary source — have been used. The reason is returned as text so
    it lands in the extraction log rather than vanishing, and it cannot match a program code,
    so it cannot influence identification."""
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not really a pdf")

    text = psa["workflow"].read_text(str(path))

    assert "could not read" in text
    assert psa["workflow"]._codes_in(text) == []


def test_a_missing_file_reports_the_problem(psa, tmp_path):
    text = psa["workflow"].read_text(str(tmp_path / "absent.pdf"))

    assert "could not read" in text


def test_an_unrecognised_extension_yields_no_text(psa, tmp_path):
    """A .txt or a .xlsx is not one of the three document types the extractors understand."""
    path = tmp_path / "notes.txt"
    path.write_text("Dosage Form: Liquid", encoding="utf-8")

    assert psa["workflow"].read_text(str(path)) == ""
