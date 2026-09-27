"""Pulling Part A/B facts out of uploaded documents, and the analyst assessment record.

A PSA run starts from the Smartsheet, but an assessor may also drop in a PDD, a TPP or a
presentation. `extract_docs.py` reads Part A/B fields out of whatever text those contain — real
documents written by different people over several years, so the extractors are label-pattern
searches rather than a schema.

**Every extractor here is deliberately conservative, and in a specific direction: it returns
nothing rather than something wrong.** An unfound field is omitted, which renders as `[PENDING]`
or `TBD` in the form and prompts the assessor to supply it. A wrongly-extracted field renders as
a fact in a signed GxP document. `extract_mfr_address` states this outright — it requires both a
comma AND an address cue, "otherwise None so the caller falls back to the Smartsheet site code
(never a wrong address on a governance form)".

So the tests come in pairs: what the extractor must find, and what it must refuse to guess at.
The refusals are the more important half.

These are text-level tests. Nothing here opens a PDF: `_pages` is the PyMuPDF boundary and the
extractors take text, which is exactly the seam that makes them testable without a corpus.
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
    from da_silos.psa import config, extract_docs, ingest_assessment

    return {"docs": extract_docs, "assessment": ingest_assessment, "config": config}


@pytest.fixture(autouse=True)
def restore_config(psa):
    before = psa["config"]._INJECTED
    yield
    psa["config"]._INJECTED = before


@pytest.fixture
def workspace(psa, tmp_path):
    import dataclasses

    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(),
        work_dir=str(tmp_path),
        db_path=str(tmp_path / "Database" / "psa.db"),
        output_dir=str(tmp_path / "Output_Files"),
        uploads_dir=str(tmp_path / "uploads"),
        storage_dir=str(tmp_path / "store"),
    ))
    return tmp_path


# ── finding a value beside its label ──────────────────────────────────────────


def test_a_labelled_field_is_extracted(psa):
    found = psa["docs"].extract_generic_ab("Dosage Form: Lyophilised powder")

    assert found["form"] == "Lyophilised powder"


@pytest.mark.parametrize("separator", [":", "-", "–", "—"])
def test_any_of_the_separators_documents_use_is_accepted(psa, separator):
    """Four different dashes appear across real documents, and an en dash is what Word
    autocorrects a hyphen into."""
    found = psa["docs"].extract_generic_ab(f"Product Name {separator} Etentamig")

    assert found["program_name"] == "Etentamig"


@pytest.mark.parametrize("label,expected", [
    ("Product Name", "program_name"), ("Compound Name", "program_name"),
    ("Molecule Name", "program_name"), ("Dosage Form", "form"),
    ("Drug Product Type", "form"), ("Route of Administration", "route_of_admin"),
    ("Container Closure", "vial_container_size"), ("Presentation", "vial_container_size"),
    ("Colour", "product_color"), ("Color", "product_color"),
    ("Shape", "shape"), ("Marking", "marking"), ("Debossing", "marking"),
    ("Manufacturing Site", "dp_mfr_site_raw"), ("Packaging Site", "dp_pkging_site_raw"),
])
def test_the_synonyms_real_documents_use_all_map_to_one_column(psa, label, expected):
    """Documents written by different people over several years call the same field different
    things. Each alias has to reach the same database column or the field lands as PENDING."""
    found = psa["docs"].extract_generic_ab(f"{label}: SOMEVALUE")

    assert found.get(expected) == "SOMEVALUE", f"{label} should fill {expected}"


def test_a_label_is_matched_case_insensitively(psa):
    assert psa["docs"].extract_generic_ab("DOSAGE FORM: Liquid")["form"] == "Liquid"


def test_the_first_matching_alias_wins(psa):
    """The patterns are ordered most-specific first, so 'Drug Product Name' should not be
    beaten by the looser '\\bForm\\b' style fallbacks."""
    found = psa["docs"].extract_generic_ab(
        "Drug Product Name: Etentamig\nMolecule: Something Else"
    )

    assert found["program_name"] == "Etentamig"


def test_a_value_stops_at_a_column_gap(psa):
    """These come from PDF text where a table renders as wide runs of spaces, so the next
    column would otherwise be swallowed into the value."""
    found = psa["docs"].extract_generic_ab("Shape: Vial      Marking: None")

    assert found["shape"] == "Vial"


def test_a_value_stops_at_a_tab(psa):
    found = psa["docs"].extract_generic_ab("Shape:\tVial\tMarking: None")

    assert found["shape"] == "Vial"


def test_several_fields_are_extracted_from_one_document(psa):
    text = (
        "Product Name: Etentamig\n"
        "Dosage Form: Lyophilised powder\n"
        "Route of Administration: Intravenous\n"
        "Colour: White to off-white\n"
    )

    found = psa["docs"].extract_generic_ab(text)

    assert found["program_name"] == "Etentamig"
    assert found["form"] == "Lyophilised powder"
    assert found["route_of_admin"] == "Intravenous"
    assert found["product_color"] == "White to off-white"


# ── what it must refuse to guess ──────────────────────────────────────────────


def test_an_unlabelled_document_yields_nothing(psa):
    """Which renders as [PENDING] and asks the assessor, rather than inventing a fact."""
    assert psa["docs"].extract_generic_ab("Some prose with no labelled fields at all") == {}


@pytest.mark.parametrize("empty", ["", None])
def test_no_text_yields_nothing(psa, empty):
    """An unreadable or image-only PDF reaches here as empty text, and it must not fail the
    run."""
    assert psa["docs"].extract_generic_ab(empty) == {}


def test_a_label_with_no_value_is_not_extracted(psa):
    """A blank cell in the source is not an answer."""
    assert "form" not in psa["docs"].extract_generic_ab("Dosage Form:")


def test_a_punctuation_only_value_is_rejected(psa):
    """Table borders and rule characters land here, and '---' as a dosage form would be a
    stated fact in the form."""
    assert "form" not in psa["docs"].extract_generic_ab("Dosage Form: ----")


def test_an_implausibly_long_value_is_rejected(psa):
    """A missed line break turns a whole paragraph into the value, and 120 characters of
    prose in the Form cell would be obviously wrong to a reader but silently wrong to the
    generator."""
    assert "form" not in psa["docs"].extract_generic_ab(f"Dosage Form: {'x' * 200}")


def test_a_value_at_the_length_limit_is_still_accepted(psa):
    """The boundary, so the guard is a guard rather than an accidental narrowing."""
    value = "y" * 120

    assert psa["docs"].extract_generic_ab(f"Dosage Form: {value}")["form"] == value


# ── the strength, whose formatting varies most ────────────────────────────────


@pytest.mark.parametrize("written", ["1,500 U/vial", "1 500 U/vial", "1500 U/vial",
                                     "1500 Units/vial", "1500 U per vial"])
def test_a_strength_is_read_however_it_is_punctuated(psa, written):
    """Thousands separators vary by author and locale, and the value has to normalise to one
    form or two documents describing the same product would disagree."""
    assert psa["docs"]._strength(f"Each vial contains {written}.") == "1500 U/vial"


def test_a_strength_is_matched_case_insensitively(psa):
    assert psa["docs"]._strength("1500 u/VIAL") == "1500 U/vial"


def test_no_strength_is_invented(psa):
    assert psa["docs"]._strength("Contains an unspecified amount per vial") is None


def test_a_bare_number_is_not_a_strength(psa):
    """Documents are full of numbers; only one beside a per-vial unit is a strength."""
    assert psa["docs"]._strength("Batch size 1500") is None


# ── the manufacturing address, the most dangerous field to get wrong ──────────


def test_an_address_near_a_manufacturing_anchor_is_found(psa):
    text = "Drug Product Manufacturing Site: 1 Waverley Road, Westport, Co Mayo, Ireland"

    found = psa["docs"].extract_mfr_address(text)

    assert found is not None
    assert "Westport" in found


@pytest.mark.parametrize("anchor", [
    "Manufacturing Site", "Site of Manufacture", "Manufactured at",
    "Manufactured by", "DP Manufacturing Site", "Manufacturing Facility",
])
def test_the_anchors_real_documents_use_are_all_recognised(psa, anchor):
    text = f"{anchor}: 22 Industrial Road, Barceloneta, Puerto Rico"

    assert psa["docs"].extract_mfr_address(text) is not None


def test_a_wrapped_address_is_joined_into_one_line(psa):
    """It becomes a site name in the form, so the line breaks have to go."""
    text = "Manufacturing Site:\n1 Waverley Road\nWestport\nCo Mayo, Ireland"

    found = psa["docs"].extract_mfr_address(text)

    assert found is not None
    assert "\n" not in found


def test_the_address_stops_at_a_blank_line(psa):
    """The next section of the document is not part of the address."""
    text = (
        "Manufacturing Site: 1 Waverley Road, Westport, Ireland\n"
        "\n"
        "Packaging Site: somewhere else entirely\n"
    )

    found = psa["docs"].extract_mfr_address(text)

    assert "Packaging" not in found


def test_a_capture_with_no_address_cue_is_refused(psa):
    """The conservative rule stated in the docstring: without a street type, country or
    postal code it is not recognisably an address, so the caller falls back to the Smartsheet
    site code. Never a wrong address on a governance form."""
    text = "Manufacturing Site: to be confirmed, pending selection"

    assert psa["docs"].extract_mfr_address(text) is None


def test_a_capture_with_no_comma_is_refused(psa):
    """Both conditions are required. A single token is a site code, not an address, and the
    Smartsheet already holds those."""
    assert psa["docs"].extract_mfr_address("Manufacturing Site: Ireland") is None


def test_text_with_no_manufacturing_anchor_yields_nothing(psa):
    """An address elsewhere in the document — a sponsor's head office, say — must not be
    reported as the manufacturing site."""
    text = "Correspondence: 1 Waverley Road, Westport, Co Mayo, Ireland"

    assert psa["docs"].extract_mfr_address(text) is None


@pytest.mark.parametrize("empty", ["", None])
def test_no_text_yields_no_address(psa, empty):
    assert psa["docs"].extract_mfr_address(empty) is None


def test_an_implausibly_long_capture_is_refused(psa):
    """A document with no blank line after the anchor would otherwise yield a paragraph."""
    text = "Manufacturing Site: " + ", ".join(["Road Street Ireland"] * 40)

    assert psa["docs"].extract_mfr_address(text) is None


# ── locating a fact in the document, for the provenance trail ──────────────────


def test_the_page_a_value_came_from_is_reported(psa):
    """Provenance: a reviewer has to be able to turn to the page. `verify.py` checks that
    provenance rows exist at all."""
    pages = ["cover page", "product details", "Dosage Form: Liquid"]

    assert psa["docs"]._page_of(pages, r"Dosage\s*Form") == 3


def test_the_page_number_is_one_based(psa):
    """It is shown to a person reading a PDF, where the first page is 1."""
    assert psa["docs"]._page_of(["Dosage Form: Liquid"], r"Dosage") == 1


def test_the_first_occurrence_wins(psa):
    """A field repeated in a summary and a detail section should cite the earlier page."""
    assert psa["docs"]._page_of(["Strength", "other", "Strength"], r"Strength") == 1


def test_a_value_that_appears_nowhere_has_no_page(psa):
    assert psa["docs"]._page_of(["cover", "details"], r"Marking") is None


def test_no_pages_yields_no_page(psa):
    assert psa["docs"]._page_of([], r"anything") is None


def test_a_pattern_search_returns_the_captured_group(psa):
    assert psa["docs"]._find(r"Form:\s*(\w+)", "Form: Liquid") == "Liquid"


def test_a_failed_pattern_search_returns_nothing(psa):
    """Never a partial or a placeholder — the caller omits the field."""
    assert psa["docs"]._find(r"Form:\s*(\w+)", "no form here") is None


def test_a_found_value_is_trimmed(psa):
    assert psa["docs"]._find(r"Form:(.*)", "Form:   Liquid   ") == "Liquid"


# ── which documents exist for a program ───────────────────────────────────────


def test_a_program_with_no_registered_documents_is_reported_as_having_none(psa, workspace):
    """The normal case: most runs are Smartsheet-only, and `has_docs` is what lets the
    workflow skip the extraction rather than fail on a missing file."""
    assert psa["docs"].has_docs("ABBV-400") is False


def test_the_registered_reference_program_is_reported_as_having_documents(psa):
    """`REGISTRY` is a hardcoded map rather than a directory scan, so `has_docs` answers from
    the map. AGN-151586 (BoNT/E) is the transcribed reference case the extractors were tuned
    against, and it is the only entry — a second program's documents need a REGISTRY entry,
    not just a file on disk."""
    assert psa["docs"].has_docs("AGN-151586") is True
    assert list(psa["docs"].REGISTRY) == ["AGN-151586"]


def test_the_registry_names_a_role_for_each_document_type(psa):
    """The role decides which extractor runs, so an unregistered type would be uploaded and
    silently ignored."""
    assert set(psa["docs"].EXTRACTORS) <= set(psa["docs"].REGISTRY) | {"PDD", "TPP"}
    assert "PDD" in psa["docs"].EXTRACTORS
    assert "TPP" in psa["docs"].EXTRACTORS


def test_the_document_list_skips_the_photo_configuration_entry(psa):
    """`REGISTRY` holds a 'photo' key that is a configuration entry rather than a document,
    and treating it as one would try to extract text from an image path."""
    roles = [role for role, _path in psa["docs"]._doc_files("AGN-151586")]

    assert "photo" not in roles


# ── the analyst assessment record, which overrides the auto-draft ─────────────


def test_a_program_with_no_assessment_record_is_reported_as_having_none(psa, workspace):
    """Parts D and E are auto-drafted from the risk engine when there is no record, so this
    is the common path rather than an error."""
    assert psa["assessment"].has_assessment("AGN-151586") is False


def test_the_assessment_path_is_named_for_the_program(psa, workspace):
    """One record per program, found by convention rather than configuration."""
    path = psa["assessment"].assessment_path("AGN-151586")

    assert "AGN-151586" in path


def test_the_assessment_is_read_from_the_shipped_input_tree(psa, workspace):
    """`assess_dir` resolves under `paths.root()` rather than `work_dir`, and that is correct
    here: an assessment record is read-only INPUT data that ships with the silo, not something
    a run writes. Only the derived database and the generated output follow `work_dir`."""
    assert psa["assessment"].assess_dir().startswith(psa["assessment"].paths.root())
    assert "Input_Data_Sources" in psa["assessment"].assess_dir()
    assert str(workspace) not in psa["assessment"].assess_dir()


def test_the_recorded_attributes_are_the_ones_the_form_needs(psa):
    """A key absent from `ATTR_KEYS` cannot be read out of a record, so an analyst could fill
    it in and see it ignored."""
    assert psa["assessment"].ATTR_KEYS
    assert all(isinstance(key, str) for key in psa["assessment"].ATTR_KEYS)
