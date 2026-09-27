"""Cap-colour normalisation, the supplier palette, and the override that edits it.

`cap_colors.py` turns the Smartsheet's free-text cap-colour strings into a canonical colour plus
a representative hex, and `palette.py` holds the catalogue it matches against. Both are pure
functions over rows, so nothing here needs a database, a sheet or a network.

These are characterisation tests: the normaliser was tuned against the real sheet's spelling, so
they pin the behaviour that already exists rather than a specification someone wrote down. Where
a case looks arbitrary — 'Magenta 2063C' being custom while 'Blue 6043' is not — it is recording
a real distinction in the data, and the docstring on each test says which.

**The override is process-global and lives in the platform object store**, not under PSA's own
`storage_dir` (`palette.get_object_store()` is the platform's). So every test that mutates it
must put the store back, or the next test reads a catalogue the previous one edited and passes
for the wrong reason. `clean_palette` below does that, and it is autouse.
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
    from da_silos.psa import cap_colors, palette, scope

    return {"cap_colors": cap_colors, "palette": palette, "scope": scope}


@pytest.fixture(autouse=True)
def clean_palette(psa):
    """Remove any override and empty the palette cache, before and after every test.

    Both halves matter. Before, because a leaked override from another test would change
    what `palette()` returns. After, because this store outlives the test — it is the
    platform's, keyed on `psa/palette/...`, and nothing else clears it.
    """
    cap_colors, palette = psa["cap_colors"], psa["palette"]

    def reset():
        store = palette.get_object_store()
        try:
            store.delete(palette.override_key())
        except Exception:
            pass
        cap_colors._PALETTE_CACHE = None

    reset()
    yield
    reset()


# ── the shipped palette ───────────────────────────────────────────────────────


def test_the_default_palette_loads_from_the_shipped_asset(psa):
    """The catalogue is a shipped CSV, so an empty palette means the asset did not travel
    with the silo — and every recommendation would silently have nothing to rank."""
    rows = psa["cap_colors"].palette()

    assert rows, "no palette rows; check assets/cap_palette.csv shipped"
    assert all(r["vendor"] for r in rows)


def test_every_palette_row_carries_a_canonical_colour(psa):
    """A row with no canonical colour can never be matched or recommended, so it would be
    dead weight in the catalogue."""
    assert all(r["canonical_color"] for r in psa["cap_colors"].palette())


def test_the_palette_can_be_filtered_to_one_vendor(psa):
    rows = psa["cap_colors"].palette(vendor="Datwyler")

    assert rows
    assert {r["vendor"].upper() for r in rows} == {"DATWYLER"}


def test_the_vendor_filter_is_case_insensitive(psa):
    """The Smartsheet's spelling of a supplier is not something to depend on."""
    assert psa["cap_colors"].palette(vendor="datwyler")


def test_the_palette_defaults_to_pp_discs(psa):
    """PP disc is the component the cap-colour screen is about; the catalogue holds others."""
    rows = psa["cap_colors"].palette()

    assert {r["component"] for r in rows} == {"pp_disc"}


def test_passing_no_component_returns_every_component(psa):
    """`component=None` is how the code matcher searches the whole catalogue — a code is
    authoritative regardless of which component it belongs to."""
    every = psa["cap_colors"].palette(component=None)
    discs = psa["cap_colors"].palette(component="pp_disc")

    assert len(every) >= len(discs)


def test_the_off_the_shelf_filter_excludes_custom_colours(psa):
    rows = psa["cap_colors"].palette(off_the_shelf_only=True)

    assert rows
    assert all(r["off_the_shelf"] for r in rows)


def test_each_row_is_enriched_with_a_normalised_code(psa):
    """`_code_norm` is what the code matcher compares against, so a row without it can
    never be reached by a code hit even when its `vendor_code` looks right."""
    rows = [r for r in psa["cap_colors"].palette(component=None) if r["vendor_code"]]

    assert rows
    assert all("_code_norm" in r for r in rows)


def test_available_for_narrows_the_palette_to_a_diameter(psa):
    """A cap that does not fit the vial is not a candidate, however good the colour match.

    `sizes` holds the diameters as strings, because they come from splitting the CSV's
    `sizes_mm` ('13;20') rather than from a numeric column.
    """
    rows = psa["cap_colors"].available_for("Datwyler", 20)

    assert rows
    assert all("20" in (r["sizes"] or []) for r in rows), "every row must list the diameter"


def test_a_row_with_no_stated_sizes_is_kept(psa):
    """A blank `sizes_mm` means "not transcribed yet", not "fits nothing" — dropping those
    rows would silently shrink the catalogue as new suppliers are added."""
    rows = psa["cap_colors"].available_for("Datwyler", 999)

    assert all(not r["sizes"] for r in rows), (
        "only rows that state no size may survive an unstocked diameter"
    )


def test_a_non_numeric_diameter_is_stripped_before_matching(psa):
    """The screen may send '20 mm'; the catalogue stores '20'."""
    assert psa["cap_colors"].available_for("Datwyler", "20mm") == \
        psa["cap_colors"].available_for("Datwyler", 20)


def test_no_diameter_at_all_returns_the_whole_palette(psa):
    """The recommendation engine asks this way when the vial size is not yet known."""
    every = psa["cap_colors"].palette(vendor="Datwyler", off_the_shelf_only=True)

    assert len(psa["cap_colors"].available_for("Datwyler", None)) == len(every)


def test_available_for_spans_every_supplier_when_no_vendor_is_named(psa):
    """The best cap colour is chosen across both catalogues and the supplier is an attribute
    of the answer, not an input to it (decided 2026-08-21)."""
    rows = psa["cap_colors"].available_for(None, 20)

    assert len({r["vendor"].upper() for r in rows}) >= 1
    assert len(rows) >= len(psa["cap_colors"].available_for("Datwyler", 20))


# ── normalising the sheet's free text ─────────────────────────────────────────


def test_a_code_match_is_authoritative(psa):
    """The code ties to a catalogue row, which is stronger evidence than the colour word
    beside it — so confidence is 'code' and the hex comes from the row, not the fallback."""
    result = psa["cap_colors"].normalize("Blue 6043")

    assert result["canonical_color"] == "Blue"
    assert result["vendor_code"] == "6043"
    assert result["matched_code"] is True
    assert result["confidence"] == "code"


def test_a_bare_code_is_enough(psa):
    """The sheet does not always spell the colour out beside the code."""
    result = psa["cap_colors"].normalize("6043")

    assert result["canonical_color"] == "Blue"
    assert result["confidence"] == "code"


def test_a_colour_word_alone_matches_by_name(psa):
    """No code, so the hex comes from the fallback table and confidence drops to 'name'."""
    result = psa["cap_colors"].normalize("Red")

    assert result["canonical_color"] == "Red"
    assert result["confidence"] == "name"
    assert result["matched_code"] is False


def test_the_hex_is_populated_for_a_recognised_colour(psa):
    """The hex is what the swatch renders, so a recognised colour with no hex would show
    the assessor a blank chip."""
    assert psa["cap_colors"].normalize("Blue 6043")["hex"].startswith("#")


def test_a_named_colour_with_an_unmatched_code_is_custom(psa):
    """The BoNT/E cap: 'Magenta 2063C' is a Pantone reference, not a supplier code, so it
    does not tie to the palette. A recognised colour that is not off-the-shelf is a custom
    cap, and calling it standard would understate the tooling work."""
    result = psa["cap_colors"].normalize("Magenta 2063C")

    assert result["canonical_color"] == "Magenta"
    assert result["matched_code"] is False
    assert result["is_custom"] is True
    assert result["off_the_shelf"] is False


def test_an_off_the_shelf_colour_is_not_flagged_custom(psa):
    result = psa["cap_colors"].normalize("Blue 6043")

    assert result["is_custom"] is False
    assert result["off_the_shelf"] is True


@pytest.mark.parametrize("token", ["TBD", "TBC", "N/A", "NA", "NONE", "PENDING", ""])
def test_the_placeholder_tokens_are_unknown_not_wrong(psa, token):
    """The sheet uses all of these for "not decided yet". Guessing a colour from one would
    put a fictional cap in an assessment."""
    result = psa["cap_colors"].normalize(token)

    assert result["is_unknown"] is True
    assert result["canonical_color"] is None


def test_the_placeholder_check_ignores_case(psa):
    assert psa["cap_colors"].normalize("tbd")["is_unknown"] is True


def test_uninterpretable_text_is_unknown_rather_than_an_error(psa):
    """"Returns a dict (never raises)" is the contract: this runs over every row of a live
    sheet, and one odd cell must not stop an assessment."""
    result = psa["cap_colors"].normalize("qwertyuiop")

    assert result["is_unknown"] is True
    assert result["confidence"] == "none"


def test_none_is_treated_as_a_blank(psa):
    """A NULL cell arrives as None, and it is as common as an empty string."""
    assert psa["cap_colors"].normalize(None)["is_unknown"] is True


def test_the_raw_input_is_always_returned(psa):
    """The audit trail needs what the sheet actually said, not only our reading of it."""
    assert psa["cap_colors"].normalize("  Blue 6043  ")["raw"] == "Blue 6043"


def test_a_vendor_hint_is_kept_when_nothing_matches(psa):
    """The caller knows the row's supplier; an unknown colour should not discard it."""
    assert psa["cap_colors"].normalize("TBD", vendor_hint="West")["vendor"] == "West"


def test_a_code_hit_prefers_a_row_of_the_hinted_vendor(psa):
    """Two suppliers can use overlapping code numbers, so the hint decides which
    catalogue's row the answer comes from."""
    result = psa["cap_colors"].normalize("6043", vendor_hint="Datwyler")

    assert result["vendor"].upper() == "DATWYLER"
    assert result["confidence"] == "code"


def test_a_cross_vendor_code_hit_is_flagged_rather_than_dropped(psa):
    """A Datwyler 6xxx code is authoritative even if the sheet mis-tagged the vendor as
    West — a real data inconsistency. The answer is still given, but the confidence says
    it disagreed with the hint, so a reviewer can see why."""
    result = psa["cap_colors"].normalize("6043", vendor_hint="West")

    assert result["matched_code"] is True
    assert result["confidence"] == "code-xvendor"


# ── perceptual distance, which is what ranks a recommendation ─────────────────


def test_a_colour_is_zero_distance_from_itself(psa):
    assert psa["cap_colors"].delta_e("#ff0000", "#ff0000") == 0


def test_distance_is_symmetric(psa):
    """Ranking would depend on argument order otherwise."""
    forward = psa["cap_colors"].delta_e("#ff0000", "#00ff00")
    backward = psa["cap_colors"].delta_e("#00ff00", "#ff0000")

    assert forward == pytest.approx(backward)


def test_a_near_colour_is_closer_than_an_opposite_one(psa):
    """The whole point of using ΔE rather than string equality: the ranking has to agree
    with what an assessor sees."""
    near = psa["cap_colors"].delta_e("#ff0000", "#fe0000")
    far = psa["cap_colors"].delta_e("#ff0000", "#0000ff")

    assert near < far


def test_a_hex_without_its_hash_is_accepted(psa):
    """The CSV and the sheet are not consistent about the leading '#'."""
    assert psa["cap_colors"].delta_e("ff0000", "#ff0000") == 0


def test_a_missing_hex_gives_no_distance_rather_than_a_number(psa):
    """An unknown colour has `hex == ''`, and a transparent cap has no solid colour at all.
    Returning 0 would rank those as a perfect match; None makes the caller decide."""
    assert psa["cap_colors"].delta_e("", "#ff0000") is None
    assert psa["cap_colors"].delta_e("#ff0000", None) is None


def test_a_malformed_hex_gives_no_distance(psa):
    """Hand-entered data reaches this, and a partial hex must not become a wrong number."""
    assert psa["cap_colors"].delta_e("#ff", "#ff0000") is None


# ── the override: editing the catalogue the tool recommends from ──────────────


def test_a_clean_deployment_reports_no_override(psa):
    state = psa["palette"].load()

    assert state.is_override is False
    assert (state.added, state.removed) == (0, 0)


def test_adding_a_colour_makes_it_available(psa):
    """The point of the editor: a colour the supplier stocks but the shipped CSV missed."""
    ok, message = psa["cap_colors"].add_color(
        "Datwyler", "Test Teal", "Teal", hex="#008080", sizes_mm="20"
    )

    assert ok, message
    assert psa["palette"].contains("Datwyler", "Test Teal")


def test_an_added_colour_appears_in_the_palette(psa):
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")

    names = {r["vendor_color_name"] for r in psa["cap_colors"].palette(vendor="Datwyler")}
    assert "Test Teal" in names


def test_the_state_counts_the_addition(psa):
    """The screen shows these counts to say "this catalogue has been edited"."""
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")

    state = psa["palette"].load()
    assert state.is_override is True
    assert state.added == 1


def test_re_adding_a_colour_updates_it_rather_than_duplicating(psa):
    """Two rows with the same (vendor, name) would both be offered and rank separately."""
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#009090")

    rows = [r for r in psa["cap_colors"].palette(vendor="Datwyler")
            if r["vendor_color_name"] == "Test Teal"]
    assert len(rows) == 1
    assert rows[0]["hex"] == "#009090"


def test_removing_a_shipped_colour_takes_it_out_of_the_palette(psa):
    """A supplier discontinuing a colour must stop it being recommended, without editing
    the shipped asset."""
    first = psa["cap_colors"].palette(vendor="Datwyler")[0]

    ok, message = psa["cap_colors"].remove_color("Datwyler", first["vendor_color_name"])

    assert ok, message
    names = {r["vendor_color_name"] for r in psa["cap_colors"].palette(vendor="Datwyler")}
    assert first["vendor_color_name"] not in names


def test_the_state_counts_the_removal(psa):
    first = psa["cap_colors"].palette(vendor="Datwyler")[0]

    psa["cap_colors"].remove_color("Datwyler", first["vendor_color_name"])

    assert psa["palette"].load().removed == 1


def test_removing_an_added_colour_leaves_no_trace(psa):
    """Add then remove should return to the shipped catalogue rather than record both."""
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")
    psa["cap_colors"].remove_color("Datwyler", "Test Teal")

    assert psa["palette"].contains("Datwyler", "Test Teal") is False


def test_removing_a_colour_that_is_not_there_is_reported(psa):
    """A silent success would tell the screen an edit happened when none did."""
    ok, message = psa["cap_colors"].remove_color("Datwyler", "No Such Colour")

    assert ok is False
    assert message


def test_the_override_survives_a_cache_clear(psa):
    """The override is stored, not remembered: a worker restart must not lose an edit."""
    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")
    psa["cap_colors"]._PALETTE_CACHE = None

    assert psa["palette"].contains("Datwyler", "Test Teal")


def test_the_default_rows_are_never_mutated_by_an_edit(psa):
    """The shipped asset is the fallback. If an edit reached it, resetting the override
    could not restore the original catalogue."""
    before = len(psa["palette"].default_rows())

    psa["cap_colors"].add_color("Datwyler", "Test Teal", "Teal", hex="#008080")

    assert len(psa["palette"].default_rows()) == before


# ── scope: which presentations the assessment is about ────────────────────────


@pytest.mark.parametrize("batch_type", ["Commercial", "Pipeline", "commercial", None, "", "  "])
def test_non_clinical_presentations_are_in_scope(psa, batch_type):
    """Business decision, 2026-07-17: prioritise COMMERCIAL and PIPELINE. A blank counts as
    in scope — unknown is not clinical, and dropping it would silently shrink the universe."""
    assert psa["scope"].is_in_scope(batch_type) is True


@pytest.mark.parametrize("batch_type", ["Clinical", "clinical", "  CLINICAL  "])
def test_clinical_presentations_are_out_of_scope(psa, batch_type):
    """Matched case-insensitively and trimmed, because the sheet is hand-entered."""
    assert psa["scope"].is_in_scope(batch_type) is False


def test_the_scope_decision_explains_itself(psa):
    """G-5: the audit trail has to say why a presentation was left out, not just that it was."""
    assert "excluded" in psa["scope"].scope_reason("Clinical")
    assert "in scope" in psa["scope"].scope_reason("Commercial")


def test_the_reason_names_an_unspecified_batch_type(psa):
    """"in scope (batch_type=)" would read like a bug to whoever audits it."""
    assert "unspecified" in psa["scope"].scope_reason(None)


def test_the_scope_predicate_keeps_null_rows(psa):
    """The SQL has to agree with `is_in_scope`, or a listing and the risk engine would
    disagree about the universe. NULL is the case a plain NOT IN would silently drop."""
    predicate = psa["scope"].scope_sql("p")

    assert "IS NULL" in predicate
    assert "'CLINICAL'" in predicate


def test_the_scope_predicate_honours_its_alias(psa):
    assert psa["scope"].scope_sql("p2").count("p2.batch_type") == 2


def test_the_late_stage_predicate_also_excludes_discontinued(psa):
    """A provisional proxy for QPP11-04-001-G004 §1.3: in scope AND not discontinued. It is
    a superset of the true definition, because the sheet exposes no primary-stability
    milestone — so it must at least not offer discontinued products as comparators."""
    predicate = psa["scope"].late_stage_sql("p")

    assert "DISCONTINUED" in predicate
    assert "batch_type" in predicate, "it must still apply the scope rule"
