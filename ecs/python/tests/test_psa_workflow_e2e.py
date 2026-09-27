"""The document-driven run and the data layer beneath it, end to end and offline.

`tests/test_psa_workflow.py` stops at the pure halves of `workflow.py` — the scorers, the role
classifier, the presentation matcher — with `_read_docs` monkeypatched away. That leaves the parts
that actually touch files and the database untested: `read_text` against real PDF / DOCX / PPTX
bytes, `_read_docs`, `_pick_presentation`, `load_new_product`, `process_documents` and `main`. This
file covers those, plus `engines.py`, which is the whole data layer `router.py` sits on and has no
tests of its own.

Everything runs against the shared catalogue from `tests/psa_fixtures.py`, which is built offline
through the real ingest path — so an identification, a presentation pick and a report here all read
the same six products a production run would, loaded by production code.

**Nothing here touches the network.** `psa_fixtures.psa_config` supplies a token and a sheet id (it
has to: other PSA tests exercise the credential path), so `ingest_smartsheet_api.available()` would
otherwise be True and `process_documents(rebuild=True)` / `recommendation(refresh_first=True)` would
issue a REST read. The `offline` fixture forces `ingest_source="xlsx"`, which is the real switch that
turns that off, and then replaces `urllib.request.urlopen` with a stub that fails the test — so a
lost guard is a red test rather than a request leaving the process. The three tests that DO want a
live read opt back in explicitly, with `urlopen` replaced by a recording stub, and assert on the URL
that was requested.

Only two things are stubbed beyond that boundary, both because they cannot be driven from outside:
`risk.assess_program` is made to raise (the real engine does not fail against a valid catalogue) and
`verify.main` is made to return non-zero, to reach the two "the run continues and reports it"
branches. `main()`'s argument parsing is tested against a recording `process_documents` because the
exit codes are the behaviour under test, not a second full run.

Two characterisations in here look like source bugs and are marked as such inline — the address
bleed across documents in `load_new_product`, and the collapse of two same-role uploads in
`process_documents`. Both record the ACTUAL behaviour so a fix is a deliberate change with a test to
update.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sqlite3

import pytest
from docx import Document

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.psa_fixtures import (SUBJECT, SUBJECT_ROW, catalogue, con,  # noqa: F401
                                docx_text, empty_db, generated_report, psa_config, row,
                                sheet, snapshot_bytes, subject_product_id)

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import (config, engines, ingest_smartsheet_api, paths, snapshot,
                              workflow)

    return {"workflow": workflow, "engines": engines, "config": config, "paths": paths,
            "snapshot": snapshot, "api": ingest_smartsheet_api}


@pytest.fixture(autouse=True)
def offline(psa, psa_config, monkeypatch):
    """Forbid the live sheet, then forbid the network outright.

    `ingest_source="xlsx"` makes `available()` False, which is what keeps the default
    `rebuild=True` / `refresh_first=True` paths off Smartsheet. The `urlopen` stub is the
    backstop: a lost guard fails the test instead of issuing a REST read.
    """
    psa["config"].set_config(dataclasses.replace(psa_config, ingest_source="xlsx"))
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("the run reached the network"),
    )


@pytest.fixture
def live(psa, psa_config, monkeypatch):
    """Opt back in to the live API, with `urlopen` recorded rather than performed.

    Returns a callable taking the sheet dict the fake API should answer with; it hands back the
    list of requested URLs so a test can assert on what was asked for and how often.
    """
    import urllib.request

    def enable(payload, ingest_source="api"):
        psa["config"].set_config(
            dataclasses.replace(psa_config, ingest_source=ingest_source))
        urls: list[str] = []
        body = json.dumps(payload).encode("utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        def urlopen(request, timeout=None):
            urls.append(getattr(request, "full_url", request))
            return Response()

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return urls

    return enable


@pytest.fixture
def rebuild(psa):
    """Factory: build a catalogue of exactly the given presentations.

    Each argument is the keyword set `psa_fixtures.row` takes, so a test states only the
    attributes it cares about.
    """
    def build(*presentations):
        from da_silos.psa.ingest_smartsheet import COLS

        rows = [row(COLS, n, **spec) for n, spec in enumerate(presentations)]
        return psa["snapshot"].rebuild(snapshot_bytes(sheet(rows)))

    return build


# -- building real uploads -----------------------------------------------------


def make_docx(path, *paragraphs):
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))
    return str(path)


def make_pdf(path, *pages):
    """A real PyMuPDF PDF, one page per argument, written line by line so nothing clips."""
    import fitz

    document = fitz.open()
    for text in pages:
        page = document.new_page()
        y = 60.0
        for line in (text.splitlines() or [""]):
            page.insert_text((50, y), line, fontsize=9)
            y += 12
    document.save(str(path))
    document.close()
    return str(path)


def make_image_only_pdf(path):
    """A PDF with a picture and no text layer — what a scanned upload looks like."""
    import fitz
    from PIL import Image

    document = fitz.open()
    page = document.new_page()
    page.insert_image(fitz.Rect(40, 40, 240, 140), stream=png_bytes())
    document.save(str(path))
    document.close()
    return str(path)


def make_pptx(path, *lines):
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(4))
    frame = box.text_frame
    frame.text = lines[0] if lines else ""
    for line in lines[1:]:
        frame.add_paragraph().text = line
    deck.save(str(path))
    return str(path)


def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 100), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


# A body long enough to clear `identify`'s 20-non-whitespace-character unreadable guard, naming
# no program code and no catalogue product name, so it cannot influence which product wins.
READABLE = "This assessment describes the vial and its closure configuration."

# One page of readable filler, long enough that the 800-character title slice cannot reach the
# following page.
FILLER = "\n".join(["Vial and closure configuration review paragraph."] * 40)


# -- reading an uploaded document ----------------------------------------------


def test_every_page_of_a_pdf_upload_is_read(psa, tmp_path):
    """Identification scores the whole body, so a reader that stopped at page one would miss
    the program code in a deck that names its subject on the second slide."""
    path = make_pdf(tmp_path / "deck.pdf", "First page text", "Second page text")

    text = psa["workflow"].read_text(path)

    assert "First page text" in text
    assert "Second page text" in text


def test_a_corrupt_upload_reports_the_reason_as_text_instead_of_raising(psa, tmp_path):
    """A corrupt upload must not fail the run before the Smartsheet fields — the primary
    source — have been used, and the reason has to land in the log rather than vanish."""
    path = tmp_path / "bad.pptx"
    path.write_bytes(b"this is not a presentation")

    text = psa["workflow"].read_text(str(path))

    assert text.startswith("(could not read bad.pptx:")
    assert "Package not found" in text


def test_an_image_only_pdf_reads_as_no_text_at_all(psa, tmp_path):
    """The scanned-upload case. Empty (not an error) is what makes `identify` answer
    `unreadable` and ask the human, instead of trusting the filename."""
    path = make_image_only_pdf(tmp_path / "scan.pdf")

    assert psa["workflow"].read_text(path) == ""


def test_a_table_on_a_slide_is_read_as_well_as_its_text_boxes(psa, tmp_path):
    """Gate Review decks carry the presentation attributes in a table, so a reader that took
    only the text frames would find nothing in the documents that matter most."""
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    table = slide.shapes.add_table(1, 2, Inches(1), Inches(1), Inches(6),
                                   Inches(1)).table
    table.cell(0, 0).text = "Dosage Form"
    table.cell(0, 1).text = "Lyophilised powder"
    path = str(tmp_path / "deck.pptx")
    deck.save(path)

    assert "Lyophilised powder" in psa["workflow"].read_text(path)


# -- what the identifier is given ---------------------------------------------


def test_the_document_names_are_joined_as_bare_basenames(psa, tmp_path):
    """The filename blob is the authoritative evidence, so a directory component leaking into
    it would let a folder called `ABBV-400` decide which product an upload is about."""
    first = make_docx(tmp_path / "a.docx", READABLE)
    second = make_pdf(tmp_path / "b.pdf", READABLE)

    assert psa["workflow"]._read_docs([first, second])["raw_names"] == "a.docx b.pdf"


def test_only_the_first_eight_hundred_characters_count_as_the_title(psa, tmp_path):
    """The title slice carries eight times a body mention's weight, so its size decides how
    much of a long first page is treated as the document's own statement of its subject."""
    path = make_docx(tmp_path / "long.docx", "X" * 900, "MARKER")

    read = psa["workflow"]._read_docs([path])

    assert read["raw_titles"] == "X" * 800
    assert "MARKER" in read["raw_body"], "the marker is in the body, just not the title"


# -- identifying against the real catalogue -----------------------------------


def test_a_document_named_after_a_catalogue_product_resolves_to_that_program(psa, con,
                                                                            tmp_path):
    """The confident path against a real catalogue: the product's name is in the filename, so
    the answer is a Smartsheet product and no human is asked."""
    path = make_docx(tmp_path / "Product 1 assessment.docx", READABLE)

    result = psa["workflow"].identify([path], con)

    assert result["decision"] == "smartsheet"
    assert result["program_no"] == "ABBV-400"
    assert result["reason"] == "name-in-filename"


def test_a_filename_naming_two_catalogue_products_is_undetermined(psa, con, tmp_path):
    """Two products named equally in the filename is exactly the ambiguity the margin exists
    for — guessing here would assess the wrong product."""
    path = make_docx(tmp_path / "Product 1 and Product 2 review.docx", READABLE)

    result = psa["workflow"].identify([path], con)

    assert result["decision"] == "undetermined"
    assert result["reason"] == "low-confidence"


def test_a_code_the_catalogue_does_not_know_is_a_new_product_even_from_the_body(psa, con,
                                                                               tmp_path):
    """A code with no catalogue entry has no comparator it could be confused with, so a body
    mention is safe evidence here where it would not be for a known product."""
    path = make_pdf(tmp_path / "gate review deck.pdf", FILLER,
                    "The subject of this review is RGX-314.")

    result = psa["workflow"].identify([path], con)

    assert result["decision"] == "new_product"
    assert result["subject_code"] == "RGX-314"
    assert result["reason"] == "code-in-body-not-in-catalogue"


def test_a_scanned_upload_is_undetermined_however_it_is_named(psa, con, tmp_path):
    """A file called after a product whose contents could not be read has not been shown to be
    about that product, and an assessment built on the filename alone is a wrong assessment."""
    path = make_image_only_pdf(tmp_path / "Product 1 assessment.pdf")

    result = psa["workflow"].identify([path], con)

    assert result["decision"] == "undetermined"
    assert result["reason"] == "unreadable"


# -- picking one presentation of a multi-row program --------------------------


def test_a_distinguishing_attribute_in_the_document_picks_that_presentation(psa, rebuild,
                                                                            tmp_path):
    """The document is the baseline. Two presentations of one program differ only by their
    vial, so the vial named in the upload is the whole signal — and picking the other row
    produces an assessment of a presentation nobody asked about."""
    rebuild({"program": SUBJECT, "vial": "2R"}, {"program": SUBJECT, "vial": "10R"})
    path = make_docx(tmp_path / "review.docx", "Container closure: 10R vial")

    with sqlite3.connect(psa["paths"].db_path()) as connection:
        picked, n_rows = psa["workflow"]._pick_presentation(SUBJECT, [path], connection)
        chosen_row = connection.execute(
            "SELECT source_row FROM product WHERE product_id=?", (picked,)).fetchone()[0]

    assert n_rows == 2
    assert chosen_row == SUBJECT_ROW + 1, "the 10R row, not the first one"


def test_a_program_with_no_document_defaults_to_the_first_row_and_says_so(psa, rebuild,
                                                                          capsys):
    """The automated report path passes no documents at all, so this default runs on every
    such run — and it has to be visible in the log, because "presentation 1 of 2" chosen
    silently is indistinguishable from a considered choice."""
    rebuild({"program": SUBJECT, "vial": "2R"}, {"program": SUBJECT, "vial": "10R"})

    with sqlite3.connect(psa["paths"].db_path()) as connection:
        picked, n_rows = psa["workflow"]._pick_presentation(SUBJECT, [], connection)
        chosen_row = connection.execute(
            "SELECT source_row FROM product WHERE product_id=?", (picked,)).fetchone()[0]

    assert (chosen_row, n_rows) == (SUBJECT_ROW, 2)
    assert "no distinguishing signal in the document; defaulted to first row" in \
        capsys.readouterr().out


def test_a_clinical_presentation_is_never_auto_selected(psa, rebuild, capsys):
    """The house scope rule. Dropping the clinical row leaves one candidate, so the scoring
    is skipped entirely — an assessment must not be started on a clinical presentation."""
    rebuild({"program": SUBJECT, "batch_type": "Commercial"},
            {"program": SUBJECT, "batch_type": "Clinical"})
    capsys.readouterr()                      # discard the ingest's own commentary

    with sqlite3.connect(psa["paths"].db_path()) as connection:
        picked, n_rows = psa["workflow"]._pick_presentation(SUBJECT, [], connection)
        batch = connection.execute(
            "SELECT batch_type FROM product WHERE product_id=?", (picked,)).fetchone()[0]

    assert (batch, n_rows) == ("Commercial", 1)
    assert capsys.readouterr().out == "", "one candidate needs no scoring commentary"


def test_an_unknown_program_yields_no_presentation_rather_than_raising(psa, con):
    """Reached by a `--product` override for a code the catalogue does not hold, which the
    caller then handles as a new product."""
    assert psa["workflow"]._pick_presentation("NOPE-000", [], con) == (None, 0)


# -- a subject the Smartsheet does not know yet -------------------------------


def test_a_new_product_records_which_document_it_came_from(psa, con, tmp_path):
    """Provenance is the audit answer to "where did this value come from?", and a deck is
    recorded as PPT rather than as its role so a reviewer can see it was a slide."""
    path = make_pptx(tmp_path / "tpp deck.pptx", "Gate review", "Dosage Form: Liquid")

    pid = psa["workflow"].load_new_product("RGX-314", {"TPP": path}, con)

    source = con.execute(
        "SELECT doc_role, source_type FROM doc_source WHERE product_id=?", (pid,)).fetchone()
    provenance = {r[0] for r in con.execute(
        "SELECT DISTINCT source_doc FROM field_provenance WHERE entity_id=?", (pid,))}
    assert (source["doc_role"], source["source_type"]) == ("TPP", "pptx")
    assert provenance == {"PPT"}


def test_a_new_product_with_nothing_extractable_falls_back_to_the_bare_code(psa, con,
                                                                            tmp_path,
                                                                            capsys):
    """Never fabricate: an unfound field is left NULL so it renders as the template's
    [PENDING]/TBD, and the name defaults to the code rather than to a guess."""
    path = make_pptx(tmp_path / "deck.pptx", "A slide with no labelled fields")

    pid = psa["workflow"].load_new_product("RGX-314", {"PDD": path}, con)

    stored = con.execute(
        "SELECT program_no, program_name, form FROM product WHERE product_id=?",
        (pid,)).fetchone()
    assert (stored["program_no"], stored["program_name"]) == ("RGX-314", "RGX-314")
    assert stored["form"] is None
    assert "extracted nothing -> all [PENDING]/TBD" in capsys.readouterr().out


def test_a_labelled_field_in_an_upload_becomes_the_new_products_value(psa, con, tmp_path):
    """The point of the new-product path: Parts A and B come from the document because there
    is no Smartsheet row to read them from."""
    path = make_docx(tmp_path / "pdd.docx", "Dosage Form: Lyophilised powder",
                     "Route of Administration: Subcutaneous")

    pid = psa["workflow"].load_new_product("RGX-314", {"PDD": path}, con)

    stored = con.execute(
        "SELECT form, route_of_admin FROM product WHERE product_id=?", (pid,)).fetchone()
    assert stored["form"] == "Lyophilised powder"
    assert stored["route_of_admin"] == "Subcutaneous"


def test_a_manufacturing_address_runs_into_the_next_documents_text(psa, con, tmp_path):
    """CHARACTERISATION OF WHAT LOOKS LIKE A BUG (workflow.py:285-291).

    `load_new_product` joins every uploaded document's text into one blob and then extracts a
    single manufacturing address from it. The address regex stops at a blank line, and a
    document boundary is a single newline — so the address captured from the first document
    continues into the second one's opening sentence, and that concatenation is what is
    written to `product.mfr_site_address` and rendered on the signed Part B.

    Recorded as the ACTUAL behaviour rather than worked around. The fix is to extract per
    document, which would make this assertion the wrong one.
    """
    first = make_docx(tmp_path / "pdd.docx",
                      "Site of manufacture: 1 North Waukegan Road, North Chicago, IL 60064")
    second = make_docx(tmp_path / "tpp.docx", "Prepared by the packaging team")

    pid = psa["workflow"].load_new_product(
        "RGX-314", {"PDD": first, "TPP": second}, con)

    address = con.execute(
        "SELECT mfr_site_address FROM product WHERE product_id=?", (pid,)).fetchone()[0]
    assert address == ("1 North Waukegan Road, North Chicago, IL 60064, "
                       "Prepared by the packaging team")


# -- the whole run ------------------------------------------------------------


def run_documents(psa, docs, **kwargs):
    """`process_documents` on the already-built fixture catalogue, with no live read."""
    kwargs.setdefault("rebuild", False)
    return psa["workflow"].process_documents(docs, **kwargs)


def test_a_run_that_must_not_touch_smartsheet_says_where_its_catalogue_came_from(psa,
                                                                                 catalogue):
    """`rebuild=False` is the ARIA `report` stage's promise that the report describes exactly
    the catalogue the recommendation did. The log is the audit record of that promise."""
    result = run_documents(psa, [], program_override=SUBJECT)

    assert result["status"] == "ok"
    assert ">> Smartsheet source: the run's stored snapshot (already loaded; no live read)" \
        in result["log"]


def test_a_selected_presentation_that_no_longer_exists_falls_back_to_the_auto_pick(psa,
                                                                                   catalogue):
    """A stale `source_row` held by a screen across a rebuild must not fail the run, and the
    row that was actually used is reported back so the caller can see the substitution."""
    result = run_documents(psa, [], program_override=SUBJECT, presentation=999)

    assert result["presentation_used"] == SUBJECT_ROW


def test_a_run_reports_the_presentation_it_used_and_the_ones_it_could_have(psa, rebuild):
    """The picker on the screen is built from this, so a run that returns no alternatives
    leaves the analyst unable to correct an auto-selection."""
    rebuild({"program": SUBJECT, "vial": "2R"}, {"program": SUBJECT, "vial": "10R"})

    result = run_documents(psa, [], program_override=SUBJECT)

    assert [p["source_row"] for p in result["presentations"]] == [SUBJECT_ROW,
                                                                 SUBJECT_ROW + 1]
    assert result["presentation_used"] == SUBJECT_ROW


def test_an_explicitly_selected_presentation_is_used_without_scoring_anything(psa, rebuild,
                                                                             tmp_path):
    """An analyst correcting an auto-selection is the authority. A run that re-scored the
    documents and overrode them would make the override button do nothing."""
    rebuild({"program": SUBJECT, "vial": "2R"}, {"program": SUBJECT, "vial": "10R"})
    path = make_docx(tmp_path / "Product 0 review.docx", "Container closure: 2R vial")

    result = run_documents(psa, [path], presentation=SUBJECT_ROW + 1)

    assert result["presentation_used"] == SUBJECT_ROW + 1, "the selection, not the 2R match"
    assert f"using the selected presentation ({SUBJECT_ROW + 1})" in result["log"]


def test_a_presentation_selected_by_its_list_number_resolves_to_that_row(psa, rebuild):
    """The list number is what an assessor actually knows; our Smartsheet row number is
    internal. Both have to reach the same presentation or the picker lies."""
    rebuild({"program": SUBJECT, "vial": "2R"}, {"program": SUBJECT, "vial": "10R"})

    result = run_documents(psa, [], program_override=SUBJECT, presentation="NDC-001")

    assert result["presentation_used"] == SUBJECT_ROW + 1


def test_an_identified_run_reports_the_program_name_it_resolved_to(psa, catalogue, tmp_path):
    """The name is what a human recognises, and it is shown beside the decision so an
    identification can be sanity-checked before the form is signed."""
    path = make_docx(tmp_path / "Product 1 assessment.docx", READABLE)

    result = run_documents(psa, [path])

    assert result["decision"] == "smartsheet"
    assert result["program"] == "ABBV-400"
    assert result["identified_name"] == "Product 1"
    assert result["reason"] == "name-in-filename"


def test_an_override_typed_in_lower_case_kills_the_run(psa, catalogue):
    """CHARACTERISATION OF WHAT LOOKS LIKE A THIRD BUG (workflow.py:344).

    The override's catalogue test is `_norm(program) in cat_codes`, which is case-INSENSITIVE,
    so a lower-cased code is judged a `smartsheet` decision. Every query downstream then
    compares `program_no=?` literally — `list_presentations` returns nothing,
    `_pick_presentation` answers `(None, 0)`, and `populate_template` raises
    `SystemExit("No product with program_no='agn-151586'")`.

    So the two halves disagree: the code is known enough to skip the new-product path and not
    known enough to be assessed. A code typed by hand into the override box reaches this — the
    normal way an override is supplied. Either the decision should be case-sensitive too (the
    run would then produce a new-product form, which is at least a document), or the override
    should be upper-cased before use.

    Pinned as the ACTUAL behaviour, including the `SystemExit`: it is a BaseException, so a
    caller guarding with `except Exception` would let it unwind the worker.
    """
    with pytest.raises(SystemExit, match="No product with program_no='agn-151586'"):
        run_documents(psa, [], program_override=SUBJECT.lower())


def test_the_report_engine_turns_that_into_a_message_the_screen_can_render(psa, catalogue):
    """The other side of the same characterisation, and the reason `generate_report` catches
    `SystemExit` explicitly rather than relying on `except Exception`. Without it the lower-case
    override above would take the worker down instead of showing the analyst a reason."""
    result = generated_report(program=SUBJECT.lower())

    assert result["status"] == "message"
    assert "No product with program_no='agn-151586'" in result["message"]


def test_an_override_for_a_code_the_catalogue_holds_is_a_smartsheet_decision(psa, catalogue):
    """The override skips identification entirely, but it still has to decide whether the code
    is a catalogue product or a new one — the two paths fill Parts A and B from different
    sources, so getting this wrong produces a form of [PENDING]s for a known product."""
    result = run_documents(psa, [], program_override=SUBJECT)

    assert result["decision"] == "smartsheet"
    assert result["subject_code"] == SUBJECT
    assert result["identified_name"] == "Product 0"


def test_a_failing_risk_engine_does_not_fail_the_assessment(psa, catalogue, monkeypatch):
    """The risk figure is provisional analysis; the form is the deliverable. A risk engine
    crash that lost the generated document would be a far worse outcome than a missing
    similarity number, and the reason is carried back so it can be shown."""
    monkeypatch.setattr(
        psa["workflow"].risk, "assess_program",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = run_documents(psa, [], program_override=SUBJECT)

    assert result["status"] == "ok"
    assert result["risk"]["overall_risk"] == "n/a"
    assert result["risk"]["error"] == "boom"
    assert result["risk"]["rationale"] == "risk assessment unavailable: boom"


def test_a_failed_verification_is_reported_without_failing_the_run(psa, catalogue,
                                                                  monkeypatch):
    """The document exists and is worth looking at even when a check failed — but the caller
    has to be told, or an incomplete form gets signed."""
    monkeypatch.setattr(psa["workflow"].verify, "main", lambda program: 1)

    result = run_documents(psa, [], program_override=SUBJECT)

    assert result["status"] == "ok"
    assert result["verify_ok"] is False


def test_a_run_produces_the_report_on_disk_under_the_programs_name(psa, catalogue):
    """`output_path` is what the platform attaches to the run, so it has to be the file that
    was actually written rather than the name it was going to have."""
    result = run_documents(psa, [], program_override=SUBJECT)

    assert result["output_exists"] is True
    assert os.path.basename(result["output_path"]) == f"{SUBJECT}_PSA_generated.docx"
    assert docx_text(result["output_path"]).count(SUBJECT) >= 1


def test_a_scanned_upload_asks_for_a_text_based_document_instead_of_guessing(psa, catalogue,
                                                                            tmp_path):
    """The commonest real failure: someone uploads a scan. The reply has to name the cause,
    because "could not identify the product" sends the analyst looking for the wrong problem,
    and the product list has to come back so they can pick manually."""
    path = make_image_only_pdf(tmp_path / "Product 1 assessment.pdf")

    result = run_documents(psa, [path])

    assert result["status"] == "needs_override"
    assert "scanned / image-only" in result["message"]
    assert SUBJECT in [code for code, _name in result["candidates"]]


def test_two_uploads_of_the_same_role_collapse_to_the_last_one(psa, catalogue, tmp_path):
    """CHARACTERISATION OF WHAT LOOKS LIKE A BUG (workflow.py:368-370).

    `doc_roles` is a dict keyed by ROLE, and `classify_role` answers PDD for anything that is
    not a TPP — so two PDD-shaped uploads (a Product Definition and a slide deck, the normal
    submission) overwrite each other and only the last file is ever read. The first upload is
    silently discarded: it is not extracted, it is not recorded in `doc_source`, and nothing
    in the result says a file was dropped.

    Asserted as the ACTUAL behaviour. The fix is to key by role with a list, or to make the
    roles unique, which would make the second assertion below wrong.
    """
    first = make_docx(tmp_path / "one.docx", "Dosage Form: Liquid")
    second = make_docx(tmp_path / "two.docx", "Dosage Form: Lyophilised powder")

    result = run_documents(psa, [first, second], program_override=SUBJECT)

    uploaded = result["log"].split("| uploaded: ")[1].splitlines()[0]
    assert uploaded.count("'PDD'") == 1
    assert "two.docx" in uploaded and "one.docx" not in uploaded


def test_a_rebuild_without_live_credentials_warns_that_the_export_is_stale(psa, catalogue,
                                                                          monkeypatch):
    """The fallback is legitimate but the data may be months old, and an assessment built on
    a stale export with no warning is indistinguishable from one built on today's sheet."""
    called = []
    monkeypatch.setattr(psa["workflow"].build_db, "main", lambda: None)
    monkeypatch.setattr(psa["workflow"].ingest_smartsheet, "main",
                        lambda *a, **kw: called.append("xlsx"))

    result = run_documents(psa, [], program_override=SUBJECT, rebuild=True)

    assert called == ["xlsx"]
    assert ">> WARNING: live Smartsheet not configured" in result["log"]


def test_a_rebuild_with_live_credentials_names_the_api_as_the_source(psa, catalogue, live,
                                                                    monkeypatch):
    """The other half of the same branch. Which source a run used is the first question asked
    about a wrong value on a form."""
    live(sheet())
    called = []
    monkeypatch.setattr(psa["workflow"].build_db, "main", lambda: None)
    monkeypatch.setattr(psa["workflow"].ingest_smartsheet_api, "main",
                        lambda *a, **kw: called.append("api"))

    result = run_documents(psa, [], program_override=SUBJECT, rebuild=True)

    assert called == ["api"]
    assert ">> Smartsheet source: LIVE API" in result["log"]


# -- the command line --------------------------------------------------------


@pytest.fixture
def cli(psa, monkeypatch):
    """Drive `main()` with a recording `process_documents`.

    The exit code and the argument parsing are what is under test here; a second real run
    would only re-cover `process_documents`, and a failure could then be either module's.
    """
    calls: list[dict] = []

    def recorder(docs, program_override=None, presentation=None, **kwargs):
        calls.append({"docs": docs, "program_override": program_override,
                      "presentation": presentation})
        return dict(result)

    result: dict = {"status": "ok", "program": SUBJECT, "identified_name": "Product 0",
                    "output_path": "out.docx", "output_exists": True, "verify_ok": True,
                    "risk": {"overall_risk": "low", "rationale": "no comparators"},
                    "log": ""}

    def run(argv, **overrides):
        result.update(overrides)
        monkeypatch.setattr(psa["workflow"].sys, "argv", ["workflow", *argv])
        monkeypatch.setattr(psa["workflow"], "process_documents", recorder)
        return psa["workflow"].main(), calls

    return run


def test_the_cli_with_no_arguments_prints_its_usage_and_fails(psa, cli, capsys):
    """A non-zero exit is what a shell script wrapping this checks; printing usage and
    returning 0 would make an empty invocation look like a successful assessment."""
    code, calls = cli([])

    assert code == 1
    assert calls == [], "nothing was run"
    assert "usage: python -m da_silos.psa.workflow" in capsys.readouterr().out


def test_the_cli_lifts_the_flags_out_of_the_document_list(psa, cli):
    """The flags are positional-adjacent, so a parser that left them in `args` would try to
    read `--product` as a file and identify the product from a filename of "--product"."""
    code, calls = cli(["a.pdf", "--product", "ABBV-400", "b.pdf",
                       "--presentation", "NDC-002"])

    assert code == 0
    assert calls[0] == {"docs": ["a.pdf", "b.pdf"], "program_override": "ABBV-400",
                        "presentation": "NDC-002"}


def test_the_cli_succeeds_when_every_check_passed(psa, cli, capsys):
    code, _calls = cli(["a.pdf"])

    assert code == 0
    assert "ALL CHECKS PASSED" in capsys.readouterr().out


def test_the_cli_fails_when_a_check_failed(psa, cli, capsys):
    """The document was still produced, so the exit code is the only signal a caller has that
    it should not be circulated."""
    code, _calls = cli(["a.pdf"], verify_ok=False)

    assert code == 1
    assert "VERIFY: some checks failed" in capsys.readouterr().out


def test_the_cli_uses_its_own_exit_code_when_a_human_must_choose(psa, cli, capsys):
    """A distinct code, not 1: "pick a product" is an input problem the caller can retry, and
    conflating it with a failed verification hides that."""
    code, _calls = cli(["a.pdf"], status="needs_override", message="Pick the product.",
                       candidates=[("ABBV-400", "Etentamig")])

    assert code == 2
    output = capsys.readouterr().out
    assert "Pick the product." in output
    assert "Products: ABBV-400" in output


# -- engines: is there a database? -------------------------------------------


def test_a_missing_database_does_not_exist(psa, psa_config):
    """Every table query in `engines` guards on this, and answering True for a missing file
    turns an unbuilt checkout into "no such table: product" on the screen."""
    assert psa["engines"].db_exists() is False


def test_a_zero_byte_database_does_not_exist_either(psa, psa_config):
    """The file a plain `sqlite3.connect()` leaves behind. It is not a database, and treating
    it as one is exactly the crash `connect()`'s read-only mode exists to prevent."""
    db = psa["paths"].db_path()
    os.makedirs(os.path.dirname(db), exist_ok=True)
    open(db, "wb").close()

    assert psa["engines"].db_exists() is False


def test_a_rebuilt_database_exists(psa, catalogue):
    assert psa["engines"].db_exists() is True


def test_reading_an_unbuilt_database_raises_and_leaves_no_file_behind(psa, psa_config):
    """The whole reason `connect()` opens `mode=ro`: a plain connect would CREATE psa.db, and
    the 0-byte file left behind makes the next caller crash instead of skipping."""
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        with psa["engines"].connect():
            pass

    assert not os.path.exists(psa["paths"].db_path())


def test_the_connection_handed_out_cannot_write(psa, catalogue):
    """These connections are handed to query helpers on a database that is dropped and
    recreated per run; a stray write would corrupt a shared artifact nothing owns."""
    with psa["engines"].connect() as connection:
        with pytest.raises(sqlite3.OperationalError,
                           match="attempt to write a readonly database"):
            connection.execute("DELETE FROM product")


# -- engines: which Smartsheet source ----------------------------------------


def test_a_forced_xlsx_source_is_not_live_even_with_credentials(psa, psa_config):
    """The switch an offline test — or an operator working from an export — relies on. If a
    token alone made it live, `ingest_source` would be unable to prevent a network read."""
    assert psa["engines"].smartsheet_live() is False


def test_a_configured_api_source_is_live(psa, psa_config):
    psa["config"].set_config(dataclasses.replace(psa_config, ingest_source="api"))

    assert psa["engines"].smartsheet_live() is True


def test_a_refresh_without_a_token_reports_the_missing_credential_and_never_raises(
        psa, catalogue, psa_config):
    """`ingest_api.main()` raises `SystemExit` on a missing credential — a BaseException, so a
    plain `except Exception` would let it unwind the worker and take every other queued run
    with it. The message names the variable so the operator knows what to set."""
    psa["config"].set_config(
        dataclasses.replace(psa_config, ingest_source="api", smartsheet_token=None))

    ok, message = psa["engines"].refresh()

    assert ok is False
    assert message.startswith("Refresh failed: SMARTSHEET_ACCESS_TOKEN is not set")


def test_a_failed_refresh_leaves_the_database_rebuilt_but_empty(psa, catalogue, psa_config):
    """`build_db.main()` has already dropped every table by the time the credential check
    fails, so the previous catalogue is gone. Characterised because a caller that treats a
    failed refresh as a no-op would then read an empty catalogue as "no products"."""
    psa["config"].set_config(
        dataclasses.replace(psa_config, ingest_source="api", smartsheet_token=None))

    psa["engines"].refresh()

    with psa["engines"].connect() as connection:
        assert connection.execute("SELECT count(*) FROM product").fetchone()[0] == 0


def test_a_successful_refresh_reads_the_sheet_once_and_asks_for_its_attachments(psa, live):
    """One read per refresh, and `include=attachments` is what makes the product photo
    reachable — a refresh that omitted it would silently stop ingesting images."""
    urls = live(sheet())

    ok, message = psa["engines"].refresh()

    assert (ok, message) == (True, "Product list refreshed from the LIVE Smartsheet.")
    assert [u for u in urls if u.endswith("/sheets/SHEET-1?include=attachments")] == urls
    assert len(urls) == 1


# -- engines: the pickers ----------------------------------------------------


def test_the_program_picker_prefers_the_live_sheet_over_the_stored_catalogue(psa, catalogue,
                                                                            live):
    """The picker exists so an analyst can start on a program added to Smartsheet minutes
    ago; answering from psa.db would hide exactly the row they came to find."""
    from da_silos.psa.ingest_smartsheet import COLS

    live(sheet([row(COLS, 7, program="ABBV-777")]))

    assert psa["engines"].programs() == [
        {"program_no": "ABBV-777", "program_name": "Product 7",
         "label": "ABBV-777 (Product 7)"}
    ]


def test_an_all_clinical_live_sheet_falls_back_to_the_stored_catalogue(psa, catalogue, live):
    """Scope drops every live row, and an empty live answer must not be mistaken for "the
    portfolio is empty" — the picker would offer nothing at all."""
    from da_silos.psa.ingest_smartsheet import COLS

    live(sheet([row(COLS, 8, program="ABBV-888", batch_type="Clinical")]))

    assert SUBJECT in [p["program_no"] for p in psa["engines"].programs()]


def test_the_presentation_picker_prefers_the_live_sheet(psa, catalogue, live):
    """Same reason, and the row number is the key the rest of the run is driven by — so a
    stale one starts the assessment on the wrong presentation."""
    from da_silos.psa.ingest_smartsheet import COLS

    live(sheet([row(COLS, 7, program=SUBJECT)]))

    got = psa["engines"].presentations(SUBJECT)

    assert [p["source_row"] for p in got] == [SUBJECT_ROW + 7]
    assert got[0]["label"] == "2R  |  Commercial  |  100 mg"


def test_a_snapshot_only_presentation_read_never_opens_a_socket(psa, catalogue, psa_config):
    """`live=False` is a promise a stage makes to the platform. It was broken once —
    `recommendation()` called this unconditionally just to build a display string — so the
    credentials are present here and the network is booby-trapped."""
    psa["config"].set_config(dataclasses.replace(psa_config, ingest_source="api"))

    got = psa["engines"].presentations(SUBJECT, live=False)

    assert [p["source_row"] for p in got] == [SUBJECT_ROW]


@pytest.mark.parametrize("vial,batch,strength,expected", [
    ("2R", "Commercial", "100 mg", "2R  |  Commercial  |  100 mg"),
    ("2R", "", None, "2R"),
    ("", "Commercial", None, "?  |  Commercial"),
    ("", "", None, ""),
])
def test_a_presentation_label_names_what_it_can(psa, vial, batch, strength, expected):
    """The empty answer is the load-bearing one: it lets the caller's `or f"row {sr}"` fall
    back to naming the sheet row. A "?" placeholder there would be truthy and defeat it, so a
    nearly-blank row — which a Smartsheet form submission can leave behind — rendered in the
    picker as a bare "?" that named nothing the analyst could go and look at."""
    assert psa["engines"].pres_label(vial, batch, strength) == expected


def test_a_program_is_labelled_with_its_name(psa, catalogue):
    """The heading of the recommendation document and the export, so a bare code there tells
    the reader nothing about what they are signing."""
    assert psa["engines"].program_label(SUBJECT) == f"{SUBJECT} (Product 0)"


def test_an_unknown_program_is_labelled_with_its_bare_code(psa, catalogue):
    """A code typed by hand into the override box reaches this, and a raised exception would
    lose the whole screen over a missing display string."""
    assert psa["engines"].program_label("NOPE-000") == "NOPE-000"


# -- engines: the tables -----------------------------------------------------


def test_the_products_using_one_cap_colour_are_listed(psa, catalogue):
    """This table is how a colour is checked for prior use before it is assigned, so a missing
    row is a colour handed out twice."""
    got = psa["engines"].products_by_color("Datwyler", "Blue 6043")

    assert len(got) == 3
    assert SUBJECT in " ".join(r["product"] for r in got)


def test_a_clinical_product_is_not_counted_against_its_site(psa, catalogue):
    """The site chart drives where a mix-up is plausible, and counting out-of-scope clinical
    presentations would overstate every site."""
    counts = {r["site_code"]: r["n_products"] for r in psa["engines"].site_product_counts()}

    assert counts == {"AP16": 4, "LU": 1}


@pytest.mark.parametrize("query", ["colours", "sites"])
def test_the_tables_are_empty_rather_than_broken_without_a_database(psa, psa_config, query):
    """Both are rendered on a first visit to an unbuilt checkout. A 500 there is a blank
    screen with no explanation instead of an empty table and a refresh button."""
    if query == "colours":
        assert psa["engines"].products_by_color("Datwyler", "Blue 6043") == []
    else:
        assert psa["engines"].site_product_counts() == []


# -- engines: the recommendation and its export ------------------------------


def test_a_recommendation_is_produced_for_a_real_presentation(psa, catalogue):
    """The main read path of the cap layer, through `engines` rather than `cap_recommend`, so
    the row-to-product mapping is exercised too."""
    result, program_label, presentation_label = psa["engines"].recommendation(
        SUBJECT, SUBJECT_ROW)

    assert result and not result.get("error")
    assert program_label == f"{SUBJECT} (Product 0)"
    assert presentation_label == "2R  |  Commercial  |  100 mg"


@pytest.mark.parametrize("program,source_row", [(SUBJECT, 999), ("NOPE-000", SUBJECT_ROW)])
def test_an_unresolvable_presentation_is_absent_rather_than_an_error(psa, catalogue,
                                                                    program, source_row):
    """`None` and an `error` dict mean different things to the caller: the first is "nothing
    to show, refresh", the second is "something went wrong". A stale row after a rebuild is
    the first, and reporting it as an error sends the analyst to the logs for nothing."""
    result, _program_label, _label = psa["engines"].recommendation(program, source_row)

    assert result is None


def test_a_recommendation_against_an_unbuilt_database_explains_itself(psa, psa_config):
    """A 500 is something the screen cannot render, so the failure comes back as an `error`
    the UI can show — with the action that fixes it."""
    result, _program_label, _label = psa["engines"].recommendation(SUBJECT, SUBJECT_ROW)

    assert "has not been built yet" in result["error"]
    assert _label == f"row {SUBJECT_ROW}", "the fallback label, since there is nothing to read"


def test_a_recommendation_against_a_schema_only_database_explains_itself_too(psa, empty_db):
    """A different branch from "no database": the file exists so `db_exists()` passes, and the
    query then finds no rows. Both have to reach the screen as an `error` rather than a 500."""
    result, _program_label, _label = psa["engines"].recommendation(SUBJECT, SUBJECT_ROW)

    assert result is None or result.get("error"), "nothing to recommend, and not a crash"


def test_a_snapshot_only_recommendation_never_opens_a_socket(psa, catalogue, psa_config):
    """`refresh_first=False` is the ARIA `recommend` stage's promise. It was broken once, by a
    `presentations()` call made purely to build a display string — so the credentials are
    present here and `urlopen` is booby-trapped by the `offline` fixture's replacement."""
    psa["config"].set_config(dataclasses.replace(psa_config, ingest_source="api"))

    result, program_label, label = psa["engines"].recommendation(
        SUBJECT, SUBJECT_ROW, refresh_first=False)

    assert result and not result.get("error")
    assert (program_label, label) == (f"{SUBJECT} (Product 0)", "2R  |  Commercial  |  100 mg")


def test_a_recommendation_source_row_matches_across_types(psa, catalogue):
    """A JSON client sends the row as `12` or `"12"` while the internal map is keyed the other
    way, so both have to resolve — a type mismatch here looks exactly like a stale row."""
    from_int, _p, _l = psa["engines"].recommendation(SUBJECT, SUBJECT_ROW)
    from_str, _p, _l = psa["engines"].recommendation(SUBJECT, str(SUBJECT_ROW))

    assert from_int is not None
    assert from_str == from_int


def test_the_export_is_named_after_the_program_and_the_presentation(psa, catalogue):
    """Several exports land in one folder and a reviewer picks between them by filename, so
    the presentation has to be in it — two presentations of one program are different
    recommendations."""
    path, message = psa["engines"].export_recommendation(SUBJECT, SUBJECT_ROW, None)

    assert os.path.basename(path) == f"cap_recommendation_{SUBJECT}_{SUBJECT_ROW}.docx"
    assert message == "Recommendation exported."
    assert os.path.getsize(path) > 0


def test_exporting_a_presentation_that_is_not_in_the_database_says_what_to_do(psa,
                                                                             catalogue):
    """`cap_export.build` would choke on the `None` this guards, and the reply names the three
    steps in order rather than reporting a crash."""
    path, message = psa["engines"].export_recommendation(SUBJECT, 999, None)

    assert path is None
    assert "not in the analysis database yet" in message


# -- engines: the report -----------------------------------------------------


def test_a_report_generated_from_the_stored_catalogue_carries_the_products_fields(psa,
                                                                                 catalogue):
    """The automated path: no uploaded documents at all, so Parts A and B must come from the
    Smartsheet row of the chosen presentation."""
    result = generated_report()

    assert result["status"] == "ok"
    text = docx_text(result["output_path"])
    assert "NDC-000" in text, "the subject's list number"
    assert "2R" in text


def test_a_photo_supplied_as_bytes_is_embedded_once(psa, catalogue):
    """Under ARIA there is no ingested image on disk — the `report` stage reads the photo back
    from the run's media and passes the bytes. A second copy would mean the placeholder cell
    was filled as well, and none would mean the run's captured photo was lost."""
    result = generated_report(photo=png_bytes())

    assert result["status"] == "ok"
    assert len(Document(result["output_path"]).inline_shapes) == 1


def test_a_report_for_a_program_the_catalogue_does_not_hold_assesses_it_as_new(psa,
                                                                              catalogue):
    """A code typed into the override box that Smartsheet has not caught up with is not an
    error: the run inserts a transient row and produces a form of [PENDING]s for the analyst
    to complete. Characterised because "status: ok" on a form with no data is only correct
    while `decision` says why, and a caller reading `status` alone would circulate it."""
    result = generated_report(program="NOPE-000")

    assert result["status"] == "ok"
    assert result["decision"] == "new_product"
    assert result["output_exists"] is True
    assert "NOPE-000" in docx_text(result["output_path"])
    assert result["risk"]["n_comparators"] == 0, "a program of one has nothing to resemble"
