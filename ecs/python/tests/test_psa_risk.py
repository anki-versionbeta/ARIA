"""The mix-up risk engine: which products could be confused with which, and why.

`risk.py` is PSA's largest module and the one an assessor's judgement actually rests on. It
answers two questions — how similar is this presentation to every other one made at the same
site, and what should Parts D and E of the form say about it.

Two kinds of test here, and the distinction matters:

  * the pure functions (`grade`, `_norm`, `_compare`, the family grouping) are tested directly.
    They are **characterisation** tests: the thresholds were calibrated against three signed
    baseline assessments (`docs/PSA_Baseline_Test_Combined.md`), so these pin the numbers that
    were agreed rather than a specification. `GRADE_THRESHOLDS` and `RISK_YES_LEVELS` are marked
    SME-tunable in the source — if an SME retunes them, these tests are the record of what
    changed, which is the point of pinning them.

  * the query functions get a real SQLite database built from the shipped `assets/schema.sql`
    and populated row by row. A fake connection would test the shape of our SQL string rather
    than what SQLite does with it, and the scope predicate (`p2.batch_type IS NULL OR ...`) is
    exactly the kind of thing that reads correctly and behaves otherwise.

The conservative direction of every rule is the thing to hold on to: an unknown value is never
treated as a difference, because a value nobody recorded cannot be relied on to tell two vials
apart. Getting that backwards would let the tool report a low mix-up risk on the strength of
missing data.
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
    from da_silos.psa import asset_store, risk

    return {"risk": risk, "asset_store": asset_store}


@pytest.fixture
def con(psa):
    """An empty PSA database, from the shipped DDL.

    Built from `assets/schema.sql` rather than from a hand-written subset so a column the
    engine reads cannot be missing here while present in production.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(psa["asset_store"].read_text("schema.sql"))
    yield connection
    connection.close()


def add_product(con, product_id, **columns):
    """Insert one presentation. Only the columns a test cares about need naming."""
    row = {"product_id": product_id, "program_no": f"PRG-{product_id}",
           "program_name": f"Program {product_id}", "batch_type": "Commercial",
           "status": "Commercial", "modality": "Liquid", "form": "Liquid",
           **columns}
    keys = ", ".join(row)
    marks = ", ".join("?" * len(row))
    con.execute(f"INSERT INTO product ({keys}) VALUES ({marks})", list(row.values()))
    con.commit()
    return product_id


def add_site(con, site_id, code="SITE-A"):
    con.execute("INSERT INTO site (site_id, site_code) VALUES (?, ?)", (site_id, code))
    con.commit()
    return site_id


def link_site(con, product_id, site_id, role="dp_mfr"):
    con.execute(
        "INSERT INTO product_site (product_id, site_id, role) VALUES (?, ?, ?)",
        (product_id, site_id, role),
    )
    con.commit()


def add_cap(con, product_id, name="", code=""):
    con.execute(
        "INSERT INTO cap_color (product_id, cap_color_name, color_code) VALUES (?, ?, ?)",
        (product_id, name, code),
    )
    con.commit()


# ── the grade ladder ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("n_diffs,expected", [
    (0, "High"), (1, "High"), (2, "Med"), (3, "Low"), (4, "Low"), (7, "Low"),
])
def test_fewer_differences_means_higher_risk(psa, n_diffs, expected):
    """Calibrated against the signed baselines: assessors treat two clear differentiators as
    enough to rule out a mix-up, so only near-identical products (0-1 diffs) grade High."""
    assert psa["risk"].grade(n_diffs) == expected


def test_the_thresholds_are_the_calibrated_ones(psa):
    """These two numbers are the whole ladder. Pinned because they are SME-tunable, so a
    change should be a deliberate edit with this test updated alongside it."""
    assert psa["risk"].GRADE_THRESHOLDS == {"high_max_diffs": 1, "med_max_diffs": 2}


def test_only_high_risk_ticks_the_forms_yes_box(psa):
    """Baseline-calibrated: Yes ONLY when near-identical. A 2-diff "Med" pair maps to No but
    is surfaced as a borderline note, which lifted auto-alignment with the signed examples
    from 1/7 to 6/7."""
    assert psa["risk"].RISK_YES_LEVELS == {"High"}
    assert psa["risk"]._box("High") == "Yes"
    assert psa["risk"]._box("Med") == "No"
    assert psa["risk"]._box("Low") == "No"


# ── normalisation, which decides what counts as "the same" ────────────────────


@pytest.mark.parametrize("left,right", [
    ("2R", "2r"), ("2 mL", "2mL"), ("ABBV-400", "abbv400"),
    ("  White  ", "white"), ("20/40", "2040"),
])
def test_formatting_differences_are_not_real_differences(psa, left, right):
    """The sheet is hand-entered, so punctuation and case vary between rows describing the
    same thing. Treating '2 mL' and '2mL' as different would invent a differentiator."""
    assert psa["risk"]._norm(left) == psa["risk"]._norm(right)


def test_genuinely_different_values_stay_different(psa):
    """The normaliser must not be so aggressive that it erases real distinctions."""
    assert psa["risk"]._norm("2R") != psa["risk"]._norm("6R")


@pytest.mark.parametrize("blank", [None, "", "   ", "---"])
def test_blank_shaped_values_normalise_to_nothing(psa, blank):
    """Which is what makes them non-distinguishing downstream."""
    assert psa["risk"]._norm(blank) == ""


def test_a_number_normalises_the_same_as_its_string(psa):
    """Fill volumes arrive as both from SQLite."""
    assert psa["risk"]._norm(2) == psa["risk"]._norm("2")


# ── comparing two presentations ───────────────────────────────────────────────


def presentation(**columns):
    """A presentation dict in the shape `_presentation` returns."""
    base = {"product_id": 1, "program_no": "PRG-1", "program_name": "One",
            "form": "Liquid", "vial_container_size": "2R",
            "target_fill_volume_ml": "2.0", "product_color": "Colourless",
            "shape": "Vial", "marking": "None", "modality": "Liquid",
            "cap_colors": set()}
    return {**base, **columns}


def test_identical_presentations_have_no_distinguishing_attributes(psa):
    """Which grades High — two vials nobody could tell apart is the case the form exists for."""
    rows, n = psa["risk"]._compare(presentation(), presentation())

    assert n == 0
    assert psa["risk"].grade(n) == "High"


def test_a_differing_attribute_is_counted(psa):
    rows, n = psa["risk"]._compare(
        presentation(vial_container_size="2R"), presentation(vial_container_size="6R")
    )

    assert n == 1
    row = next(r for r in rows if r["attribute"] == "Vial / container size")
    assert row["is_distinguishing"] is True


def test_an_unknown_value_is_never_distinguishing(psa):
    """The conservative rule, and the most important one in the module: an attribute nobody
    recorded cannot be relied on to tell products apart. Counting it as a difference would
    lower the reported mix-up risk on the strength of missing data."""
    rows, n = psa["risk"]._compare(
        presentation(marking="ABC-123"), presentation(marking="")
    )

    assert n == 0
    row = next(r for r in rows if r["attribute"] == "Marking")
    assert row["is_distinguishing"] is False


def test_both_sides_unknown_is_not_a_difference_either(psa):
    rows, n = psa["risk"]._compare(presentation(shape=None), presentation(shape=None))

    assert n == 0


def test_every_appearance_attribute_is_reported(psa):
    """The table in the report shows all of them, differing or not — an assessor needs to
    see what was compared, not only what differed."""
    rows, _ = psa["risk"]._compare(presentation(), presentation())

    labels = {r["attribute"] for r in rows}
    for _, label in psa["risk"].APPEARANCE_ATTRS:
        assert label in labels
    assert "Cap colour" in labels, "handled separately, but still reported"


def test_the_comparison_rows_carry_both_values(psa):
    """So the report can show them side by side without a second lookup."""
    rows, _ = psa["risk"]._compare(
        presentation(product_color="Colourless"), presentation(product_color="Yellow")
    )

    row = next(r for r in rows if r["attribute"] == "Product colour")
    assert (row["subject_value"], row["comparator_value"]) == ("Colourless", "Yellow")


def test_values_are_trimmed_for_display(psa):
    rows, _ = psa["risk"]._compare(presentation(shape="  Vial  "), presentation())

    assert next(r for r in rows if r["attribute"] == "Shape")["subject_value"] == "Vial"


def test_a_missing_value_displays_as_empty_rather_than_none(psa):
    """"None" in a GxP form reads as a stated answer; blank reads as not recorded."""
    rows, _ = psa["risk"]._compare(presentation(marking=None), presentation())

    assert next(r for r in rows if r["attribute"] == "Marking")["subject_value"] == ""


def test_several_differences_accumulate(psa):
    rows, n = psa["risk"]._compare(
        presentation(vial_container_size="2R", product_color="Colourless", shape="Vial"),
        presentation(vial_container_size="6R", product_color="Yellow", shape="Cartridge"),
    )

    assert n == 3
    assert psa["risk"].grade(n) == "Low"


# ── cap colour, which is a set comparison rather than a value ─────────────────


def test_caps_that_share_no_colour_are_distinguishing(psa):
    """Different caps are what the cap-colour programme exists to achieve, so this is the
    differentiator the assessment is usually looking for."""
    rows, n = psa["risk"]._compare(
        presentation(cap_colors={"BLUE"}), presentation(cap_colors={"RED"})
    )

    assert next(r for r in rows if r["attribute"] == "Cap colour")["is_distinguishing"]


def test_caps_sharing_one_colour_are_not_distinguishing(psa):
    """A presentation may ship with several cap colours. Sharing even one means the two could
    appear identical on a shelf, so the pair must not be treated as distinguished."""
    rows, n = psa["risk"]._compare(
        presentation(cap_colors={"BLUE", "RED"}), presentation(cap_colors={"RED"})
    )

    assert next(r for r in rows if r["attribute"] == "Cap colour")["is_distinguishing"] is False


def test_an_unknown_cap_is_not_distinguishing(psa):
    """Same conservative rule as every other attribute: at assessment time the cap is often
    not yet chosen, and an empty set is not evidence of difference."""
    rows, _ = psa["risk"]._compare(
        presentation(cap_colors={"BLUE"}), presentation(cap_colors=set())
    )

    assert next(r for r in rows if r["attribute"] == "Cap colour")["is_distinguishing"] is False


def test_the_cap_values_are_listed_alphabetically(psa):
    """A stable order, so the same pair renders the same way in every report."""
    rows, _ = psa["risk"]._compare(
        presentation(cap_colors={"RED", "BLUE"}), presentation()
    )

    assert next(r for r in rows if r["attribute"] == "Cap colour")["subject_value"] == \
        "BLUE, RED"


# ── the dosage-form family, which scopes the comparator universe ──────────────


@pytest.mark.parametrize("modality,expected", [
    ("Liquid", "LIQUID"), ("Frozen Liquid", "LIQUID"),
    ("Lyo Powder", "LYOPHILISED"), ("Lyo-Cake", "LYOPHILISED"),
])
def test_the_sheets_modalities_group_into_families(psa, modality, expected):
    """The signed baselines compare within "filled liquid vials" and "lyophilised powder vial
    products", which are coarser than the sheet's `modality` values."""
    assert psa["risk"]._family_key(presentation(modality=modality)) == expected


def test_an_unrecognised_modality_falls_back_to_the_form_text(psa):
    """New modality wording should degrade to a reasonable grouping rather than to none."""
    assert psa["risk"]._family_key(
        presentation(modality="Something New", form="Lyophilised powder")
    ) == "LYOPHILISED"


def test_a_modality_that_matches_nothing_has_no_family(psa):
    """None means "fall back to comparing on form", which `_family_predicate` then does."""
    assert psa["risk"]._family_key(presentation(modality="", form="Gel")) is None


def test_products_in_the_same_family_are_comparable(psa):
    assert psa["risk"]._same_family(
        presentation(modality="Liquid"), presentation(modality="Frozen Liquid")
    ) is True


def test_products_in_different_families_are_not_comparable(psa):
    """A lyophilised cake and a filled liquid vial do not get confused with each other, so
    including them would dilute the assessment with irrelevant comparators."""
    assert psa["risk"]._same_family(
        presentation(modality="Liquid"), presentation(modality="Lyo Powder")
    ) is False


def test_the_family_predicate_names_every_modality_in_the_family(psa):
    """It is SQL with bound parameters, so the placeholder count has to match the values —
    a mismatch is a runtime error rather than a wrong answer."""
    sql, params = psa["risk"]._family_predicate(presentation(modality="Liquid"))

    assert sql.count("?") == len(params)
    assert set(params) == {"Liquid", "Frozen Liquid"}


def test_the_predicate_falls_back_to_the_form(psa):
    sql, params = psa["risk"]._family_predicate(
        presentation(modality="Unknown", form="Gel")
    )

    assert sql == "p2.form = ?"
    assert params == ["Gel"]


def test_the_predicate_matches_everything_when_nothing_is_known(psa):
    """A subject with neither modality nor form should still get an assessment, against a
    wider universe, rather than no assessment at all."""
    sql, params = psa["risk"]._family_predicate(presentation(modality="", form=""))

    assert (sql, params) == ("1=1", [])


def test_the_family_label_reads_as_the_baselines_word_it(psa):
    """It goes into the report's narrative, so it has to be the assessors' phrase."""
    assert psa["risk"]._family_label(presentation(modality="Liquid")) == \
        "Liquid vial drug products"
    assert psa["risk"]._family_label(presentation(modality="Lyo Powder")) == \
        "Lyophilised powder vial products"


def test_an_unknown_family_gets_the_generic_label(psa):
    assert psa["risk"]._family_label(presentation(modality="", form="")) == \
        "Vial drug products"


# ── labelling a presentation for a human ──────────────────────────────────────


def test_a_presentation_is_labelled_by_its_program(psa):
    assert psa["risk"]._label(
        {"product_id": 1, "program_no": "ABBV-400", "program_name": "Etentamig"}
    ) == "ABBV-400 (Etentamig)"


def test_a_redundant_name_is_not_repeated(psa):
    """"ABBV-400 (ABBV-400)" is noise in a form a person reads."""
    assert psa["risk"]._label(
        {"product_id": 1, "program_no": "ABBV-400", "program_name": "ABBV-400"}
    ) == "ABBV-400"


@pytest.mark.parametrize("placeholder", ["N/A", "NA", ""])
def test_a_placeholder_program_number_falls_back_to_the_name(psa, placeholder):
    """The sheet uses these for products without a program code assigned yet."""
    assert psa["risk"]._label(
        {"product_id": 1, "program_no": placeholder, "program_name": "Etentamig"}
    ) == "Etentamig"


def test_a_presentation_with_no_names_falls_back_to_its_id(psa):
    """Never blank: an unlabelled row in a comparator table cannot be looked up."""
    assert psa["risk"]._label(
        {"product_id": 42, "program_no": "", "program_name": ""}
    ) == "product 42"


# ── the queries, against a real database ──────────────────────────────────────


def test_the_cap_colours_of_a_presentation_are_normalised(psa, con):
    """They are compared as a set, so they have to be normalised on the way out or 'Blue'
    and 'blue' would look like two different caps."""
    add_product(con, 1)
    add_cap(con, 1, name="Blue")

    assert psa["risk"]._cap_colors(con, 1) == {"BLUE"}


def test_a_cap_code_is_used_when_there_is_no_name(psa, con):
    """Half the sheet records the code and not the name."""
    add_product(con, 1)
    add_cap(con, 1, name="", code="6043")

    assert psa["risk"]._cap_colors(con, 1) == {"6043"}


def test_a_presentation_with_no_cap_has_an_empty_set(psa, con):
    """Which is the non-distinguishing case, not an error."""
    add_product(con, 1)

    assert psa["risk"]._cap_colors(con, 1) == set()


def test_several_caps_are_all_returned(psa, con):
    add_product(con, 1)
    add_cap(con, 1, name="Blue")
    add_cap(con, 1, name="Red")

    assert psa["risk"]._cap_colors(con, 1) == {"BLUE", "RED"}


def test_a_presentation_is_read_with_its_caps_attached(psa, con):
    """`_compare` reads `cap_colors` off the dict, so a presentation loaded without them
    would raise rather than compare."""
    add_product(con, 1, program_no="ABBV-400")
    add_cap(con, 1, name="Blue")

    subject = psa["risk"]._presentation(con, 1)

    assert subject["program_no"] == "ABBV-400"
    assert subject["cap_colors"] == {"BLUE"}


def test_an_unknown_product_id_reads_as_nothing(psa, con):
    assert psa["risk"]._presentation(con, 999) is None


def test_a_comparator_shares_a_site_with_the_subject(psa, con):
    """The whole basis of the assessment: products made in the same place are the ones that
    could be mixed up, so the comparator universe is site-driven."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="PRG-A")
    add_product(con, 2, program_no="PRG-B")
    link_site(con, 1, site)
    link_site(con, 2, site)

    assert psa["risk"]._comparators(con, psa["risk"]._presentation(con, 1)) == [2]


def test_a_product_at_another_site_is_not_a_comparator(psa, con):
    """It cannot be confused with the subject on a line it never appears on."""
    add_product(con, 1)
    add_product(con, 2)
    link_site(con, 1, add_site(con, 1, "SITE-A"))
    link_site(con, 2, add_site(con, 2, "SITE-B"))

    assert psa["risk"]._comparators(con, psa["risk"]._presentation(con, 1)) == []


def test_another_presentation_of_the_same_program_is_not_a_comparator(psa, con):
    """A program is not at risk of being mistaken for itself, and its own presentations would
    otherwise dominate the comparator list."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="ABBV-400")
    add_product(con, 2, program_no="ABBV-400")
    link_site(con, 1, site)
    link_site(con, 2, site)

    assert psa["risk"]._comparators(con, psa["risk"]._presentation(con, 1)) == []


def test_a_clinical_comparator_is_excluded_by_the_scope_rule(psa, con):
    """The 2026-07-17 decision, enforced in SQL. This is the test that proves the predicate
    works against real SQLite rather than merely reading correctly."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="PRG-A")
    add_product(con, 2, program_no="PRG-B", batch_type="Clinical")
    link_site(con, 1, site)
    link_site(con, 2, site)

    assert psa["risk"]._comparators(con, psa["risk"]._presentation(con, 1)) == []


def test_a_comparator_with_no_batch_type_is_kept(psa, con):
    """NULL is in scope — unknown is not clinical. This is the case a plain `NOT IN` would
    silently drop, because in SQL `NULL NOT IN (...)` is not true."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="PRG-A")
    add_product(con, 2, program_no="PRG-B", batch_type=None)
    link_site(con, 1, site)
    link_site(con, 2, site)

    assert psa["risk"]._comparators(con, psa["risk"]._presentation(con, 1)) == [2]


def test_the_shared_sites_are_reported_with_their_role(psa, con):
    """"Same site" means something different for manufacturing and for packaging, and the
    report says which."""
    site = add_site(con, 1, "AP01")
    add_product(con, 1)
    add_product(con, 2)
    link_site(con, 1, site, role="dp_mfr")
    link_site(con, 2, site, role="dp_mfr")

    assert psa["risk"]._shared_sites(con, 1, 2) == [("AP01", "dp_mfr")]


def test_sites_shared_in_different_roles_are_not_shared(psa, con):
    """Manufactured at a site that only packages the other product is not the same exposure."""
    site = add_site(con, 1, "AP01")
    add_product(con, 1)
    add_product(con, 2)
    link_site(con, 1, site, role="dp_mfr")
    link_site(con, 2, site, role="dp_pkging")

    assert psa["risk"]._shared_sites(con, 1, 2) == []


# ── the whole assessment ──────────────────────────────────────────────────────


def two_products_at_one_site(con, **comparator):
    site = add_site(con, 1, "AP01")
    add_product(con, 1, program_no="PRG-A", vial_container_size="2R")
    add_product(con, 2, program_no="PRG-B", vial_container_size="2R", **comparator)
    link_site(con, 1, site)
    link_site(con, 2, site)


def test_an_assessment_ranks_the_most_similar_comparator_first(psa, con):
    """An assessor reads the top of the list, so the ordering is the answer."""
    site = add_site(con, 1, "AP01")
    add_product(con, 1, program_no="PRG-A", vial_container_size="2R", shape="Vial")
    add_product(con, 2, program_no="PRG-B", vial_container_size="6R", shape="Cartridge")
    add_product(con, 3, program_no="PRG-C", vial_container_size="2R", shape="Vial")
    for pid in (1, 2, 3):
        link_site(con, pid, site)

    subject, results = psa["risk"]._assess_raw(con, 1)

    assert [r["comparator"]["product_id"] for r in results][0] == 3


def test_each_result_carries_its_own_risk_level(psa, con):
    two_products_at_one_site(con)

    _, results = psa["risk"]._assess_raw(con, 1)

    assert results[0]["risk_level"] in {"High", "Med", "Low"}
    assert results[0]["n_distinguishing"] == 0
    assert results[0]["risk_level"] == "High"


def test_a_result_records_the_shared_sites(psa, con):
    """Which is the evidence for why the pair was compared at all."""
    two_products_at_one_site(con)

    _, results = psa["risk"]._assess_raw(con, 1)

    assert results[0]["shared_sites"] == [("AP01", "dp_mfr")]


def test_assessing_an_unknown_product_says_so(psa, con):
    """A silent empty assessment would look like "no similar products", which is the
    opposite of "we could not find the product"."""
    with pytest.raises(ValueError, match="no product with product_id"):
        psa["risk"]._assess_raw(con, 999)


def test_a_product_with_no_comparators_assesses_cleanly(psa, con):
    """A first-of-its-kind product at a new site is a legitimate outcome, not a failure."""
    add_product(con, 1)

    subject, results = psa["risk"]._assess_raw(con, 1)

    assert subject["product_id"] == 1
    assert results == []


def test_only_the_closest_presentation_of_a_program_is_kept(psa, con):
    """A program with six presentations would otherwise fill the comparator table with six
    near-identical rows and push the genuinely different products off the list."""
    site = add_site(con, 1, "AP01")
    add_product(con, 1, program_no="PRG-A", vial_container_size="2R")
    add_product(con, 2, program_no="PRG-B", vial_container_size="2R")
    add_product(con, 3, program_no="PRG-B", vial_container_size="6R")
    for pid in (1, 2, 3):
        link_site(con, pid, site)

    _, results = psa["risk"]._assess_raw(con, 1)
    deduped = psa["risk"]._dedup_by_program(results)

    assert [r["comparator"]["program_no"] for r in deduped] == ["PRG-B"]
    assert deduped[0]["comparator"]["product_id"] == 2, "the closest one"
