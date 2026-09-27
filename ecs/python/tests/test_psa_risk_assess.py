"""The risk engine end to end: the assessment it publishes, the audit rows it writes, and the
Parts D / E draft it hands to `populate_template`.

`tests/test_psa_risk.py` deliberately stops at the pure comparators — `grade`, `_norm`,
`_compare`, the family grouping — against a hand-populated in-memory schema. Everything above
them is untested there, and it is the half that decides what an assessor actually reads:

  * `assess` splits the co-located universe into the set it ASSESSES (same dosage-form family,
    shared MANUFACTURING site) and the set it merely shows for context (`informational`). Getting
    that split backwards would put a lyophilised cake or a packaging-only neighbour into the
    report's Part E as an assessed mix-up comparator.
  * `_persist` is the audit trail. It has to be idempotent, because a run is repeatable and a
    second assessment of the same subject must replace the first rather than double it.
  * `form_fields` is the seam `populate_template` consumes (`d1_product_families`,
    `d1_similarity_risk`, `d2_comments`, `_e_sites`). It swallows every exception by design, so a
    key it silently stops producing would show up only as a blank box in a signed GxP form.

**Nothing here touches the network.** The catalogues are built by `tests/psa_fixtures.py`, which
loads a Smartsheet-shaped dict through the real ingest path — so the products these assertions
run against were created by the same code production uses. `rebuild()` below is the same trick
with a caller-chosen set of presentations, for the shapes the shared catalogue does not have
(a 0-difference clone, a discontinued product, a second manufacturing site).

Two stubs, both at a boundary the real code cannot be driven across from outside. `_comparators`
is monkeypatched to return an id that no longer resolves, which is the only way to reach
`_assess_raw`'s defensive skip — the real query joins `product`, so it cannot return a dangling
id. And `sys.stdout` is replaced by a `StringIO` (which has no `reconfigure`) to reach `main`'s
encoding guard, because a real console always has one.

Text is asserted on by substring, and the numbers (`n_distinguishing`, the grade) are asserted
exactly. The thresholds themselves are pinned in `test_psa_risk.py`; this file pins the sentences
and the rows built on top of them, because they are what a reviewer signs.
"""

from __future__ import annotations

import io
import sqlite3

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.psa_fixtures import (SUBJECT, catalogue, con, psa_config, row, sheet, snapshot_bytes,
                                subject_product_id)

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import paths, risk, snapshot

    return {"risk": risk, "paths": paths, "snapshot": snapshot}


@pytest.fixture
def rebuild(psa, psa_config):
    """Factory: build a catalogue of exactly the given presentations, and connect to it.

    Each presentation is the keyword set `psa_fixtures.row` takes, so a test states only the
    attribute it cares about. Products come out with `product_id` 1..n in the order given and
    `program_name` "Product <n>", which is what the labels below are built from.
    """
    made = []

    def build(*presentations):
        from da_silos.psa.ingest_smartsheet import COLS

        rows = [row(COLS, n, **spec) for n, spec in enumerate(presentations)]
        psa["snapshot"].rebuild(snapshot_bytes(sheet(rows)))
        connection = sqlite3.connect(psa["paths"].db_path())
        connection.row_factory = sqlite3.Row
        made.append(connection)
        return connection

    yield build
    for connection in made:
        connection.close()


def labels(slims):
    return [s["label"] for s in slims]


def programs(con):
    """program_no by product_id, for reading the persisted rows back in human terms."""
    return {r[0]: r[1] for r in con.execute("SELECT product_id, program_no FROM product")}


# ── the published assessment ──────────────────────────────────────────────────


def test_the_headline_risk_is_driven_by_the_most_similar_comparator(psa, con):
    """The overall grade is what the UI shows first. Driving it from anything but the closest
    product would let one clearly-different neighbour dilute a genuine look-alike pair."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)

    assert res["overall_risk"] == "High"
    assert res["closest"]["label"] == "ABBV-400 (Product 1)"
    assert res["closest"]["n_distinguishing"] == 1
    assert res["comparators"][0] == res["closest"], "the list is sorted most-similar first"


def test_the_assessed_set_is_same_family_products_made_at_a_shared_site(psa, con):
    """This is the scoping the report's Part D/E uses. A comparator that leaks into it becomes an
    assessed mix-up risk in a signed document; one that is wrongly dropped hides a real one."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)

    assert labels(res["comparators"]) == ["ABBV-400 (Product 1)", "ABBV-401 (Product 2)"]
    assert res["n_comparators"] == 2
    joined = " ".join(labels(res["comparators"]))
    assert "ABBV-403" not in joined, "clinical is out of scope (2026-07-17 decision)"
    assert "ABBV-402" not in joined, "made at another site"
    assert "ABBV-404" not in joined, "a lyophilised powder, not a liquid vial"


def test_a_different_dosage_form_family_is_shown_but_not_assessed(psa, con):
    """A lyophilised cake at the same site is worth seeing and is not a mix-up comparator. If it
    were assessed it would grade High here on a single difference, and that would be wrong."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)

    lyo = next(s for s in res["informational"] if s["label"] == "ABBV-404 (Product 5)")
    assert lyo["n_distinguishing"] == 1, "near-identical on appearance — and still not comparable"
    assert lyo["label"] not in labels(res["comparators"])
    assert "not assessed, not in the report" in res["note"]


def test_the_result_names_the_presentation_that_was_assessed(psa, con):
    """A run is per-presentation, so the record has to say which one — a program with six rows
    would otherwise be impossible to reconcile with the document."""
    pid = subject_product_id(con)

    res = psa["risk"].assess(con, pid, persist=False)

    assert res["subject"] == {"product_id": pid, "program_no": SUBJECT,
                              "program_name": "Product 0", "batch_type": "Commercial",
                              "label": f"{SUBJECT} (Product 0)"}


def test_a_comparator_reports_what_differed_and_what_matched(psa, con):
    """Both halves are the evidence. An assessor reading only "1 difference" cannot tell whether
    the shared attributes were the ones that matter."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)

    closest = res["closest"]
    assert closest["distinguishing"] == ["Cap colour"]
    assert closest["shared_attributes"] == ["Form", "Vial / container size", "Fill volume",
                                            "Product colour", "Shape", "Marking"]
    assert closest["shared_sites"] == ["AP16 (mfr)", "AP16 (pkging)"]
    assert closest["risk_level"] == "High"
    assert [a["attribute"] for a in closest["attributes"]][-1] == "Cap colour", \
        "the full comparison travels with the summary"


def test_no_closest_comparator_slims_to_nothing_rather_than_an_empty_dict(psa):
    """The UI tests `closest` for truthiness; an empty dict would render a blank comparator card
    instead of the "no comparator found" message."""
    assert psa["risk"]._slim(None) is None


def test_only_the_closest_presentation_of_a_program_is_reported(psa, rebuild):
    """A program with several presentations would otherwise fill the comparator list with rows
    describing the same product and push the genuinely different neighbours off it."""
    con = rebuild({"program": SUBJECT},
                  {"program": "ABBV-400", "cap": "Red 6070"},
                  {"program": "ABBV-400", "vial": "6R", "shape": "Cartridge"})

    res = psa["risk"].assess(con, 1, persist=False)

    assert labels(res["comparators"]) == ["ABBV-400 (Product 1)"]
    assert res["comparators"][0]["n_distinguishing"] == 1, "the more similar of the two"
    assert res["n_comparators"] == 1


def test_a_program_already_assessed_is_not_repeated_as_informational(psa, rebuild):
    """Otherwise the same program appears twice in the UI — once assessed and once "for
    context" — which reads as two different products at the same site."""
    con = rebuild({"program": SUBJECT},
                  {"program": "ABBV-400", "cap": "Red 6070"},
                  {"program": "ABBV-400", "vial": "6R", "shape": "Cartridge"})
    con.execute("DELETE FROM product_site WHERE product_id=3 AND role='mfr'")
    con.commit()

    res = psa["risk"].assess(con, 1, persist=False)

    assert labels(res["comparators"]) == ["ABBV-400 (Product 1)"]
    assert res["informational"] == []


def test_comparators_without_a_program_number_are_still_listed_separately(psa, rebuild):
    """The sheet carries rows for products with no code assigned yet. Collapsing them all under
    one blank key would report a single comparator where there are two."""
    con = rebuild({"program": SUBJECT}, {"program": "", "vial": "6R"}, {"program": "", "vial": "10R"})

    res = psa["risk"].assess(con, 1, persist=False)

    assert labels(res["comparators"]) == ["Product 1", "Product 2"]


# ── when there is nothing to compare against ──────────────────────────────────


def test_a_product_alone_at_its_site_grades_as_none(psa, con):
    """"None" is a legitimate outcome and a different statement from "Low" — no comparator was
    found, rather than comparators were found and ruled out."""
    res = psa["risk"].assess(con, subject_product_id(con, "ABBV-402"), persist=False)

    assert res["overall_risk"] == "None"
    assert (res["closest"], res["comparators"], res["n_comparators"]) == (None, [], 0)
    assert "shares no manufacturing site" in res["rationale"]


def test_packaging_only_co_location_is_never_assessed(psa, rebuild):
    """Part E assesses manufacturing co-location (2026-07-23 decision). A product only packaged
    alongside the subject is context, and must not produce an assessed High in the report."""
    con = rebuild({"program": SUBJECT}, {"program": "ABBV-400", "cap": "Red 6070"})
    con.execute("DELETE FROM product_site WHERE product_id=2 AND role='mfr'")
    con.commit()

    res = psa["risk"].assess(con, 1, persist=True)

    assert res["overall_risk"] == "None"
    assert res["informational"][0]["shared_sites"] == ["AP16 (pkging)"]
    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == 0, \
        "nothing was assessed, so nothing is audited"


def test_the_rationale_calls_out_a_product_with_no_differentiator_at_all(psa, rebuild):
    """The 0-difference case is the one the form exists for, so it gets its own sentence rather
    than "distinguished by 0 attribute(s)"."""
    con = rebuild({"program": SUBJECT}, {"program": "ABBV-502"})

    res = psa["risk"].assess(con, 1, persist=False)

    assert res["overall_risk"] == "High"
    assert "matches" in res["rationale"] and "every compared attribute" in res["rationale"]
    assert "no visual differentiator found" in res["rationale"]


def test_the_rationale_names_the_attributes_that_told_them_apart(psa, con):
    """An assessor has to be able to check the engine's reasoning against the vials in front of
    them, which means the rationale names the differentiators, not just how many there were."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)

    assert "ABBV-400 (Product 1)" in res["rationale"]
    assert "AP16 (mfr)" in res["rationale"]
    assert "1 attribute(s): cap colour" in res["rationale"]
    assert "High mix-up risk" in res["rationale"]
    assert "2 in-scope co-located comparator(s) assessed" in res["rationale"]


# ── the audit trail ───────────────────────────────────────────────────────────


def test_the_assessed_comparators_are_written_to_the_risk_tables(psa, con):
    """`risk_assessment` is the record that the analysis happened and what it concluded. A
    result shown in the UI but never persisted cannot be reconciled with the signed document."""
    pid = subject_product_id(con)

    psa["risk"].assess(con, pid, persist=True)

    by_id = programs(con)
    rows = con.execute("SELECT * FROM risk_assessment ORDER BY num_distinguishing_diffs").fetchall()
    assert [by_id[r["comparator_product_id"]] for r in rows] == ["ABBV-400", "ABBV-401"]
    assert [r["subject_product_id"] for r in rows] == [pid, pid]
    assert [(r["num_distinguishing_diffs"], r["risk_level"]) for r in rows] == \
        [(1, "High"), (4, "Low")]
    assert {r["status"] for r in rows} == {"auto-provisional"}, "not a human assessment"
    assert all(r["assessed_date"] for r in rows)
    site = con.execute("SELECT site_code FROM site WHERE site_id=?", (rows[0]["site_id"],)).fetchone()
    assert site["site_code"] == "AP16", "the shared site is the reason the pair was compared"


def test_the_persisted_rationale_is_the_one_the_ui_shows(psa, con):
    """So a reviewer opening the database a year later reads the same explanation the assessor
    read on the day."""
    psa["risk"].assess(con, subject_product_id(con), persist=True)

    rationale = con.execute("SELECT rationale FROM risk_assessment "
                            "ORDER BY num_distinguishing_diffs").fetchone()["rationale"]
    assert "ABBV-400 (Product 1)" in rationale
    assert "1 attribute(s): cap colour" in rationale


def test_every_compared_attribute_is_persisted_not_only_the_differing_ones(psa, con):
    """"We compared shape and it matched" is evidence. Storing only the differences would make
    an unrecorded attribute indistinguishable from one that was checked and agreed."""
    psa["risk"].assess(con, subject_product_id(con), persist=True)

    aid = con.execute("SELECT assessment_id FROM risk_assessment "
                      "ORDER BY num_distinguishing_diffs").fetchone()["assessment_id"]
    rows = con.execute("SELECT * FROM risk_attribute_cmp WHERE assessment_id=?", (aid,)).fetchall()
    assert [r["attribute"] for r in rows] == \
        ["Form", "Vial / container size", "Fill volume", "Product colour", "Shape", "Marking",
         "Cap colour"]
    cap = rows[-1]
    assert (cap["is_distinguishing"], cap["weight"]) == (1, 1.0)
    assert (cap["subject_value"], cap["comparator_value"]) == ("BLUE6043", "RED6070")
    assert con.execute("SELECT count(*) FROM risk_attribute_cmp "
                       "WHERE assessment_id=? AND is_distinguishing=0", (aid,)).fetchone()[0] == 6


def test_re_assessing_a_subject_replaces_its_rows_rather_than_appending(psa, con):
    """A run is repeatable. Appending would leave two contradictory assessments of the same
    presentation in the audit table with nothing to say which is current."""
    pid = subject_product_id(con)
    psa["risk"].assess(con, pid, persist=True)
    first = con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0]

    psa["risk"].assess(con, pid, persist=True)

    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == first == 2
    assert con.execute("SELECT count(*) FROM risk_attribute_cmp").fetchone()[0] == 14
    assert con.execute("SELECT count(*) FROM risk_attribute_cmp c LEFT JOIN risk_assessment a "
                       "ON a.assessment_id=c.assessment_id WHERE a.assessment_id IS NULL"
                       ).fetchone()[0] == 0, "no orphaned comparison rows"


def test_an_unpersisted_assessment_leaves_the_tables_untouched(psa, con):
    """The UI previews an assessment on every page load; those must not write GxP audit rows."""
    psa["risk"].assess(con, subject_product_id(con), persist=False)

    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM risk_attribute_cmp").fetchone()[0] == 0


def test_every_assessed_presentation_is_audited_even_though_one_is_reported(psa, rebuild):
    """The UI collapses a program to its closest presentation; the audit trail must not, or the
    record would not show that the other presentations were compared at all."""
    con = rebuild({"program": SUBJECT},
                  {"program": "ABBV-400", "cap": "Red 6070"},
                  {"program": "ABBV-400", "vial": "6R", "shape": "Cartridge"})

    res = psa["risk"].assess(con, 1, persist=True)

    assert res["n_comparators"] == 1
    assert [tuple(r) for r in con.execute(
        "SELECT comparator_product_id, num_distinguishing_diffs FROM risk_assessment "
        "ORDER BY comparator_product_id")] == [(2, 1), (3, 2)]


# ── assessing by program number ───────────────────────────────────────────────


def test_a_program_can_be_assessed_without_the_caller_opening_the_catalogue(psa, con, psa_config):
    """`assess_program` is what the router and the CLI call. It owns the connection, so a
    connection it failed to close would hold a lock on psa.db for the rest of the process."""
    res = psa["risk"].assess_program(SUBJECT, db=psa["paths"].db_path())

    assert res["subject"]["program_no"] == SUBJECT
    assert res["overall_risk"] == "High"
    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == 2, "persists"


def test_assessing_by_program_can_skip_persisting(psa, con, psa_config):
    """The preview path again, one level up."""
    res = psa["risk"].assess_program(SUBJECT, db=psa["paths"].db_path(), persist=False)

    assert res["n_comparators"] == 2
    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == 0


def test_an_explicit_presentation_wins_over_the_program_lookup(psa, con):
    """A program has several presentations and the caller has already chosen one; re-resolving
    from the program number would silently assess a different row."""
    assert psa["risk"]._resolve_pid(con, SUBJECT, 42) == 42, "not even looked up"


def test_the_first_sheet_row_of_a_program_is_the_default_presentation(psa, rebuild):
    """Deterministic, and traceable to the Smartsheet — `product_id` is not stable across
    rebuilds, so ordering by it instead would make the default drift."""
    con = rebuild({"program": SUBJECT, "cap": "Red 6070"}, {"program": SUBJECT})
    first = con.execute("SELECT product_id FROM product ORDER BY source_row").fetchone()[0]

    assert psa["risk"]._resolve_pid(con, SUBJECT, None) == first


def test_an_unknown_program_is_an_error_rather_than_an_empty_assessment(psa, con):
    """An empty assessment reads as "no similar products found", which is the opposite of "we
    could not find the product you asked about"."""
    with pytest.raises(ValueError, match="no product with program_no=NOPE"):
        psa["risk"]._resolve_pid(con, "NOPE", None)


def test_assessing_an_unknown_presentation_says_so(psa, con):
    """Same distinction one layer down, where `assess` is given a stale product_id."""
    with pytest.raises(ValueError, match="no product with product_id=999999"):
        psa["risk"].assess(con, 999999, persist=False)


# ── Part D: the late-stage pipeline draft ─────────────────────────────────────


def test_part_d_compares_against_the_same_family_at_every_site(psa, con, psa_config):
    """Part D is a pipeline-wide question, so unlike Part E it is not site-scoped — a product
    made elsewhere still has to appear, or the families cell understates the pipeline."""
    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    families = fields["d1_product_families"]
    assert families.startswith("Liquid vial drug products in late-stage development: ")
    assert "ABBV-402 (Product 3)" in families, "another site, still a pipeline comparator"
    assert "ABBV-403" not in families, "clinical is out of scope"
    assert "ABBV-404" not in families, "a different dosage-form family"


def test_a_discontinued_product_is_not_a_late_stage_pipeline_comparator(psa, rebuild):
    """Part D's universe is the live pipeline. A discontinued product is still in scope for the
    site question, though — it may sit on the same line — so the two parts must disagree here."""
    con = rebuild({"program": SUBJECT}, {"program": "ABBV-600", "status": "Discontinued"})

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d1_product_families"] == \
        "Liquid vial drug products in late-stage development — none identified in the catalogue."
    assert fields["d1_similarity_risk"] == "No"
    assert "ABBV-600 (Product 1)" in fields["_e_sites"][0]["families"], "in scope at the site"
    assert fields["_e_sites"][0]["risk"] == "Yes"


@pytest.mark.parametrize("comparator,diffs,level,box", [
    ({"program": "ABBV-502"}, 0, "High", "Yes"),
    ({"program": "ABBV-500", "cap": "Red 6070"}, 1, "High", "Yes"),
    ({"program": "ABBV-500", "vial": "6R", "shape": "Cartridge"}, 2, "Med", "No"),
    ({"program": "ABBV-401", "vial": "10R", "cap": "White 6003", "colour": "Yellow",
      "shape": "Cartridge"}, 4, "Low", "No"),
])
def test_the_draft_similarity_risk_box_follows_the_grade(psa, rebuild, comparator, diffs, level, box):
    """This box is the answer a reviewer signs. Baseline-calibrated: Yes only for near-identical
    products, so a 2-difference pair reads No — and says so in words as well as in the box."""
    rebuild({"program": SUBJECT}, comparator)

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d1_similarity_risk"] == box
    assert f"Provisional mix-up risk: {level} → Similarity Risk = {box}." in fields["d2_comments"]
    if diffs:
        assert f"is distinguished by {diffs} attribute(s)" in fields["d2_comments"]
    else:
        assert "matches on every compared appearance attribute" in fields["d2_comments"]


@pytest.mark.parametrize("comparator,expected", [
    ({"program": "ABBV-502"}, "Recommend SME review of appearance / labelling / handling controls."),
    ({"program": "ABBV-500", "vial": "6R", "shape": "Cartridge"}, "Borderline: the closest product"),
])
def test_the_draft_tells_the_sme_what_to_do_next(psa, rebuild, comparator, expected):
    """A High gets a review instruction; a borderline Med gets an explicit "confirm this is
    enough differentiation", which is the case the box alone would understate."""
    rebuild({"program": SUBJECT}, comparator)

    assert expected in psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)["d2_comments"]


def test_a_low_risk_draft_asks_for_nothing_further(psa, rebuild):
    """Three clear differentiators is the case the assessors treat as closed, so adding an
    action would send every low-risk assessment to an SME queue."""
    rebuild({"program": SUBJECT},
            {"program": "ABBV-401", "vial": "10R", "cap": "White 6003", "colour": "Yellow",
             "shape": "Cartridge"})

    comments = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)["d2_comments"]

    assert "Recommend SME review" not in comments
    assert "Borderline" not in comments


def test_every_drafted_paragraph_is_tagged_as_auto_generated(psa, con, psa_config):
    """It goes into a document a human signs. Text that cannot be told from the analyst's own
    writing is the failure mode this tag exists to prevent."""
    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d2_comments"].startswith(psa["risk"].DRAFT_TAG)
    assert "SME to review and finalise" in psa["risk"].DRAFT_TAG
    for site in fields["_e_sites"]:
        assert site["comments"].startswith(psa["risk"].DRAFT_TAG)


def test_the_draft_counts_programs_rather_than_presentations(psa, rebuild):
    """"Compared against 2 products" when both rows are the same program overstates the
    breadth of the comparison to a reader who cannot see the presentation grain."""
    rebuild({"program": SUBJECT},
            {"program": "ABBV-400", "cap": "Red 6070"},
            {"program": "ABBV-400", "vial": "6R", "shape": "Cartridge"})

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert "compared against 1 in-scope late-stage pipeline products" in fields["d2_comments"]
    assert fields["d1_product_families"].endswith("ABBV-400 (Product 1).")


@pytest.mark.parametrize("modality,form,expected", [
    ("Liquid", "Liquid", "Liquid vial drug products"),
    ("Lyo Powder", "Lyophilised powder", "Lyophilised powder vial products"),
])
def test_the_families_cell_uses_the_assessors_own_phrase(psa, rebuild, modality, form, expected):
    """It is lifted from the signed baselines; a paraphrase would not match the wording the
    reviewers are used to reading in Part D.1."""
    rebuild({"program": SUBJECT, "modality": modality, "form": form})

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d1_product_families"].startswith(f"{expected} in late-stage development")


def test_an_empty_comparator_set_says_so_rather_than_going_blank(psa, rebuild):
    """A blank cell in a GxP form is indistinguishable from an unfinished one; "none identified"
    is a statement the assessor made."""
    rebuild({"program": SUBJECT})

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d1_product_families"].endswith("— none identified in the catalogue.")
    assert "no mix-up risk identified" in fields["d2_comments"]
    assert "No in-scope late-stage pipeline products were found" in fields["d2_comments"]
    assert fields["d1_similarity_risk"] == "No"


# ── Part E: one block per manufacturing site ──────────────────────────────────


def test_the_draft_returns_exactly_the_keys_populate_template_consumes(psa, con, psa_config):
    """`populate_template` reads these by name and falls back silently when one is missing, so a
    renamed key becomes a blank box in the report rather than an error."""
    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert set(fields) == {"d1_product_families", "d1_similarity_risk", "d2_comments", "_e_sites"}
    assert set(fields["_e_sites"][0]) == {"site", "families", "risk", "comments", "qa_label"}


def test_part_e_gets_one_block_per_manufacturing_site(psa, rebuild):
    """The form has a Part E per site, each signed by that site's QA. One merged block would
    ask a single QA group to sign for a product line they do not own."""
    con = rebuild({"program": SUBJECT, "site": "AP16, LU"},
                  {"program": "ABBV-400", "cap": "Red 6070"},
                  {"program": "ABBV-405", "site": "LU", "vial": "6R"})

    sites = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)["_e_sites"]

    assert [s["site"] for s in sites] == ["AP16 — Manufacturing", "LU — Manufacturing"]
    assert [s["qa_label"] for s in sites] == ["Site QA - AP16", "Site QA - LU"]
    assert "ABBV-400 (Product 1)" in sites[0]["families"]
    assert "ABBV-405 (Product 2)" in sites[1]["families"], "each block sees only its own site"
    assert "ABBV-405" not in sites[0]["families"]


def test_a_site_with_a_name_is_shown_with_its_code_as_well(psa, rebuild):
    """The code is what appears on the batch record and the name is what a reader recognises,
    so Part E needs both — and must not print "AP16 (AP16)" when they are the same."""
    con = rebuild({"program": SUBJECT, "site": "AP16, LU"}, {"program": "ABBV-400", "cap": "Red 6070"})
    con.execute("UPDATE site SET site_name='Barceloneta' WHERE site_code='LU'")
    con.commit()

    sites = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)["_e_sites"]

    assert [s["site"] for s in sites] == ["AP16 — Manufacturing", "Barceloneta (LU) — Manufacturing"]


def test_part_e_ignores_a_neighbour_that_is_only_packaged_alongside(psa, rebuild):
    """The preview shows packaging co-location for context; Part E is manufacturing-only
    (2026-07-23 / QPP §3). Writing it into the report would assess a risk nobody agreed to."""
    con = rebuild({"program": SUBJECT}, {"program": "ABBV-400", "cap": "Red 6070"})
    con.execute("DELETE FROM product_site WHERE product_id=2 AND role='mfr'")
    con.commit()

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["_e_sites"][0]["families"].endswith("— none identified in the catalogue.")
    assert fields["_e_sites"][0]["risk"] == "No"
    assert "No in-scope products at AP16 were found" in fields["_e_sites"][0]["comments"]


def test_a_subject_with_no_manufacturing_site_gets_no_part_e_block(psa, rebuild):
    """Part E is per site, so with no site there is nothing to render — and
    `populate_template` substitutes its own TBD block rather than inventing a site here."""
    con = rebuild({"program": SUBJECT}, {"program": "ABBV-400", "cap": "Red 6070"})
    con.execute("DELETE FROM product_site WHERE product_id=1 AND role='mfr'")
    con.commit()

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["_e_sites"] == []
    assert fields["d1_similarity_risk"] == "Yes", "Part D is not site-scoped, so it still answers"


def test_the_site_narrative_names_the_site_it_is_about(psa, con, psa_config):
    """Each block is signed separately, so a paragraph that could belong to either site is a
    document-control problem as much as a reporting one."""
    site = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)["_e_sites"][0]

    assert "compared against 2 in-scope products at AP16" in site["comments"]
    assert site["families"].startswith("Liquid vial drug products at AP16: ")
    assert "ABBV-400 (Product 1)" in site["families"]
    assert "ABBV-404" not in site["families"], "same site, other family — not a Part E comparator"


@pytest.mark.parametrize("kwargs", [
    {"program_no": SUBJECT, "product_id": 999999},
    {"program_no": "NO-SUCH-PROGRAM"},
])
def test_the_draft_degrades_to_nothing_rather_than_failing_the_report(psa, con, psa_config, kwargs):
    """D/E are one section of a document that is useful without them. `form_fields` returning {}
    lets the rest of the report generate and fall back to the blank form."""
    assert psa["risk"].form_fields(psa["paths"].db_path(), **kwargs) == {}


def test_an_unreadable_catalogue_does_not_fail_the_report_either(psa, psa_config, tmp_path):
    """Same contract at the connection boundary, which is where a missing directory shows up."""
    assert psa["risk"].form_fields(str(tmp_path / "gone" / "psa.db"), SUBJECT) == {}


# ── a dosage form the family table does not know ──────────────────────────────


def test_an_unrecognised_modality_scopes_the_assessment_by_the_form_text(psa, rebuild):
    """`FAMILY` lists the modalities seen in the sheet today. A new one must not collapse the
    comparator universe to "everything co-located" — it falls back to matching the `form` text,
    so a gel is still assessed against gels and merely shown alongside a solution."""
    con = rebuild({"program": SUBJECT, "modality": "Gel", "form": "Gel"},
                  {"program": "ABBV-400", "modality": "Gel", "form": "Gel", "cap": "Red 6070"},
                  {"program": "ABBV-401", "modality": "Gel", "form": "Solution", "cap": "Red 6070"})

    res = psa["risk"].assess(con, 1, persist=False)

    assert labels(res["comparators"]) == ["ABBV-400 (Product 1)"], "same form text"
    assert labels(res["informational"]) == ["ABBV-401 (Product 2)"], "same site, other form"
    assert res["overall_risk"] == "High"


def test_an_unrecognised_form_is_drafted_under_the_generic_family_name(psa, rebuild):
    """The families cell names the family it compared within, so a form with no agreed phrase has
    to read as the generic one rather than silently claiming to be a liquid vial product."""
    rebuild({"program": SUBJECT, "modality": "Gel", "form": "Gel"},
            {"program": "ABBV-400", "modality": "Gel", "form": "Gel", "cap": "Red 6070"})

    fields = psa["risk"].form_fields(psa["paths"].db_path(), SUBJECT)

    assert fields["d1_product_families"] == \
        "Vial drug products in late-stage development: ABBV-400 (Product 1)."
    assert fields["_e_sites"][0]["families"] == "Vial drug products at AP16: ABBV-400 (Product 1)."


def test_a_subject_with_neither_modality_nor_form_is_compared_against_everything(psa, rebuild):
    """A half-filled sheet row must still get an assessment. With no family to scope by, the
    engine widens the universe instead of narrowing it to nothing — so even a lyophilised powder
    becomes an assessed comparator here, which is the conservative direction."""
    con = rebuild({"program": SUBJECT, "modality": "", "form": ""},
                  {"program": "ABBV-404", "modality": "Lyo Powder",
                   "form": "Lyophilised powder", "cap": "Red 6070"})
    subject = psa["risk"]._presentation(con, 1)
    assert psa["risk"]._family_predicate(subject) == ("1=1", []), "no filter to apply"

    res = psa["risk"].assess(con, 1, persist=False)

    assert labels(res["comparators"]) == ["ABBV-404 (Product 1)"]
    assert res["informational"] == [], "nothing was excluded from the assessment"
    assert res["overall_risk"] == "High"


# ── the command-line view ─────────────────────────────────────────────────────


def test_the_printed_summary_leads_with_the_subject_and_the_grade(psa, con, capsys):
    """It is the operator-facing view of the same analysis; a summary that omitted the grade
    would need the full comparator table read to answer the only question asked of it."""
    res = psa["risk"].assess(con, subject_product_id(con), persist=False)
    capsys.readouterr()

    psa["risk"]._print(res)

    out = capsys.readouterr().out
    assert f"Similarity / mix-up risk: {SUBJECT} (Product 0)" in out
    assert "batch_type=Commercial" in out
    assert "Overall risk: High   |   2 in-scope co-located comparator(s)" in out
    assert "[High] ABBV-400 (Product 1)" in out
    assert "distinguished by: Cap colour" in out


def test_the_printed_comparator_list_is_capped(psa, rebuild, capsys):
    """A site with fifty co-located products would otherwise bury the summary — and the ones
    that matter are at the top, because the list is sorted most-similar first."""
    specs = [{"program": SUBJECT}] + [{"program": f"ABBV-{600 + n}", "vial": f"{n}R"}
                                      for n in range(9)]
    con = rebuild(*specs)
    res = psa["risk"].assess(con, 1, persist=False)
    assert res["n_comparators"] == 9
    capsys.readouterr()

    psa["risk"]._print(res)

    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln.startswith("  [")]) == 8


def test_the_entry_point_assesses_the_program_it_is_given(psa, con, psa_config, monkeypatch, capsys):
    """The CLI is how the engine is run by hand during calibration, so its argument has to be
    the program actually assessed rather than the hard-coded default."""
    monkeypatch.setattr("sys.argv", ["risk.py", "ABBV-401"])
    capsys.readouterr()

    assert psa["risk"].main() == 0

    out = capsys.readouterr().out
    assert "ABBV-401 (Product 2)" in out
    assert f"[ Low] {SUBJECT} (Product 0)" in out, "the subject is the comparator now"
    assert con.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] > 0, "it persists"


def test_the_entry_point_defaults_to_the_reference_program(psa, con, psa_config, monkeypatch, capsys):
    """Run with no arguments it assesses the program the extractors were tuned against, which is
    what makes `python -m ... risk` a usable smoke test."""
    monkeypatch.setattr("sys.argv", ["risk.py"])
    capsys.readouterr()

    assert psa["risk"].main() == 0

    assert f"Similarity / mix-up risk: {SUBJECT} (Product 0)" in capsys.readouterr().out


def test_the_entry_point_still_reports_on_a_stream_it_cannot_set_to_utf8(psa, con, psa_config,
                                                                        monkeypatch):
    """`main` widens stdout to UTF-8 because the rationale contains "⇒". A captured or wrapped
    stream has no `reconfigure`, and failing to widen it must not stop the report being printed —
    the assessment is the point, the encoding is a convenience."""
    captured = io.StringIO()
    assert not hasattr(captured, "reconfigure"), "the condition this test exists for"
    monkeypatch.setattr("sys.argv", ["risk.py", SUBJECT])
    monkeypatch.setattr("sys.stdout", captured)

    assert psa["risk"].main() == 0

    out = captured.getvalue()
    assert f"Similarity / mix-up risk: {SUBJECT} (Product 0)" in out
    assert "Overall risk: High" in out


# ── the defensive skips ───────────────────────────────────────────────────────


def test_a_comparator_id_that_no_longer_resolves_is_skipped(psa, con, monkeypatch):
    """`_comparators` is stubbed here to hand back a dangling id — the real query joins
    `product`, so it cannot. The point is that one unreadable row must not abandon the whole
    assessment: the remaining comparators still have to be reported."""
    real = subject_product_id(con, "ABBV-400")
    monkeypatch.setattr(psa["risk"], "_comparators", lambda con, subject: [real, 999999])

    _subject, results = psa["risk"]._assess_raw(con, subject_product_id(con))

    assert [r["comparator"]["product_id"] for r in results] == [real]


def test_comparing_against_an_id_that_is_not_a_presentation_skips_it(psa, con):
    """`_assess_against` takes an explicit id list from the Part D / Part E queries, so the same
    rule applies one layer up: drop the row, keep the assessment."""
    subject = psa["risk"]._presentation(con, subject_product_id(con))

    results = psa["risk"]._assess_against(con, subject, [subject_product_id(con, "ABBV-400"), 424242])

    assert [r["comparator"]["program_no"] for r in results] == ["ABBV-400"]
    assert results[0]["risk_level"] == "High"
