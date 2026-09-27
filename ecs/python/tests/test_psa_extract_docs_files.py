"""The document-facing half of PSA ingestion, driven against REAL files on disk.

`tests/test_psa_extract_docs.py` covers the text-level extractors — the functions that take a
string and answer with a field. Everything below that seam was untested: opening a PDF, walking
its pages, pulling the chosen embedded image out of it, writing the results into
`doc_source` / `doc_attribute` / `field_provenance`, and the analyst assessment record that
later overrides the auto-draft. Those are the paths a GxP reviewer's provenance trail is made
of, and they only run against files.

So this file builds the files. **Nothing is stubbed and nothing is mocked** — the PDFs are
synthesised with PyMuPDF (the same library `extract_docs` reads them back with), the DOCX with
python-docx, the assessment record with `json.dump`, and the catalogue is the shared fixture
built offline from a canned Smartsheet snapshot. There is no network in any direction: the
extractors are anchored-regex readers of local bytes, and `REGISTRY` is a hardcoded map rather
than a lookup. `pypdf` and `reportlab` are not installed and are not needed — PyMuPDF both
writes and reads.

Two things are deliberately asserted as they ACTUALLY behave rather than as the source's
comments describe them; both are marked inline and reported as suspected bugs:

  * the CMC-director capture glues the preceding word onto the name, because `_find` passes
    `re.I` and that defeats the `[A-Z][a-z]+` word shape the pattern relies on;
  * the generic manufacturing-address fallback runs on to the end of the text block, so a
    strength sitting on the next line lands inside the address.

The last section drives each module's `if __name__ == "__main__"` argument dispatch through
`runpy`, because that block is the whole of the two modules' command-line contract and calling
`main()` directly steps over it. It is deleted from `sys.modules` first (via `monkeypatch`, so
it comes back) — `runpy` warns and may behave unpredictably when re-executing a module that is
already imported. Re-execution builds a SECOND module object, so those tests use the real
`REGISTRY` entries rather than a patched one: a `monkeypatch.setitem` on the imported module
would be invisible to the copy `runpy` runs.

Every file this module writes goes under `tmp_path`: `psa_config` is re-injected with
`root=tmp_path` as well, because `paths.root()` is where `extract_docs` and
`ingest_assessment` look for their INPUT documents, and the shared fixture leaves it pointing
at the source tree.
"""

from __future__ import annotations

import dataclasses
import json
import os
import runpy
import sys

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.psa_fixtures import (SUBJECT, catalogue, con, psa_config,  # noqa: F401
                               subject_product_id)

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import config, extract_docs, ingest_assessment, paths

    return {"docs": extract_docs, "assessment": ingest_assessment,
            "config": config, "paths": paths}


@pytest.fixture
def rooted(psa, psa_config, tmp_path):
    """The shared PSA config, with the INPUT root moved into `tmp_path` too.

    `psa_config` relocates everything a run WRITES but leaves `root` at the repo, which is
    correct for the tests that read the shipped inputs and wrong here: every document in this
    file is synthesised, so it has to be synthesised somewhere the silo will look and nowhere
    near the source tree. `psa_config` restores the previous injection afterwards, so
    re-injecting on top of it needs no teardown of its own.
    """
    psa["config"].set_config(dataclasses.replace(psa_config, root=str(tmp_path)))
    return tmp_path


# ── building the documents the extractors read ────────────────────────────────


def write_pdf(root, rel, pages):
    """Write a text PDF at `root/rel`; return `rel`, which is what the loaders take.

    One `insert_text` per line, so `page.get_text()` reads the lines back in order and a
    multi-line address arrives wrapped exactly as it does out of a real PDD.
    """
    import fitz

    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    document = fitz.open()
    for lines in pages:
        page = document.new_page()
        for i, line in enumerate(lines):
            page.insert_text((60, 70 + 15 * i), line)
    document.save(full)
    document.close()
    return rel


def write_image_pdf(root, rel, images, pages=1, on_page=0):
    """Write a PDF with `images` — (width, height, mode) — embedded on page `on_page`."""
    import fitz
    from PIL import Image

    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    document = fitz.open()
    for _ in range(max(pages, on_page + 1)):
        document.new_page()
    target = document[on_page]
    for n, (width, height, mode) in enumerate(images):
        suffix = "jpg" if mode == "CMYK" else "png"
        source = os.path.join(str(root), f"embed_{n}_{width}x{height}.{suffix}")
        bands = len(Image.new(mode, (1, 1)).getbands())
        Image.new(mode, (width, height), tuple([60 + 30 * n] * bands)).save(source)
        top = 40 + 200 * n
        target.insert_image(fitz.Rect(30, top, 30 + width / 4, top + height / 4),
                            filename=source)
    document.save(full)
    document.close()
    return rel


def write_docx(root, rel, text):
    from docx import Document

    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    document = Document()
    document.add_paragraph(text)
    document.save(full)
    return rel


TPP_REL = "Input_Data_Sources/TPPs/CMC TPP under test.pdf"
PDD_REL = "Input_Data_Sources/PDDs/Product Definition under test.pdf"


def tpp_pages(strength="1,500 U/vial"):
    """A TPP shaped like the reference one: cover, product page, signature page."""
    return [
        ["CMC Target Product Profile", "Programme AGN-151586"],
        ["Nonproprietary name: Trenibotulinumtoxin E", f"Each vial contains {strength}"],
        ["Signatory: Sinead Duffy", "Director, CMC Product Development"],
    ]


def pdd_pages(strength="1400 U/vial"):
    """A PDD with the wrapped Westport address block and the US site line."""
    return [
        ["Product Definition Document"],
        ["Drug Product Manufacturing Site", "AbbVie", "Castlebar Road",
         "Westport, County Mayo", "Ireland"],
        ["Sponsor site: North Chicago, Illinois", f"Each vial contains {strength}"],
    ]


# ── reading the pages of a real pdf ───────────────────────────────────────────


def test_every_page_of_a_pdf_is_read_in_order(psa, rooted):
    """`_pages` is the only PyMuPDF boundary in the module, and every page number that ends up
    in the provenance trail is this list's index — pages out of order would cite the wrong page
    of a signed document."""
    rel = write_pdf(rooted, PDD_REL, [["first page"], ["second page"], ["third page"]])

    pages = psa["docs"]._pages(os.path.join(str(rooted), rel))

    assert len(pages) == 3
    assert [p.strip() for p in pages] == ["first page", "second page", "third page"]


def test_the_document_is_closed_after_being_read(psa, rooted):
    """The `finally: doc.close()` matters on Windows, where an open handle stops the run from
    replacing the file — which is what an upload of a corrected PDD does."""
    rel = write_pdf(rooted, PDD_REL, [["only page"]])
    full = os.path.join(str(rooted), rel)

    psa["docs"]._pages(full)

    os.replace(full, full)          # would raise PermissionError while a handle was open
    assert os.path.exists(full)


# ── the tpp extractor, against a real tpp ─────────────────────────────────────


def test_the_inn_name_is_extracted_with_the_page_it_came_from(psa, rooted):
    """The INN is half of the product name printed in Part A, and the page is what lets a
    reviewer turn to it. All whitespace is stripped because the PDF lays the name across a
    line break and an INN is one word."""
    pages = psa["docs"]._pages(os.path.join(str(rooted), write_pdf(rooted, TPP_REL, tpp_pages())))

    field = psa["docs"].extract_tpp(pages)["inn_name"]

    assert field["value"] == "TrenibotulinumtoxinE"
    assert field["page"] == 2
    assert field["label"] == "INN / Nonproprietary name"
    assert field["conf"] == 0.9


def test_the_per_vial_strength_is_extracted_from_the_tpp_with_its_page(psa, rooted):
    """The strength is the field the two documents disagree about, so its page is what an
    analyst resolving the conflict reads first."""
    pages = psa["docs"]._pages(os.path.join(str(rooted), write_pdf(rooted, TPP_REL, tpp_pages())))

    field = psa["docs"].extract_tpp(pages)["per_vial_strength"]

    assert field["value"] == "1500 U/vial"
    assert field["page"] == 2


def test_the_development_director_is_named_from_the_signature_block(psa, rooted):
    """Part C names the CMC Product Development Director. A wrong name there is a wrong
    accountable person on a governance form."""
    pages = psa["docs"]._pages(os.path.join(str(rooted), write_pdf(rooted, TPP_REL, tpp_pages())))

    field = psa["docs"].extract_tpp(pages)["pd_director_name"]

    assert field["value"] == "Sinead Duffy"
    assert field["page"] == 3


def test_the_director_is_found_by_title_when_the_name_is_not_the_reference_one(psa, rooted):
    """The first pattern is tuned to the reference document's signatory; the fallback is what
    makes the extractor work for any other product, and without it every other TPP would
    silently yield no director."""
    rel = write_pdf(rooted, TPP_REL, [
        ["Trenibotulinumtoxin E"],
        ["Jane A. Smith, Director, CMC Product Development"],
    ])

    found = psa["docs"].extract_tpp(psa["docs"]._pages(os.path.join(str(rooted), rel)))

    assert found["pd_director_name"]["value"] == "Jane A. Smith"


def test_the_director_capture_absorbs_the_word_in_front_of_the_name(psa, rooted):
    """CHARACTERISING WHAT IT ACTUALLY DOES — this looks like a bug. The source comment says
    the pattern "avoids grabbing 'Approved by' / newlines", but `_find` searches with `re.I`,
    which makes `[A-Z][a-z]+` match any word, and `\\s+` crosses the line break. So a name on
    its own line under a heading arrives with the heading's last word attached, and that string
    is what gets recorded as the accountable person."""
    rel = write_pdf(rooted, TPP_REL, [["Approved by", "Sinead Duffy"]])

    found = psa["docs"].extract_tpp(psa["docs"]._pages(os.path.join(str(rooted), rel)))

    assert found["pd_director_name"]["value"] == "Approved by Sinead Duffy"


def test_a_tpp_with_none_of_the_fields_yields_nothing(psa, rooted):
    """An unrecognised or image-only TPP must contribute no facts at all rather than partial
    ones — the form then renders [PENDING] and asks the assessor."""
    rel = write_pdf(rooted, TPP_REL, [["Commercial in confidence"], ["No fields here"]])

    assert psa["docs"].extract_tpp(psa["docs"]._pages(os.path.join(str(rooted), rel))) == {}


# ── the pdd extractor, against a real pdd ─────────────────────────────────────


def test_the_manufacturing_address_is_rejoined_from_its_wrapped_lines(psa, rooted):
    """The PDD lays the site address across four short lines; it is printed as one site name in
    Part B, so the line breaks have to become commas and the company line has to survive."""
    pages = psa["docs"]._pages(os.path.join(str(rooted), write_pdf(rooted, PDD_REL, pdd_pages())))

    field = psa["docs"].extract_pdd(pages)["mfr_site_address"]

    assert field["value"] == "AbbVie, Castlebar Road, Westport, County Mayo, Ireland"
    assert field["page"] == 2
    assert field["conf"] == 0.8


def test_a_single_line_address_is_used_when_there_is_no_castlebar_block(psa, rooted):
    """The fallback pattern. A PDD that names the site on one line still has to yield an
    address, or Part B falls back to the bare Smartsheet site code."""
    rel = write_pdf(rooted, PDD_REL, [["DP manufacturing site: Westport, County Mayo, Ireland"]])

    field = psa["docs"].extract_pdd(psa["docs"]._pages(os.path.join(str(rooted), rel)))

    assert field["mfr_site_address"]["value"].endswith("Westport, County Mayo, Ireland")


@pytest.mark.parametrize("written", ["Lake County, Illinois", "North Chicago, Illinois",
                                     "North Chicago, IL"])
def test_the_us_site_location_is_read_however_the_pdd_writes_it(psa, rooted, written):
    """Three spellings of the same place appear across real PDDs, and Part B prints one of
    them."""
    rel = write_pdf(rooted, PDD_REL, [[f"Sponsor: {written}"]])

    found = psa["docs"].extract_pdd(psa["docs"]._pages(os.path.join(str(rooted), rel)))

    assert found["site_location"]["value"] == written
    assert found["site_location"]["conf"] == 0.7


def test_the_pdd_strength_is_recorded_under_its_own_key(psa, rooted):
    """It has to be a DIFFERENT key from the TPP's, or the loader could not see the two
    documents disagreeing and would silently auto-fill one of them."""
    pages = psa["docs"]._pages(os.path.join(str(rooted), write_pdf(rooted, PDD_REL, pdd_pages())))

    found = psa["docs"].extract_pdd(pages)

    assert found["pdd_strength"]["value"] == "1400 U/vial"
    assert found["pdd_strength"]["label"] == "Per-vial strength (PDD)"
    assert "per_vial_strength" not in found


def test_a_pdd_with_none_of_the_fields_yields_nothing(psa, rooted):
    rel = write_pdf(rooted, PDD_REL, [["Nothing of interest on this page"]])

    assert psa["docs"].extract_pdd(psa["docs"]._pages(os.path.join(str(rooted), rel))) == {}


# ── the dump helpers, which are how the patterns were tuned ───────────────────


def test_dumping_a_program_prints_every_page_under_its_number(psa, rooted, monkeypatch, capsys):
    """`--dump` is the documented first step for tuning a regex against a new document, so the
    page banners are the map between a pattern and the page number it will report."""
    rel = write_pdf(rooted, PDD_REL, [["text of page one"], ["text of page two"]])
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-DUMP", {"PDD": rel})

    psa["docs"].dump("TEST-DUMP")
    out = capsys.readouterr().out

    assert f"PDD: {rel}" in out
    assert out.index("----- page 1 -----") < out.index("----- page 2 -----")
    assert "text of page one" in out and "text of page two" in out


@pytest.mark.parametrize("entry", [None, {"photo": {"page": 7, "index": 1}}],
                         ids=["unregistered", "photo-config-only"])
def test_dumping_a_program_with_no_document_files_says_so(psa, rooted, monkeypatch, capsys,
                                                          entry):
    """The 'photo' key is configuration, not a document. Treating it as one would try to read
    page text out of a dict."""
    if entry is not None:
        monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-NODOCS", entry)

    psa["docs"].dump("TEST-NODOCS")

    assert "No PDD/TPP files registered for TEST-NODOCS" in capsys.readouterr().out


def test_dumping_a_registered_document_that_is_absent_reports_it_and_continues(
        psa, rooted, monkeypatch, capsys):
    """A registry entry is a promise about a file that may not have been shipped; the dump has
    to name the gap and go on to the other role rather than raise."""
    present = write_pdf(rooted, TPP_REL, [["the tpp is here"]])
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-GAP",
                        {"PDD": "Input_Data_Sources/PDDs/absent.pdf", "TPP": present})

    psa["docs"].dump("TEST-GAP")
    out = capsys.readouterr().out

    assert "(file not found)" in out
    assert "the tpp is here" in out


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_any_pdf_can_be_dumped_by_path(psa, rooted, capsys, absolute):
    """`--dump-pdf` exists to transcribe the signed reference output, which is not a registry
    entry — so it takes a path, and a repo-relative one has to resolve under the input root."""
    rel = write_pdf(rooted, "Output_Files/signed example.pdf", [["verbatim assessment text"]])
    target = os.path.join(str(rooted), rel) if absolute else rel

    psa["docs"].dump_pdf(target)

    assert "verbatim assessment text" in capsys.readouterr().out


def test_dumping_a_pdf_that_is_not_there_names_the_path_it_looked_for(psa, rooted, capsys):
    """The whole point of the message is to show WHERE it looked, because the relative form
    resolves against the input root rather than the working directory."""
    psa["docs"].dump_pdf("Output_Files/never written.pdf")
    out = capsys.readouterr().out

    assert "(file not found:" in out
    assert str(rooted) in out


# ── choosing the product photo out of the pdd ─────────────────────────────────


def test_the_registry_can_pin_the_exact_embedded_image(psa, rooted):
    """A PDD page holds several images — logos, diagrams, the vial. The registry pins the vial
    by page and index, and the wrong one would be printed as the product photo on a signed
    form."""
    write_image_pdf(rooted, psa["docs"].REGISTRY[SUBJECT]["PDD"],
                    [(300, 240, "RGB"), (148, 384, "RGB")], pages=7, on_page=6)

    out = psa["docs"].extract_pdd_photo(SUBJECT)

    from PIL import Image

    assert Image.open(out).size == (148, 384)      # the pinned one, not the larger one
    assert out == os.path.join(psa["paths"].image_dir(), f"{SUBJECT}_photo.png")


def test_the_largest_image_is_used_when_none_is_pinned(psa, rooted, monkeypatch):
    """The documented default for a product whose PDD has not been inspected yet: the biggest
    image is the photo, and a logo is small."""
    rel = write_image_pdf(rooted, PDD_REL, [(40, 60, "RGB"), (300, 240, "RGB")],
                          pages=2, on_page=1)
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-PHOTO", {"PDD": rel})

    out = psa["docs"].extract_pdd_photo("TEST-PHOTO")

    from PIL import Image

    assert Image.open(out).size == (300, 240)


def test_a_cmyk_image_is_converted_to_rgb(psa, rooted, monkeypatch):
    """Print-ready PDDs carry CMYK images, and python-docx cannot embed one — the form would
    fail to build at the photo cell rather than at extraction time."""
    rel = write_image_pdf(rooted, PDD_REL, [(120, 90, "CMYK")])
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-CMYK", {"PDD": rel})

    out = psa["docs"].extract_pdd_photo("TEST-CMYK")

    from PIL import Image

    assert Image.open(out).mode == "RGB"


def test_an_uploaded_pdd_overrides_the_registered_path(psa, rooted, monkeypatch):
    """An assessor's upload is the document for this run; reading the registry's shipped copy
    instead would put a different product's vial on the form."""
    registered = write_image_pdf(rooted, PDD_REL, [(40, 60, "RGB")])
    uploaded = write_image_pdf(rooted, "Input_Data_Sources/uploads/mine.pdf",
                               [(220, 160, "RGB")])
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-UPLOAD", {"PDD": registered})

    out = psa["docs"].extract_pdd_photo("TEST-UPLOAD", pdd_rel=uploaded)

    from PIL import Image

    assert Image.open(out).size == (220, 160)


def test_a_program_with_no_pdd_has_no_photo(psa, rooted):
    """Most products have no PDD at all, and the caller renders the [PENDING] photo tag."""
    assert psa["docs"].extract_pdd_photo("ABBV-400") is None


def test_a_pdd_with_no_embedded_images_has_no_photo(psa, rooted):
    """A text-only PDD is normal, and inventing a photo is not an option."""
    rel = write_pdf(rooted, PDD_REL, [["no images on this page"]])

    assert psa["docs"].extract_pdd_photo("TEST-NOIMG", pdd_rel=rel) is None


def test_listing_the_embedded_images_reports_page_index_and_size(psa, rooted, monkeypatch,
                                                                 capsys):
    """`--images` is how the pinned page/index in the registry was chosen, so it has to print
    the two numbers the registry takes, next to the dimensions that identify the vial."""
    rel = write_image_pdf(rooted, PDD_REL, [(40, 60, "RGB"), (148, 384, "RGB")],
                          pages=2, on_page=1)
    monkeypatch.setitem(psa["docs"].REGISTRY, "TEST-IMAGES", {"PDD": rel})

    psa["docs"].list_images("TEST-IMAGES")
    out = capsys.readouterr().out

    assert "page 2  index 0" in out
    assert "40x60px" in out and "148x384px" in out
    assert 'REGISTRY["TEST-IMAGES"]["photo"]' in out


def test_listing_images_for_a_program_with_no_pdd_says_so(psa, rooted, capsys):
    psa["docs"].list_images("ABBV-400")

    assert "No PDD registered for ABBV-400" in capsys.readouterr().out


# ── loading what was extracted into the catalogue ─────────────────────────────


def docs_for(rooted, tpp_strength="1,500 U/vial", pdd_strength="1400 U/vial"):
    """A {role: relpath} pair of real PDFs, as `workflow` hands them to the loader."""
    return {"PDD": write_pdf(rooted, PDD_REL, pdd_pages(pdd_strength)),
            "TPP": write_pdf(rooted, TPP_REL, tpp_pages(tpp_strength))}


def attributes(con):
    return {r["attribute_key"]: r for r in con.execute(
        "SELECT a.attribute_key, a.attribute_label, a.value_text, a.source_locator, "
        "a.confidence, a.status, s.doc_role FROM doc_attribute a "
        "JOIN doc_source s ON s.doc_id = a.doc_id")}


def provenance(con, method="local-parse"):
    return {r["field_name"]: r for r in con.execute(
        "SELECT field_name, source_doc, source_locator, match_type, confidence "
        "FROM field_provenance WHERE extraction_method=?", (method,))}


def test_each_document_read_is_recorded_as_a_doc_source(psa, rooted, con):
    """The document row is the anchor of the provenance trail: without it a reviewer cannot
    tell which file a field came from, only which role."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    rows = {r["doc_role"]: r for r in con.execute(
        "SELECT doc_role, source_type, source_file, product_id FROM doc_source")}

    assert set(rows) == {"PDD", "TPP"}
    assert rows["PDD"]["source_type"] == "pdf"
    assert rows["PDD"]["source_file"] == PDD_REL
    assert rows["TPP"]["product_id"] == subject_product_id(con)


def test_every_extracted_field_is_recorded_with_its_page_and_confidence(psa, rooted, con):
    """Lossless capture is the stated design: a field is stored as read, against the page it was
    read from, whether or not it is promoted. That row is the audit evidence."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    found = attributes(con)

    assert set(found) == {"mfr_site_address", "site_location", "pdd_strength",
                          "inn_name", "per_vial_strength", "pd_director_name"}
    assert found["inn_name"]["doc_role"] == "TPP"
    assert found["inn_name"]["source_locator"] == "p2"
    assert found["inn_name"]["confidence"] == 0.9
    assert found["inn_name"]["status"] == "mapped"
    assert found["site_location"]["doc_role"] == "PDD"
    assert found["site_location"]["source_locator"] == "p3"


def test_the_auto_fillable_columns_are_promoted_onto_the_product(psa, rooted, con):
    """These three are what Parts A and B print. Promotion is the difference between a fact
    captured in a side table and a fact on the form."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    product = con.execute(
        "SELECT inn_name, mfr_site_address, site_location FROM product WHERE product_id=?",
        (subject_product_id(con),)).fetchone()

    assert product["inn_name"] == "TrenibotulinumtoxinE"
    assert product["mfr_site_address"] == "AbbVie, Castlebar Road, Westport, County Mayo, Ireland"
    assert product["site_location"] == "North Chicago, Illinois"


def test_each_promoted_column_gets_a_provenance_row_naming_its_document_and_page(
        psa, rooted, con):
    """`verify.py` requires provenance for a promoted field, and a reviewer asking "where did
    this address come from?" is answered by this row alone."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    rows = provenance(con)

    assert set(rows) == {"inn_name", "mfr_site_address", "site_location"}
    assert rows["inn_name"]["source_doc"] == "TPP"
    assert rows["inn_name"]["source_locator"] == "p2"
    assert rows["inn_name"]["match_type"] == "Transformed"
    assert rows["mfr_site_address"]["source_doc"] == "PDD"
    assert rows["mfr_site_address"]["confidence"] == 0.8


def test_a_field_with_no_product_column_is_captured_but_not_promoted(psa, rooted, con):
    """The director's name is read for reference and printed nowhere automatically — Part C is
    an execution-time signature block, and pre-filling it would forge an approval."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    assert "pd_director_name" in attributes(con)
    assert "pd_director_name" not in provenance(con)


# ── the strength, which the two documents disagree about ──────────────────────


def test_a_strength_conflict_is_held_for_review_and_the_column_left_empty(psa, rooted, con):
    """The reason this module exists in the form it does: the reference TPP says 1500 U/vial and
    the PDD says 1400. Auto-filling either would put an unreviewed strength on a signed
    assessment, so the column stays NULL and a review row is raised instead."""
    psa["docs"].extract_and_load(SUBJECT, con,
                                 docs=docs_for(rooted, "1,500 U/vial", "1400 U/vial"))

    pid = subject_product_id(con)
    flagged = con.execute(
        "SELECT status, severity, message FROM validation_result "
        "WHERE entity_id=? AND field_name='per_vial_strength'", (pid,)).fetchone()

    assert flagged["status"] == "review"
    assert flagged["severity"] == "warning"
    assert "1500 U/vial" in flagged["message"] and "1400 U/vial" in flagged["message"]
    assert con.execute("SELECT per_vial_strength FROM product WHERE product_id=?",
                       (pid,)).fetchone()[0] is None
    assert "per_vial_strength" not in provenance(con)


def test_agreeing_strengths_are_promoted_as_an_exact_match(psa, rooted, con):
    """Differently punctuated but the same number — '1,500' and '1 500' — must not read as a
    conflict, or every product would be held for review over formatting."""
    psa["docs"].extract_and_load(SUBJECT, con,
                                 docs=docs_for(rooted, "1,500 U/vial", "1 500 Units per vial"))

    pid = subject_product_id(con)

    assert con.execute("SELECT per_vial_strength FROM product WHERE product_id=?",
                       (pid,)).fetchone()[0] == "1500 U/vial"
    assert provenance(con)["per_vial_strength"]["match_type"] == "Exact"
    assert provenance(con)["per_vial_strength"]["source_doc"] == "TPP"
    assert con.execute("SELECT count(*) FROM validation_result").fetchone()[0] == 0


def test_a_strength_found_in_only_one_document_is_promoted_and_credited_to_it(psa, rooted, con):
    """With no second document there is nothing to disagree with, and the provenance has to
    name the PDD rather than the TPP that was never read."""
    psa["docs"].extract_and_load(
        SUBJECT, con, docs={"PDD": write_pdf(rooted, PDD_REL, pdd_pages("1400 U/vial"))})

    pid = subject_product_id(con)

    assert con.execute("SELECT per_vial_strength FROM product WHERE product_id=?",
                       (pid,)).fetchone()[0] == "1400 U/vial"
    assert provenance(con)["per_vial_strength"]["source_doc"] == "PDD"


# ── the fallback address parse, and the documents that cannot be parsed ───────


def test_the_generic_address_parse_fills_in_when_the_anchored_pattern_missed(psa, rooted, con):
    """The anchored PDD pattern is tuned to the Westport block; any other site would leave
    Part B with only a site code. The generic parse is the fallback, recorded at a lower
    confidence and locator so a reviewer can see it was inferred."""
    rel = write_pdf(rooted, PDD_REL, [
        ["Drug product manufacturing site: 22 Industrial Road, Barceloneta, Puerto Rico"],
    ])

    psa["docs"].extract_and_load(SUBJECT, con, docs={"PDD": rel})

    row = provenance(con)["mfr_site_address"]

    assert row["source_locator"] == "generic-parse"
    assert row["confidence"] == 0.55
    assert con.execute("SELECT mfr_site_address FROM product WHERE product_id=?",
                       (subject_product_id(con),)).fetchone()[0] == \
        "22 Industrial Road, Barceloneta, Puerto Rico"


def test_the_generic_parse_runs_on_past_the_end_of_the_address(psa, rooted, con):
    """CHARACTERISING WHAT IT ACTUALLY DOES — this looks wrong. `extract_mfr_address` only stops
    at a BLANK line, and PDF text rarely has one, so a line following the address is joined onto
    it with a comma. Here the per-vial strength ends up inside the manufacturing address that
    Part B prints."""
    rel = write_pdf(rooted, PDD_REL, [
        ["Drug product manufacturing site: 22 Industrial Road, Barceloneta, Puerto Rico",
         "Each vial contains 1400 U/vial"],
    ])

    psa["docs"].extract_and_load(SUBJECT, con, docs={"PDD": rel})

    assert con.execute("SELECT mfr_site_address FROM product WHERE product_id=?",
                       (subject_product_id(con),)).fetchone()[0] == \
        "22 Industrial Road, Barceloneta, Puerto Rico, Each vial contains 1400 U/vial"


def test_the_anchored_address_is_not_replaced_by_the_generic_one(psa, rooted, con):
    """Precedence: the tuned pattern is the more trustworthy of the two, and its 0.8 confidence
    has to survive a document that both patterns can match."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))

    assert provenance(con)["mfr_site_address"]["source_locator"] == "p2"


def test_a_non_pdf_upload_is_skipped_without_being_recorded(psa, rooted, con, capsys):
    """The anchored extractors are PDF-only. A .docx upload is handled by the new-product A&B
    path instead, and recording a doc_source here would claim fields were read out of it."""
    rel = write_docx(rooted, "Input_Data_Sources/uploads/deck.docx", "Dosage Form: Liquid")

    psa["docs"].extract_and_load(SUBJECT, con, docs={"PDD": rel})

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "is not a PDF" in capsys.readouterr().out


def test_a_registered_file_that_is_absent_is_skipped_and_the_others_still_load(
        psa, rooted, con, capsys):
    """A half-shipped document set must degrade to the fields that are available rather than
    fail the run."""
    docs = {"PDD": "Input_Data_Sources/PDDs/absent.pdf",
            "TPP": write_pdf(rooted, TPP_REL, tpp_pages())}

    psa["docs"].extract_and_load(SUBJECT, con, docs=docs)

    assert [r[0] for r in con.execute("SELECT doc_role FROM doc_source")] == ["TPP"]
    assert "PDD file missing" in capsys.readouterr().out


def test_a_program_that_is_not_in_the_catalogue_writes_nothing(psa, rooted, con, capsys):
    """There is no product row to hang a doc_source off, and a doc_source with a dangling
    product_id would break every provenance query."""
    psa["docs"].extract_and_load("ABBV-999", con, docs=docs_for(rooted))

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "not in DB" in capsys.readouterr().out


def test_a_program_with_no_documents_at_all_writes_nothing(psa, rooted, con, capsys):
    """The common case — most runs are Smartsheet-only — so it is a skip and not an error."""
    psa["docs"].extract_and_load("ABBV-400", con)

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "No PDD/TPP files for ABBV-400" in capsys.readouterr().out


# ── the module entry point ────────────────────────────────────────────────────


def test_the_entry_point_extracts_the_registered_program_end_to_end(psa, rooted, con):
    """`main` is what the CLI and the standalone path call: it opens its own connection to the
    configured database, loads the registry's documents and commits. A regression here is
    invisible to every test that passes its own connection in."""
    registry = psa["docs"].REGISTRY[SUBJECT]
    write_pdf(rooted, registry["PDD"], pdd_pages())
    write_pdf(rooted, registry["TPP"], tpp_pages())

    psa["docs"].main(SUBJECT)

    assert {r[0] for r in con.execute("SELECT doc_role FROM doc_source")} == {"PDD", "TPP"}
    assert con.execute("SELECT inn_name FROM product WHERE product_id=?",
                       (subject_product_id(con),)).fetchone()[0] == "TrenibotulinumtoxinE"


def test_the_entry_point_skips_a_program_with_no_registered_documents(psa, rooted, con, capsys):
    """It must not even open the database for a Smartsheet-only product."""
    psa["docs"].main("ABBV-400")

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "No PDD/TPP for ABBV-400" in capsys.readouterr().out


# ── the analyst assessment record, which overrides the auto-draft ─────────────


RECORD = {
    "onevault_id": "OV-0001234",
    "b_manufacturing_site": "AP16 Westport, County Mayo, Ireland",
    "d1_product_families": "Liquid vial family",
    "d1_similarity_risk": "No",
    "d2_comments": "No distinguishing-attribute overlap with late-stage pipeline products.",
    "e1_abbvie_site": "AP16",
    "e1_product_families": "Liquid vials",
    "e1_similarity_risk": "Yes",
    "e2_comments": "Cap colour differs from every co-located product.",
}


def write_record(psa, program=SUBJECT, **overrides):
    """Write `<program>.json` where `ingest_assessment` looks for it; return the record."""
    record = dict(RECORD)
    record.update(overrides)
    os.makedirs(psa["assessment"].assess_dir(), exist_ok=True)
    with open(psa["assessment"].assessment_path(program), "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return record


def test_the_assessment_record_is_itself_recorded_as_a_json_document(psa, rooted, con):
    """Analyst determinations exist in no data source, so the record IS their source document —
    a reviewer has to be able to see which file the Part D and E text came from."""
    write_record(psa)

    psa["assessment"].main(SUBJECT)

    row = con.execute("SELECT doc_role, source_type, source_file, product_id FROM doc_source "
                      "WHERE doc_role='ASSESSMENT'").fetchone()

    assert row["source_type"] == "json"
    assert row["product_id"] == subject_product_id(con)
    assert row["source_file"].startswith("Input_Data_Sources")   # relative to the input root
    assert row["source_file"].endswith(f"{SUBJECT}.json")


def test_every_answer_becomes_an_attribute_and_a_manual_provenance_row(psa, rooted, con):
    """These are stated by a person, not derived by the tool, and the form has to be able to
    show that: 'Manual', confidence 1.0, pointing at the record."""
    record = write_record(psa)

    psa["assessment"].main(SUBJECT)

    stored = {r["attribute_key"]: r for r in con.execute(
        "SELECT a.attribute_key, a.value_text, a.confidence, a.status, a.source_locator "
        "FROM doc_attribute a JOIN doc_source s ON s.doc_id=a.doc_id "
        "WHERE s.doc_role='ASSESSMENT'")}
    prov = provenance(con, "manual")

    assert set(stored) == set(psa["assessment"].ATTR_KEYS)
    assert stored["d2_comments"]["value_text"] == record["d2_comments"]
    assert stored["d2_comments"]["confidence"] == 1.0
    assert stored["d2_comments"]["status"] == "mapped"
    assert prov["d1_similarity_risk"]["source_doc"] == "Manual"
    assert prov["d1_similarity_risk"]["match_type"] == "Exact"
    assert prov["d1_similarity_risk"]["source_locator"] == stored["d2_comments"]["source_locator"]


def test_a_blank_answer_is_not_recorded(psa, rooted, con):
    """An empty string is an unanswered question, and storing it would render as
    "considered and found to be nothing" in a signed assessment."""
    write_record(psa, e2_comments="", d2_comments=None)

    psa["assessment"].main(SUBJECT)

    stored = {r[0] for r in con.execute(
        "SELECT a.attribute_key FROM doc_attribute a JOIN doc_source s ON s.doc_id=a.doc_id "
        "WHERE s.doc_role='ASSESSMENT'")}

    assert "e2_comments" not in stored and "d2_comments" not in stored
    assert "onevault_id" in stored


def test_the_resolved_strength_fills_the_column_that_was_held(psa, rooted, con):
    """This is the handshake with the conflict: the extractor refuses to guess, and the analyst
    record is the only thing allowed to supply the number the form prints."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))
    write_record(psa, resolved_per_vial_strength="1500 U/vial")

    psa["assessment"].main(SUBJECT)

    pid = subject_product_id(con)

    assert con.execute("SELECT per_vial_strength FROM product WHERE product_id=?",
                       (pid,)).fetchone()[0] == "1500 U/vial"
    assert provenance(con, "manual")["per_vial_strength"]["match_type"] == "Derived"


def test_resolving_the_strength_keeps_the_conflict_row_and_appends_the_decision(
        psa, rooted, con):
    """The audit row is not deleted — a reviewer has to be able to see that there WAS a
    conflict, who resolved it and on what basis. Overwriting the message would erase the
    reason."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))
    write_record(psa, resolved_per_vial_strength="1500 U/vial",
                 resolved_strength_note="TPP confirmed by CMC PD")

    psa["assessment"].main(SUBJECT)

    row = con.execute("SELECT status, message FROM validation_result "
                      "WHERE field_name='per_vial_strength'").fetchone()

    assert row["status"] == "resolved"
    assert "Strength conflict: TPP 1500 U/vial vs PDD 1400 U/vial" in row["message"]
    assert "RESOLVED: analyst set 1500 U/vial (TPP confirmed by CMC PD)." in row["message"]


def test_a_record_with_no_resolved_strength_leaves_the_conflict_open(psa, rooted, con):
    """Parts D and E can be signed off before the strength question is settled, and the review
    flag must survive that — otherwise the unresolved number quietly reaches the form."""
    psa["docs"].extract_and_load(SUBJECT, con, docs=docs_for(rooted))
    write_record(psa)

    psa["assessment"].main(SUBJECT)

    pid = subject_product_id(con)

    assert con.execute("SELECT per_vial_strength FROM product WHERE product_id=?",
                       (pid,)).fetchone()[0] is None
    assert con.execute("SELECT status FROM validation_result "
                       "WHERE field_name='per_vial_strength'").fetchone()[0] == "review"
    assert "per_vial_strength" not in provenance(con, "manual")


def test_no_assessment_record_leaves_the_catalogue_untouched(psa, rooted, con, capsys):
    """The usual case: Parts D and E are auto-drafted from the risk engine, so a missing record
    is a skip and the run continues."""
    psa["assessment"].main(SUBJECT)

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "No assessment file for" in capsys.readouterr().out


def test_a_record_for_a_program_not_in_the_catalogue_is_refused(psa, rooted, con, capsys):
    """A record whose program code does not match the catalogue is a typo, not a product, and
    loading it against no product row would strand every attribute."""
    write_record(psa, "ABBV-999")

    psa["assessment"].main("ABBV-999")

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "not in DB" in capsys.readouterr().out


# ── the command line, which is how both modules are run by hand ───────────────


@pytest.fixture
def run_cli(monkeypatch):
    """Execute a silo module's `__main__` block with the given argv, once.

    The module is dropped from `sys.modules` for the duration because `runpy` refuses to
    re-execute a live module quietly — `monkeypatch.delitem` puts the original object back, so
    the module-scoped `psa` fixture and every later test still see the one they imported.
    """
    def run(module_name, *args):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
        monkeypatch.setattr(sys, "argv", [f"{module_name.rsplit('.', 1)[-1]}.py", *args])
        runpy.run_module(module_name, run_name="__main__")

    return run


EXTRACT_CLI = "da_silos.psa.extract_docs"
ASSESS_CLI = "da_silos.psa.ingest_assessment"


@pytest.fixture
def registered_documents(psa, rooted):
    """The reference program's registered PDD and TPP, written where the REGISTRY points.

    The `__main__` tests cannot patch `REGISTRY`, because `runpy` executes a fresh copy of the
    module with a fresh copy of the map. So they write the real registered relative paths —
    under `tmp_path`, which is where `rooted` has moved the input root.
    """
    registry = psa["docs"].REGISTRY[SUBJECT]
    write_pdf(rooted, registry["PDD"], pdd_pages())
    write_pdf(rooted, registry["TPP"], tpp_pages())
    return registry


def test_the_dump_subcommand_prints_the_registered_documents(psa, registered_documents,
                                                             run_cli, capsys):
    """`--dump` with no program is the documented first step — "run `--dump` first" — and it has
    to default to the reference program, or the invocation in the module docstring prints
    nothing."""
    run_cli(EXTRACT_CLI, "--dump")
    out = capsys.readouterr().out

    assert f"PDD: {registered_documents['PDD']}" in out
    assert f"TPP: {registered_documents['TPP']}" in out
    assert "Nonproprietary name: Trenibotulinumtoxin E" in out


def test_the_dump_subcommand_takes_the_program_to_dump(psa, rooted, run_cli, capsys):
    """Every subcommand is meant to accept a program, so tuning a pattern against a second
    product does not require editing the module."""
    run_cli(EXTRACT_CLI, "--dump", "ABBV-400")

    assert "No PDD/TPP files registered for ABBV-400" in capsys.readouterr().out


def test_the_images_subcommand_lists_the_pdd_images(psa, rooted, run_cli, capsys):
    """`--images` is how the pinned page/index in the REGISTRY was chosen, so the dispatch has
    to reach `list_images` rather than the extractor — the two print nothing alike."""
    write_image_pdf(rooted, psa["docs"].REGISTRY[SUBJECT]["PDD"],
                    [(148, 384, "RGB")], pages=7, on_page=6)

    run_cli(EXTRACT_CLI, "--images")
    out = capsys.readouterr().out

    assert "page 7  index 0" in out
    assert "148x384px" in out


def test_the_dump_pdf_subcommand_defaults_to_the_signed_reference_output(psa, rooted,
                                                                        run_cli, capsys):
    """The default is `EXAMPLE_PDF`, the signed assessment the report text was transcribed from.
    Defaulting to a registered PDD instead would dump the wrong document."""
    write_pdf(rooted, psa["docs"].EXAMPLE_PDF, [["verbatim signed assessment text"]])

    run_cli(EXTRACT_CLI, "--dump-pdf")

    assert "verbatim signed assessment text" in capsys.readouterr().out


def test_the_dump_pdf_subcommand_takes_any_path(psa, rooted, run_cli, capsys):
    """The argument is a path rather than a program, which is what makes it usable on an
    uploaded document that is in no registry."""
    rel = write_pdf(rooted, "Input_Data_Sources/uploads/handed to us.pdf", [["uploaded text"]])

    run_cli(EXTRACT_CLI, "--dump-pdf", rel)

    assert "uploaded text" in capsys.readouterr().out


def test_a_bare_program_argument_runs_the_extraction_and_load(psa, registered_documents,
                                                              run_cli, con):
    """The default subcommand is the real work, not a dump: `python -m ...extract_docs
    AGN-151586` has to reach the database. A dispatch that fell through to a dump would exit
    zero and load nothing."""
    run_cli(EXTRACT_CLI, SUBJECT)

    assert {r[0] for r in con.execute("SELECT doc_role FROM doc_source")} == {"PDD", "TPP"}
    assert con.execute("SELECT inn_name FROM product WHERE product_id=?",
                       (subject_product_id(con),)).fetchone()[0] == "TrenibotulinumtoxinE"


def test_no_arguments_at_all_extracts_the_reference_program(psa, registered_documents,
                                                            run_cli, con):
    """`python -m da_silos.psa.extract_docs` with nothing after it is the smoke test, so it has
    to fall through to `main()` with the reference program rather than print usage."""
    run_cli(EXTRACT_CLI)

    assert {r[0] for r in con.execute("SELECT doc_role FROM doc_source")} == {"PDD", "TPP"}


def test_an_unknown_program_on_the_command_line_is_reported_not_extracted(psa, rooted,
                                                                          run_cli, con, capsys):
    """A mistyped program code must not silently extract the reference product's documents into
    the catalogue under the wrong name."""
    run_cli(EXTRACT_CLI, "ABBV-400")

    assert con.execute("SELECT count(*) FROM doc_source").fetchone()[0] == 0
    assert "No PDD/TPP for ABBV-400" in capsys.readouterr().out


def test_the_assessment_command_line_loads_the_program_it_is_given(psa, rooted, run_cli, con):
    """The assessment record is loaded by a separate command in the same run, and its argument
    is the program — loading the default one instead would attach an analyst's determinations to
    the wrong product."""
    write_record(psa, SUBJECT, onevault_id="OV-0009999")

    run_cli(ASSESS_CLI, SUBJECT)

    stored = con.execute(
        "SELECT a.value_text FROM doc_attribute a JOIN doc_source s ON s.doc_id=a.doc_id "
        "WHERE s.doc_role='ASSESSMENT' AND a.attribute_key='onevault_id'").fetchone()

    assert stored["value_text"] == "OV-0009999"


def test_the_assessment_command_line_defaults_to_the_reference_program(psa, rooted, run_cli,
                                                                       con):
    """Run with no argument it has to load the reference program's record, which is what makes
    the pipeline's final step runnable by hand."""
    write_record(psa, SUBJECT, resolved_per_vial_strength="1500 U/vial")

    run_cli(ASSESS_CLI)

    assert con.execute("SELECT per_vial_strength FROM product WHERE program_no=?",
                       (SUBJECT,)).fetchone()[0] == "1500 U/vial"
