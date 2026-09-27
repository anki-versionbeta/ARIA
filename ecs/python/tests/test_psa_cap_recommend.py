"""Cap-colour recommendation: which colours are still free, and which are risky.

The screen's instant preview. Given one presentation, it grades every cap colour already in
use at the same manufacturing site for mix-up risk, then ranks the unused palette colours by
how far they sit from those.

Two rules run through the whole module, and both are honesty rules rather than accuracy ones:

  * **`vendor=ALL_VENDORS` (None) means every supplier, and that is the default.** It used to
    default to "Datwyler", which silently restricted a job to one catalogue — found on the first
    real execution, where the checkpoint read `vendors_considered: ['Datwyler']` while the message
    displayed said "across every supplier". At assessment time the cap is often not yet tooled, so
    "no supplier named" means the supplier is still open.

  * **A comparison that did not happen is never implied.** A subject with no cap, or a cap whose
    text does not resolve to a palette swatch ('TBD', a Pantone reference), has no shade to
    compare — so `delta_e_to_subject` stays None, the grade rests on vial size alone, and
    `risk_reason` says so in words. Reporting ΔE 0 there would read as a perfect colour match.

`_vial_key` and `_state_key` are the normalisers that decide which products are compared at all,
and they are pure. The graders take a list of dicts, so they are tested directly rather than
through a database. `recommend()` itself gets a real SQLite database built from the shipped DDL.
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
    from da_silos.psa import asset_store, cap_queries, cap_recommend

    return {"rec": cap_recommend, "queries": cap_queries, "asset_store": asset_store}


@pytest.fixture
def con(psa):
    """An empty PSA database from the shipped DDL, with the two seal vendors present.

    `cap_color.vendor_id` is a real foreign key and `_VENDOR_BY_ID` maps 1 -> Datwyler and
    2/3 -> West, so the vendor rows have to exist before any cap can be inserted.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(psa["asset_store"].read_text("schema.sql"))
    # `vendor_name` is UNIQUE, so 3 (also West in `_VENDOR_BY_ID`) is named for its
    # Smartsheet spelling rather than inserted twice.
    connection.executemany(
        "INSERT INTO vendor (vendor_id, vendor_name) VALUES (?, ?)",
        [(1, "Datwyler"), (2, "West"), (3, "West Pharmaceutical")],
    )
    connection.commit()
    yield connection
    connection.close()


def add_product(con, product_id, **columns):
    row = {"product_id": product_id, "program_no": f"PRG-{product_id}",
           "program_name": f"Program {product_id}", "batch_type": "Commercial",
           "status": "Commercial", "modality": "Liquid", "form": "Liquid",
           "vial_container_size": "2R", **columns}
    keys = ", ".join(row)
    con.execute(f"INSERT INTO product ({keys}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))
    con.commit()
    return product_id


def add_site(con, site_id, code="AP01"):
    con.execute("INSERT INTO site (site_id, site_code) VALUES (?, ?)", (site_id, code))
    con.commit()
    return site_id


def link_site(con, product_id, site_id, role="mfr"):
    """`role` is 'mfr' or 'pkging' (schema.sql:74). Cap co-location is assessed on 'mfr'
    only — two products packed in the same place but made elsewhere never meet on a line."""
    con.execute("INSERT INTO product_site (product_id, site_id, role) VALUES (?, ?, ?)",
                (product_id, site_id, role))
    con.commit()


def add_cap(con, product_id, name="", code="", vendor_id=1):
    con.execute(
        "INSERT INTO cap_color (product_id, cap_color_name, color_code, vendor_id) "
        "VALUES (?, ?, ?, ?)", (product_id, name, code, vendor_id),
    )
    con.commit()


# ── the vendor default, which was a real bug ───────────────────────────────────


def test_all_vendors_is_the_documented_default(psa):
    """`ALL_VENDORS is None`, and it is what every entry point defaults to. A supplier name
    here would restrict a whole job to one catalogue without saying so."""
    assert psa["rec"].ALL_VENDORS is None
    assert psa["rec"].recommend.__defaults__[0] is psa["rec"].ALL_VENDORS
    assert psa["rec"].recommend_program.__defaults__[0] is psa["rec"].ALL_VENDORS
    assert psa["rec"].recommend_program_presentations.__defaults__[0] is \
        psa["rec"].ALL_VENDORS


def test_the_grid_default_vendor_is_separate_from_the_ranking_default(psa):
    """The swatch GRID shows one supplier's catalogue because a grid has to pick one; the
    RANKING spans every supplier. Collapsing the two is what caused the original bug."""
    assert psa["rec"].GRID_DEFAULT_VENDOR == "Datwyler"
    assert psa["rec"].GRID_DEFAULT_VENDOR != psa["rec"].ALL_VENDORS


# ── the vial-size normaliser ───────────────────────────────────────────────────


@pytest.mark.parametrize("written,key", [
    ("2R", "2R"), ("2R (2.00 mL) vial", "2R"), ("  2r  ", "2R"), ("10R", "10R"),
    ("20 R", "20R"),
])
def test_an_r_size_is_recognised_however_it_is_written(psa, written, key):
    """The sheet writes the same vial six ways. Two presentations in the same vial must
    compare as the same vial, or the container looks like a differentiator when it is not."""
    assert psa["rec"]._vial_key(written) == key


@pytest.mark.parametrize("written,key", [
    ("10 mL vial", "10ML"), ("10mL Botox", "10ML"), ("2 ML", "2ML"),
])
def test_a_millilitre_size_is_recognised_when_there_is_no_r_token(psa, written, key):
    assert psa["rec"]._vial_key(written) == key


def test_an_r_token_wins_over_a_millilitre_token(psa):
    """'2R (2.00 mL) vial' contains both; the R designation is the container and the mL is
    the fill, so keying on the fill would group different containers together."""
    assert psa["rec"]._vial_key("2R (2.00 mL) vial") == "2R"


def test_an_unrecognised_size_falls_back_to_the_stripped_text(psa):
    """Never blank for a non-blank input: a key of '' would compare equal to every other
    unknown size and manufacture false clashes."""
    assert psa["rec"]._vial_key("Cartridge XL") == "CARTRIDGEXL"


def test_a_blank_size_has_a_blank_key(psa):
    """Which the graders check for, so vial size drops out of the comparison entirely."""
    assert psa["rec"]._vial_key("") == ""
    assert psa["rec"]._vial_key(None) == ""


# ── the state normaliser ───────────────────────────────────────────────────────


def test_a_state_compares_case_insensitively(psa):
    assert psa["rec"]._state_key("liquid") == psa["rec"]._state_key("Liquid")


def test_a_blank_state_has_no_key(psa):
    """A blank-state product only compares against other blank-state products. Treating
    blank as a value would compare it against everything."""
    assert psa["rec"]._state_key("") is None
    assert psa["rec"]._state_key(None) is None
    assert psa["rec"]._state_key("   ") is None


def test_different_states_get_different_keys(psa):
    assert psa["rec"]._state_key("Liquid") != psa["rec"]._state_key("Lyo Powder")


# ── grading the caps already in use ────────────────────────────────────────────


def taken_cap(**columns):
    """One already-utilised cap, in the shape `_taken` produces."""
    return {"vial_key": "2R", "vial_size": "2R", "hex": "#0000ff",
            "cap": "Blue 6043", "program_no": "PRG-B", **columns}


def test_the_same_vial_and_a_near_shade_is_the_highest_risk(psa):
    """Same container, indistinguishable colour: the case the whole programme exists to
    prevent."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="#0000ff")], "2R", {"hex": "#0000fe"}
    )

    assert graded[0]["risk"] == "High"
    assert graded[0]["same_vial"] is True
    assert graded[0]["close_shade"] is True


def test_the_same_vial_with_a_clear_shade_difference_is_medium(psa):
    """The container alone does not distinguish them, so the colour is carrying the whole
    burden — worth flagging even though it currently succeeds."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="#ff0000")], "2R", {"hex": "#0000ff"}
    )

    assert graded[0]["risk"] == "Medium"
    assert graded[0]["close_shade"] is False


def test_a_different_vial_with_a_near_shade_is_medium(psa):
    """The colours could be confused, but the container tells them apart."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(vial_key="10R", hex="#0000ff")], "2R", {"hex": "#0000fe"}
    )

    assert graded[0]["risk"] == "Medium"
    assert graded[0]["same_vial"] is False


def test_a_different_vial_and_a_different_shade_is_low(psa):
    graded = psa["rec"]._grade_taken(
        [taken_cap(vial_key="10R", hex="#ff0000")], "2R", {"hex": "#0000ff"}
    )

    assert graded[0]["risk"] == "Low"


def test_the_reason_explains_a_high_grade_in_words(psa):
    """The screen shows this to an assessor who has to defend the decision, so it names both
    the vial and the shade distance rather than only the grade."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="#0000ff")], "2R", {"hex": "#0000fe"}
    )

    reason = graded[0]["risk_reason"]
    assert "2R" in reason
    assert "near-identical shade" in reason
    assert "ΔE" in reason


def test_a_blocked_colour_says_it_is_blocked(psa):
    """Same vial means the colour cannot be reused at that size, and the reason has to say
    so — otherwise the assessor sees "Medium" and may pick it anyway."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="#ff0000")], "2R", {"hex": "#0000ff"}
    )

    assert "blocked at 2R" in graded[0]["risk_reason"]


def test_the_grades_are_sorted_worst_first(psa):
    """An assessor reads the top of the list."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(vial_key="10R", hex="#ff0000"), taken_cap(hex="#0000fe")],
        "2R", {"hex": "#0000ff"},
    )

    assert [t["risk"] for t in graded] == ["High", "Low"]


def test_ties_are_broken_by_the_closest_shade(psa):
    """Two Medium clashes are not equally worrying."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="#ff0000"), taken_cap(hex="#3333ff")], "2R", {"hex": "#0000ff"}
    )

    assert graded[0]["delta_e_to_subject"] < graded[1]["delta_e_to_subject"]


def test_the_risk_order_is_the_one_the_sort_uses(psa):
    assert psa["rec"].RISK_ORDER == {"High": 0, "Medium": 1, "Low": 2}


# ── the honesty rule: never imply a comparison that did not happen ────────────


def test_a_subject_with_no_cap_gets_no_shade_comparison(psa):
    """At assessment time the cap is often not chosen yet. Reporting ΔE 0 against a blank
    would read as a perfect colour match."""
    graded = psa["rec"]._grade_taken([taken_cap()], "2R", None)

    assert graded[0]["delta_e_to_subject"] is None
    assert graded[0]["close_shade"] is False


def test_the_reason_says_why_no_shade_was_compared(psa):
    """So the screen never implies a colour comparison that did not happen."""
    graded = psa["rec"]._grade_taken([taken_cap()], "2R", None)

    assert "shade not compared" in graded[0]["risk_reason"]
    assert "no cap colour with a known swatch is selected" in graded[0]["risk_reason"]


def test_a_taken_cap_with_no_swatch_is_distinguished_from_a_missing_subject_cap(psa):
    """Two different reasons for the same missing number, and an assessor needs to know
    which side the gap is on."""
    graded = psa["rec"]._grade_taken(
        [taken_cap(hex="")], "2R", {"hex": "#0000ff"}
    )

    assert graded[0]["delta_e_to_subject"] is None
    assert "this cap has no swatch colour on file" in graded[0]["risk_reason"]


def test_without_a_shade_the_grade_rests_on_the_vial_alone(psa):
    """Same vial and no colour information is still a Medium, because the container really
    does fail to distinguish them."""
    graded = psa["rec"]._grade_taken([taken_cap()], "2R", None)

    assert graded[0]["risk"] == "Medium"


def test_neither_a_shade_nor_a_vial_match_is_the_lowest_grade(psa):
    graded = psa["rec"]._grade_taken([taken_cap(vial_key="10R")], "2R", None)

    assert graded[0]["risk"] == "Low"


def test_a_subject_with_no_vial_size_matches_no_vial(psa):
    """A blank key must not compare equal to a real one, or every product would look like it
    shared the subject's container."""
    graded = psa["rec"]._grade_taken([taken_cap(vial_key="2R")], "", {"hex": "#0000ff"})

    assert graded[0]["same_vial"] is False


# ── ranking the free colours ──────────────────────────────────────────────────


def test_the_nearest_taken_cap_is_found(psa):
    """The ranking metric: a candidate is only as good as its closest clash."""
    delta, nearest = psa["rec"]._min_delta_e(
        "#0000ff", [taken_cap(hex="#ff0000"), taken_cap(hex="#0000fe", cap="Blue")]
    )

    assert nearest["cap"] == "Blue"
    assert delta < 2


def test_caps_without_a_solid_hex_are_skipped_in_the_ranking(psa):
    """A transparent or unknown cap has no shade to be far from, so including it would
    fabricate a distance."""
    delta, nearest = psa["rec"]._min_delta_e(
        "#0000ff", [taken_cap(hex=""), taken_cap(hex="#ff0000", cap="Red")]
    )

    assert nearest["cap"] == "Red"


def test_no_comparable_cap_gives_no_distance(psa):
    """Which the caller reports as "no comparison" rather than as a perfect score."""
    assert psa["rec"]._min_delta_e("#0000ff", [taken_cap(hex="")]) == (None, None)


def test_an_empty_site_gives_no_distance(psa):
    """A first product at a new site has nothing to clash with."""
    assert psa["rec"]._min_delta_e("#0000ff", []) == (None, None)


def test_the_close_shade_threshold_is_the_calibrated_one(psa):
    """~2.3 ΔE is just-noticeable; 12 is the "could be confused on a shelf" distance these
    two constants share. SME-tunable, so pinned."""
    assert psa["rec"].CLOSE_DELTA_E == 12.0
    assert psa["rec"].SAME_SHADE_DELTA_E == psa["rec"].CLOSE_DELTA_E


# ── the whole recommendation, against a real database ─────────────────────────


def sited_product(con, product_id=1, site_id=1, **columns):
    """A presentation with a manufacturing site, which is what a recommendation needs.

    Without an `mfr` row the answer is a `note` explaining that co-location cannot be
    assessed — a legitimate outcome, covered separately below.
    """
    add_product(con, product_id, **columns)
    con.execute("INSERT OR IGNORE INTO site (site_id, site_code) VALUES (?, ?)",
                (site_id, f"AP{site_id:02d}"))
    con.commit()
    link_site(con, product_id, site_id)
    return product_id


def test_a_recommendation_names_the_subject(psa, con):
    """`subject` is the display projection the screen renders, so it carries a readable
    label rather than the raw columns."""
    sited_product(con, 1, program_no="ABBV-400", program_name="Etentamig")

    result = psa["rec"].recommend(con, 1)

    assert result["subject"]["label"] == "ABBV-400 (Etentamig)"
    assert result["subject"]["vial_size"] == "2R"


def test_a_recommendation_offers_colours(psa, con):
    """With nothing taken, the whole palette is available — and the screen still needs an
    ordered list to show."""
    sited_product(con, 1)

    result = psa["rec"].recommend(con, 1)

    assert result["recommended"], "an empty site should still recommend something"


def test_every_recommendation_names_its_own_supplier(psa, con):
    """The best colour names its own supplier, because the ranking spans every catalogue —
    so the same hue can appear twice, once per supplier."""
    sited_product(con, 1)

    result = psa["rec"].recommend(con, 1)

    assert all(r.get("vendor") for r in result["recommended"])


def test_the_recommendation_considers_both_catalogues_by_default(psa, con):
    """The bug this replaced restricted the job to one catalogue while claiming otherwise.

    The check is on the CANDIDATE pool rather than on the top ten: both suppliers stock
    similar colours, so the best few may legitimately all come from one catalogue. What must
    not happen is the other catalogue never being looked at — so a `top_n` wide enough to
    reach past the ranking is what makes this test mean anything.
    """
    sited_product(con, 1)

    result = psa["rec"].recommend(con, 1, top_n=60)

    assert len({r["vendor"].upper() for r in result["recommended"]}) > 1, (
        "the default must reach more than one supplier's catalogue"
    )


def test_restricting_to_one_supplier_yields_fewer_candidates(psa, con):
    """The observable difference between "every supplier" and "one supplier" — the property
    the original default silently removed."""
    sited_product(con, 1)

    every = psa["rec"].recommend(con, 1, top_n=60)["recommended"]
    one = psa["rec"].recommend(con, 1, vendor="Datwyler", top_n=60)["recommended"]

    assert len(one) < len(every)


def test_naming_a_vendor_restricts_the_recommendation(psa, con):
    """An explicit restriction is honoured — it is only the DEFAULT that must not restrict."""
    sited_product(con, 1)

    result = psa["rec"].recommend(con, 1, vendor="Datwyler")

    assert {r["vendor"].upper() for r in result["recommended"]} == {"DATWYLER"}


def test_the_recommendation_records_which_supplier_it_considered(psa, con):
    """The checkpoint carries this into the audit trail, and it is the field that exposed the
    original default-vendor bug: it read ['Datwyler'] while the message said every supplier."""
    sited_product(con, 1)

    assert psa["rec"].recommend(con, 1)["vendor"] is psa["rec"].ALL_VENDORS


def test_the_number_of_recommendations_is_capped(psa, con):
    """A list of sixty colours is not a recommendation."""
    sited_product(con, 1)

    result = psa["rec"].recommend(con, 1, top_n=3)

    assert len(result["recommended"]) <= 3


def test_a_product_with_no_manufacturing_site_says_why_it_cannot_be_assessed(psa, con):
    """Cap co-location is a property of a manufacturing line, so with no site there is
    nothing to assess — and the note has to tell the assessor what to go and fix rather
    than silently returning an empty list."""
    add_product(con, 1, program_no="ABBV-400")

    result = psa["rec"].recommend(con, 1)

    assert result["recommended"] == []
    assert "no manufacturing site recorded" in result["note"]


def test_the_default_cap_is_ten(psa, con):
    assert psa["rec"].DEFAULT_TOP_N == 10


def test_a_cap_in_use_at_the_same_site_is_reported_as_taken(psa, con):
    """The basis of the whole answer: what is already in use at the site the subject is
    manufactured at."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="PRG-A")
    add_product(con, 2, program_no="PRG-B")
    link_site(con, 1, site)
    link_site(con, 2, site)
    add_cap(con, 2, name="Blue 6043")

    result = psa["rec"].recommend(con, 1)

    assert result["taken"], "the colocated product's cap must be listed"


def test_a_cap_at_another_site_is_not_taken(psa, con):
    """It cannot be mixed up with the subject on a line it never appears on."""
    add_product(con, 1)
    add_product(con, 2)
    link_site(con, 1, add_site(con, 1, "AP01"))
    link_site(con, 2, add_site(con, 2, "AP02"))
    add_cap(con, 2, name="Blue 6043")

    assert psa["rec"].recommend(con, 1)["taken"] == []


def test_a_clinical_products_cap_does_not_block_a_colour(psa, con):
    """Clinical presentations are out of scope, so their caps must not constrain a
    commercial assessment."""
    site = add_site(con, 1)
    add_product(con, 1, program_no="PRG-A")
    add_product(con, 2, program_no="PRG-B", batch_type="Clinical")
    link_site(con, 1, site)
    link_site(con, 2, site)
    add_cap(con, 2, name="Blue 6043")

    assert psa["rec"].recommend(con, 1)["taken"] == []


def test_the_recommendation_never_raises_on_a_data_gap(psa, con):
    """"Dict-returning; never raises on data gaps" is the contract: this runs on every click,
    and a product with nothing filled in is common in a pipeline sheet."""
    add_product(con, 1, modality=None, form=None, vial_container_size=None)

    result = psa["rec"].recommend(con, 1)

    assert isinstance(result, dict)


def test_an_unknown_product_is_reported_rather_than_raised(psa, con):
    """The screen asks for whatever row the user picked, and a stale row id is normal after
    a database rebuild."""
    result = psa["rec"].recommend(con, 999)

    assert isinstance(result, dict)
    assert not result.get("recommended")


# ── the tables the screen shows beside the recommendation ─────────────────────


def test_products_by_colour_finds_a_product_using_that_colour(psa, con):
    """Matching is by CODE, not by hue: 'Blue 6043' resolves to 6043 and returns only
    products carrying that code, rather than every blue in the catalogue."""
    add_product(con, 1, program_no="ABBV-400", program_name="Etentamig")
    add_cap(con, 1, name="Blue 6043")

    rows = psa["queries"].products_by_color(con, "Datwyler", "Blue 6043")

    assert len(rows) == 1
    assert "ABBV-400" in rows[0]["product"]
    assert rows[0]["cap_color_name"] == "Blue 6043"


def test_products_by_colour_does_not_return_every_shade_of_the_hue(psa, con):
    """The reason the match is on the code. 'Blue 6043' and 'Blue 6044' are different caps,
    and conflating them would tell an assessor a colour was taken when it was not."""
    add_product(con, 1, program_no="ABBV-400")
    add_cap(con, 1, name="Blue 6044")

    assert psa["queries"].products_by_color(con, "Datwyler", "Blue 6043") == []


def test_products_by_colour_reports_the_manufacturing_sites(psa, con):
    """Which is what makes the row actionable: the same cap at a different site is not a
    clash."""
    add_product(con, 1, program_no="ABBV-400")
    add_cap(con, 1, name="Blue 6043")
    link_site(con, 1, add_site(con, 1, "AP01"))

    rows = psa["queries"].products_by_color(con, "Datwyler", "Blue 6043")

    assert rows[0]["mfr_sites"] == "AP01"


def test_products_by_colour_excludes_a_clinical_product(psa, con):
    """Every listing applies the same scope rule, or the screen and the report disagree."""
    add_product(con, 1, batch_type="Clinical")
    add_cap(con, 1, name="Blue 6043")

    assert psa["queries"].products_by_color(con, "Datwyler", "Blue 6043") == []


def test_products_by_colour_is_empty_for_an_unused_colour(psa, con):
    add_product(con, 1)

    assert psa["queries"].products_by_color(con, "Datwyler", "Blue 6043") == []


def test_the_site_counts_count_products_per_site(psa, con):
    """The bar chart's numbers."""
    site = add_site(con, 1, "AP01")
    add_product(con, 1)
    add_product(con, 2)
    link_site(con, 1, site)
    link_site(con, 2, site)

    rows = psa["queries"].site_product_counts(con)

    assert rows
    row = next(r for r in rows if r["site_code"] == "AP01")
    assert row["n_products"] == 2


def test_the_site_counts_exclude_clinical_products(psa, con):
    site = add_site(con, 1, "AP01")
    add_product(con, 1)
    add_product(con, 2, batch_type="Clinical")
    link_site(con, 1, site)
    link_site(con, 2, site)

    row = next(r for r in psa["queries"].site_product_counts(con)
               if r["site_code"] == "AP01")
    assert row["n_products"] == 1


def test_a_site_with_no_products_does_not_appear(psa, con):
    """A zero row in the chart is noise."""
    add_site(con, 1, "AP01")

    assert psa["queries"].site_product_counts(con) == []
