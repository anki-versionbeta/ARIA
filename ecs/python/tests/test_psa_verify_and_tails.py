"""The PSA silo's tails: the CLI entry points, the fallbacks, and the ports.

Every module covered here is small, and most of what is left in it is a branch that only fires when
something is missing — no database, no credentials, no `da_platform`, an unreadable override. Those
are exactly the branches that decide whether a deployment degrades or 500s, so they are worth a test
each even though none of them is where the interesting logic lives. This file collects them rather
than scattering two-test files across ten names.

What is stubbed:

* **The network, always.** `urlopen` is replaced with a `pytest.fail`, so any test that reached
  Smartsheet fails loudly instead of hanging or passing against live data. The catalogue the
  database-backed tests read is built by `tests.psa_fixtures` from bytes, through the real ingest.
* **`sys.argv`**, for the three `main()`/`_demo()` entry points, via `monkeypatch`.
* **Nothing else.** The palette override is real (it goes through the platform object store), and
  the reports the `verify` tests read are produced by the real `engines.generate_report`.

The palette override is process-global and lives in the PLATFORM object store, not under the test's
`tmp_path` — so `clean_palette` below is autouse and resets `palette._CACHE` as well as
`cap_colors._PALETTE_CACHE`. Without it an override written here leaks into every later test in the
session, including the ones in `test_psa_cap_colors.py`.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import urllib.request

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

from tests.psa_fixtures import (  # noqa: F401 — imported for pytest to see the fixtures
    SUBJECT,
    SUBJECT_ROW,
    catalogue,
    con,
    docx_text,
    empty_db,
    generated_report,
    psa_config,
    row,
    sheet,
    snapshot_bytes,
    subject_product_id,
)

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import (
        aria,
        build_db,
        cap_colors,
        cap_queries,
        cap_recommend,
        config,
        palette,
        paths,
        risk,
        verify,
    )
    from da_silos.psa.storage import base as storage_base

    return {
        "aria": aria,
        "build_db": build_db,
        "cap_colors": cap_colors,
        "cap_queries": cap_queries,
        "cap_recommend": cap_recommend,
        "config": config,
        "palette": palette,
        "paths": paths,
        "risk": risk,
        "storage_base": storage_base,
        "verify": verify,
    }


@pytest.fixture(autouse=True)
def offline(psa, psa_config, monkeypatch):
    """Pin the config to the temp workspace and make any network call a test failure.

    Depends on `psa_config` rather than `catalogue`, so it is in place before a test builds its
    catalogue or generates a report — `ingest_source="xlsx"` plus a poisoned `urlopen` is what
    guarantees no test in this file can reach Smartsheet even by accident.
    """
    psa["config"].set_config(dataclasses.replace(psa_config, ingest_source="xlsx"))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("reached the network")
    )


@pytest.fixture(autouse=True)
def clean_palette(psa):
    """Remove any palette override and empty both caches, before and after every test.

    The store is the platform's, keyed on `psa/palette/...`, and it outlives both the test and the
    `tmp_path` fixture — so an override written by one test is visible to every later one unless it
    is cleared here. `palette._CACHE` keys on the override bytes and `cap_colors._PALETTE_CACHE` on
    the row list identity, so both have to go.
    """
    palette, cap_colors = psa["palette"], psa["cap_colors"]

    def reset():
        try:
            palette.get_object_store().delete(palette.override_key())
        except Exception:
            pass
        palette._CACHE = None
        cap_colors._PALETTE_CACHE = None

    reset()
    yield
    reset()


# ── verify.py: the missing-report branch ──────────────────────────────────────


def test_verify_reports_a_failure_when_the_report_has_not_been_generated(psa, con, capsys):
    """`verify` is the smoke gate a run is judged on. If a missing .docx returned 0 the pipeline
    would report success for a run that produced no deliverable at all."""
    assert not os.path.exists(
        os.path.join(psa["paths"].output_dir(), f"{SUBJECT}_PSA_generated.docx")
    )

    code = psa["verify"].main(SUBJECT)

    assert code == 1
    out = capsys.readouterr().out
    assert "generated file exists" in out
    assert "1 CHECK(S) FAILED" in out


def test_verify_stops_before_reading_the_docx_when_it_is_absent(psa, con, capsys):
    """The missing-report branch returns early. If it fell through, `Document(gen)` would raise
    and the caller would see a traceback instead of a failure count."""
    psa["verify"].main(SUBJECT)

    out = capsys.readouterr().out
    assert "[Generated docx]" in out
    # The Part A/D/E checks live after the early return, so none of them may have printed.
    assert "Part A Product Name populated" not in out
    assert "Parts D & E populated" not in out


def test_verify_still_runs_the_database_checks_before_giving_up(psa, con, capsys):
    """A missing report and an empty catalogue are different faults, and the operator needs to see
    which one happened — so the DB and provenance sections must report even on the failing path."""
    psa["verify"].main(SUBJECT)

    out = capsys.readouterr().out
    assert "product rows loaded" in out
    assert "foreign-key integrity" in out
    assert f"selected product {SUBJECT} present" in out
    assert "has provenance rows" in out


def test_verify_counts_an_unknown_program_as_a_failed_check(psa, con, capsys):
    """Asking about a program that is not in the sheet must not be mistaken for a clean run; the
    provenance section is skipped (no product) but the check itself has to fail."""
    code = psa["verify"].main("NOT-A-PROGRAM")

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL selected product NOT-A-PROGRAM present" in out
    assert "has provenance rows" not in out


def test_verify_passes_every_check_against_a_freshly_generated_report(psa, con, capsys):
    """The one green path: this is what a healthy run looks like, and it is the assertion that
    keeps the populated-report branch of `verify` honest rather than merely unexercised."""
    result = generated_report()
    assert result["status"] == "ok", result
    assert result["output_exists"] is True, result["output_path"]

    code = psa["verify"].main(SUBJECT)

    out = capsys.readouterr().out
    assert code == 0, out
    assert "ALL CHECKS PASSED" in out


# ── cap_recommend.py: unique first picks across a program ─────────────────────


def test_recommending_across_presentations_returns_a_result_per_presentation(psa, con):
    """The screen indexes these by Smartsheet row. A missing entry would leave a presentation
    with no recommendation and no error to explain why."""
    results, by_row = psa["cap_recommend"].recommend_program_presentations(con, SUBJECT)

    assert len(results) >= 1
    assert set(by_row.values()) == set(results)
    assert SUBJECT_ROW in by_row


def test_a_presentation_with_a_cap_on_file_keeps_it_rather_than_being_reassigned(psa, con):
    """Pass 1 reserves an already-selected cap. Handing that presentation a new `first_unique`
    would tell an assessor to change a cap that is already committed."""
    results, _by_row = psa["cap_recommend"].recommend_program_presentations(con, SUBJECT)

    selected = [r for r in results.values() if r.get("subject_selected")]
    assert selected, "the fixture subject has 'Blue 6043' on file"
    for res in selected:
        # `recommend` seeds the key as None; only the allocator ever fills it in.
        assert res["first_unique"] is None


def test_two_capless_presentations_are_given_different_first_picks(psa, con):
    """The whole point of the two-pass allocation: two presentations of one product must be
    visually distinguishable from EACH OTHER, not just from the neighbouring products."""
    cap_recommend = psa["cap_recommend"]
    # Two presentations of one program, neither with a cap on file, so both need an allocation.
    con.execute("UPDATE cap_color SET cap_color_name='TBD'")
    con.commit()

    results, by_row = cap_recommend.recommend_program_presentations(con, SUBJECT)
    firsts = [r["first_unique"] for r in results.values() if r.get("first_unique")]

    assert firsts
    assert len(firsts) == len(set(firsts)), "no two presentations may share a first pick"


def test_the_allocated_colour_is_moved_to_the_front_of_the_recommended_list(psa, con):
    """`first_unique` names a colour and the list is what the screen renders. If the two
    disagreed the assessor would be shown a different #1 from the one that was reserved."""
    cap_recommend = psa["cap_recommend"]
    con.execute("UPDATE cap_color SET cap_color_name='TBD'")
    con.commit()

    results, _by_row = cap_recommend.recommend_program_presentations(con, SUBJECT)

    allocated = [r for r in results.values() if r.get("first_unique")]
    assert allocated
    for res in allocated:
        assert res["recommended"][0]["vendor_color_name"] == res["first_unique"]
        # The reorder must be a permutation, never a truncation.
        assert len({e["vendor_color_name"] for e in res["recommended"]}) == len(res["recommended"])


def test_an_out_of_scope_presentation_is_never_recommended_for(psa, con):
    """Clinical rows are out of scope, and reserving a colour for one would consume a colour the
    commercial presentations need."""
    results, by_row = psa["cap_recommend"].recommend_program_presentations(con, "ABBV-403")

    assert results == {}
    assert by_row == {}


def test_recommend_program_resolves_a_program_code_against_the_default_database(psa, catalogue):
    """The wrapper the CLI and the API both use. It opens `paths.db_path()` itself, so a wrong
    path here would silently create an empty database and recommend from nothing."""
    res = psa["cap_recommend"].recommend_program(SUBJECT)

    assert res["subject"]["label"]
    assert res["recommended"]


def test_recommend_program_raises_on_a_program_that_is_not_in_the_catalogue(psa, catalogue):
    """It resolves through `risk._resolve_pid`, which raises rather than returning an error dict —
    so a caller that only checked `res.get("error")` would get an unhandled exception instead."""
    with pytest.raises(ValueError, match="no product with program_no"):
        psa["cap_recommend"].recommend_program("NOT-A-PROGRAM")


def test_recommend_program_accepts_an_explicit_database_path(psa, catalogue, tmp_path):
    """`db=` is how a caller points at a snapshot other than the configured one; ignoring it
    would read the wrong catalogue without saying so."""
    other = str(tmp_path / "copy.db")
    with sqlite3.connect(psa["paths"].db_path()) as src, sqlite3.connect(other) as dst:
        src.backup(dst)

    res = psa["cap_recommend"].recommend_program(SUBJECT, db=other)

    assert res["subject"]["label"]


def test_recommend_program_honours_a_specific_presentation(psa, con):
    """The API passes a product_id when the user picked one presentation. Resolving to the
    program's first row instead would assess the wrong vial."""
    pid = subject_product_id(con)

    res = psa["cap_recommend"].recommend_program(SUBJECT, product_id=pid)

    assert res["subject"]["product_id"] == pid


def test_every_presentation_keeps_a_full_candidate_list_after_the_allocation(psa, con):
    """The allocator reorders one list per presentation. If it mutated a shared list, the second
    presentation would be missing the colour the first was given."""
    cap_recommend = psa["cap_recommend"]
    con.execute("UPDATE cap_color SET cap_color_name='TBD'")
    con.commit()

    results, _by_row = cap_recommend.recommend_program_presentations(con, SUBJECT)
    lengths = {len(r["recommended"]) for r in results.values() if r.get("recommended")}

    assert lengths, "at least one presentation must have candidates"
    assert len(lengths) == 1, f"the presentations ended up with different list lengths: {lengths}"


def test_the_allocation_is_stable_across_two_identical_calls(psa, con):
    """It reads only from the catalogue, so the same catalogue must give the same answer — an
    assessment that changed on a page refresh could not be signed off."""
    cap_recommend = psa["cap_recommend"]
    con.execute("UPDATE cap_color SET cap_color_name='TBD'")
    con.commit()

    def firsts():
        results, by_row = cap_recommend.recommend_program_presentations(con, SUBJECT)
        return {sr: results[pid].get("first_unique") for sr, pid in by_row.items()}

    assert firsts() == firsts()


# ── cap_recommend.py: the CLI printer ────────────────────────────────────────


def test_the_printer_renders_the_subject_the_taken_caps_and_the_recommendations(psa, con, capsys):
    """`_print` is the operator's only view of a recommendation from the command line, so an
    empty or partial render is the same as no answer."""
    res = psa["cap_recommend"].recommend(con, subject_product_id(con))

    psa["cap_recommend"]._print(res)

    out = capsys.readouterr().out
    assert "Cap-colour recommendation" in out
    assert "Manufacturing site(s):" in out
    assert "already-utilised caps" in out
    assert "Recommended (top" in out
    assert res["note"][:40] in out


def test_the_printer_shows_the_subjects_current_cap_when_it_has_one(psa, con, capsys):
    """The optional line that tells the reader what the product wears today — without it the
    recommendation reads as if the cap were undecided."""
    res = psa["cap_recommend"].recommend(con, subject_product_id(con))
    assert res["subject"]["current_caps"], "the fixture subject has a cap on file"

    psa["cap_recommend"]._print(res)

    assert "Current subject cap(s):" in capsys.readouterr().out


def test_the_printer_reports_an_error_dict_instead_of_a_table(psa, capsys):
    """`recommend` returns `{"error": ...}` for an unusable subject. Falling through to the
    table code would raise a KeyError on `res["subject"]` and lose the message."""
    psa["cap_recommend"]._print({"error": "no such presentation"})

    out = capsys.readouterr().out
    assert "ERROR: no such presentation" in out
    assert "Recommended" not in out


def test_the_printer_lists_the_discouraged_colours_when_there_are_any(psa, con, capsys):
    """A colour is discouraged for a reason (it collides with a neighbour); printing the list
    without the reason, or not at all, is how a rejected colour gets re-proposed."""
    cap_recommend = psa["cap_recommend"]
    res = cap_recommend.recommend(con, subject_product_id(con))
    if not res["discouraged"]:
        pytest.skip("this catalogue produced no discouraged colours")

    cap_recommend._print(res)

    out = capsys.readouterr().out
    assert f"Discouraged ({len(res['discouraged'])})" in out
    assert res["discouraged"][0]["vendor_color_name"] in out


def test_the_recommend_cli_prints_a_report_for_the_program_named_on_the_command_line(
    psa, catalogue, monkeypatch, capsys
):
    """`python -m ...cap_recommend AGN-151586` is the documented spot-check. A non-zero exit or
    an empty stdout would make it useless as one."""
    monkeypatch.setattr("sys.argv", ["cap_recommend", SUBJECT])

    code = psa["cap_recommend"].main()

    assert code == 0
    assert "Cap-colour recommendation" in capsys.readouterr().out


def test_the_recommend_cli_restricts_to_a_supplier_given_as_the_second_argument(
    psa, catalogue, monkeypatch, capsys
):
    """Omitting the supplier spans every catalogue; naming one must restrict, or the operator
    cannot check what a single-supplier answer looks like."""
    monkeypatch.setattr("sys.argv", ["cap_recommend", SUBJECT, "Datwyler"])

    code = psa["cap_recommend"].main()

    out = capsys.readouterr().out
    assert code == 0
    assert "Datwyler only" in out
    assert "all suppliers:" not in out


# ── cap_queries.py: the two screen tables ────────────────────────────────────


def test_the_products_using_a_palette_colour_are_found_by_code(psa, con):
    """Matching is by CODE, not hue: 'Blue 6043' must return the products wearing 6043 and not
    every blue cap in the catalogue, or the collision table would over-report."""
    rows = psa["cap_queries"].products_by_color(con, "Datwyler", "Blue 6043")

    assert rows
    assert all(r["cap_color_name"] == "Blue 6043" for r in rows)
    assert any(SUBJECT in r["product"] for r in rows)


def test_a_colour_no_product_wears_returns_an_empty_list(psa, con):
    """"Never raises on a data gap" is this module's stated contract, and the screen renders the
    empty list as an empty table."""
    assert psa["cap_queries"].products_by_color(con, "Datwyler", "No Such Colour") == []


def test_the_site_counts_are_returned_busiest_first(psa, con):
    """These are the bars on the chart; an unsorted result would draw them in an arbitrary
    order and the busiest site would not read as the busiest."""
    rows = psa["cap_queries"].site_product_counts(con)

    assert rows
    counts = [r["n_products"] for r in rows]
    assert counts == sorted(counts, reverse=True)
    assert {"AP16", "LU"} <= {r["site_code"] for r in rows}


def test_the_site_counts_exclude_the_clinical_presentation(psa, con):
    """Scope is applied in SQL here and in Python in `scope.py`; if they disagreed the chart and
    the risk engine would report different universes for the same catalogue."""
    rows = {r["site_code"]: r["n_products"] for r in psa["cap_queries"].site_product_counts(con)}
    at_ap16 = con.execute(
        "SELECT COUNT(DISTINCT ps.product_id) FROM product_site ps "
        "JOIN site s ON s.site_id=ps.site_id JOIN product p ON p.product_id=ps.product_id "
        "WHERE ps.role='mfr' AND s.site_code='AP16'"
    ).fetchone()[0]

    assert rows["AP16"] == at_ap16 - 1, "the clinical presentation at AP16 must be dropped"


@pytest.mark.parametrize("query", ["site_product_counts", "products_by_color"])
def test_a_query_against_a_catalogue_with_no_tables_returns_no_rows(psa, tmp_path, query):
    """A connection to a database that was never built raises OperationalError on the first
    SELECT. Both queries swallow it, because a screen must render empty rather than 500."""
    con = sqlite3.connect(str(tmp_path / "not-built.db"))
    try:
        fn = getattr(psa["cap_queries"], query)
        args = (con,) if query == "site_product_counts" else (con, "Datwyler", "Blue 6043")

        assert fn(*args) == []
    finally:
        con.close()


def test_a_schema_only_catalogue_yields_empty_tables_rather_than_an_error(psa, empty_db):
    """"No database" and "no data" are different faults but the same rendering, and this is the
    one that must go through the real SQL rather than the exception handler."""
    assert psa["cap_queries"].site_product_counts(empty_db) == []
    assert psa["cap_queries"].products_by_color(empty_db, "Datwyler", "Blue 6043") == []


def test_the_queries_demo_declines_to_run_without_a_built_database(psa, capsys):
    """Connecting would CREATE a 0-byte `psa.db`, and the smoke tests gate on the file merely
    existing — so they would fail instead of skip."""
    assert not os.path.exists(psa["paths"].db_path())

    psa["cap_queries"]._demo()

    assert "psa.db not built" in capsys.readouterr().out
    assert not os.path.exists(psa["paths"].db_path()), "the demo must not create the database"


def test_the_queries_demo_prints_the_site_table(psa, catalogue, monkeypatch, capsys):
    """The spot-check the module docstring documents; it opens the database read-only, so it
    must not need write access to report."""
    monkeypatch.setattr("sys.argv", ["cap_queries"])

    psa["cap_queries"]._demo()

    out = capsys.readouterr().out
    assert "Products per manufacturing site:" in out
    assert "AP16" in out
    assert "Products using" not in out, "no colour argument was given"


def test_the_queries_demo_adds_the_colour_table_when_a_colour_is_named(
    psa, catalogue, monkeypatch, capsys
):
    """The second positional argument is the colour, and it is the half of the demo that
    exercises the code-matching path."""
    monkeypatch.setattr("sys.argv", ["cap_queries", "Datwyler", "Blue 6043"])

    psa["cap_queries"]._demo()

    out = capsys.readouterr().out
    assert "Products using 'Blue 6043'" in out or "Blue 6043" in out
    assert SUBJECT in out


# ── palette.py: a broken override must not stop a recommendation ─────────────


def test_an_override_that_is_not_json_falls_back_to_the_shipped_palette(psa):
    """A truncated write or a hand-edited object must not take the recommender down: the state
    reports `override_invalid` and serves the shipped catalogue unchanged."""
    palette = psa["palette"]
    palette.get_object_store().put(palette.override_key(), b"not json")
    palette._CACHE = None

    state = palette.load()

    assert state.override_invalid is True
    assert len(state.rows) == len(palette.default_rows())
    assert state.is_override is False


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"[1, 2, 3]", id="a-json-array-rather-than-an-object"),
        pytest.param(b'"a string"', id="a-bare-json-string"),
        pytest.param(b'{"added": {"vendor": "Datwyler"}}', id="added-is-not-a-list"),
        pytest.param(b'{"removed": "Datwyler"}', id="removed-is-not-a-list"),
        pytest.param(b"\xff\xfe not utf-8", id="undecodable-bytes"),
    ],
)
def test_a_structurally_wrong_override_is_rejected_whole(psa, body):
    """Each of these would otherwise reach `_merge` and raise mid-recommendation. Rejecting the
    document whole is what keeps the failure to "the edit was ignored"."""
    palette = psa["palette"]
    palette.get_object_store().put(palette.override_key(), body)
    palette._CACHE = None

    state = palette.load()

    assert state.override_invalid is True
    assert len(state.rows) == len(palette.default_rows())


def test_a_broken_override_still_lets_the_palette_be_read(psa):
    """`cap_colors.palette()` is called per comparator inside `normalize`. If a bad override
    propagated an exception, every assessment on that deployment would fail."""
    palette = psa["palette"]
    palette.get_object_store().put(palette.override_key(), b"{")
    palette._CACHE = None
    psa["cap_colors"]._PALETTE_CACHE = None

    rows = psa["cap_colors"].palette()

    assert rows
    assert psa["cap_colors"].normalize("Blue 6043")["confidence"] == "code"


def test_an_added_row_replaces_a_shipped_row_in_place_rather_than_moving_it(psa):
    """Position matters: with no comparator cap on file every ΔE is None and the
    recommendations are presented in palette order, so an in-place edit must not reorder."""
    palette = psa["palette"]
    shipped = palette.default_rows()
    target = shipped[0]
    key = (target["vendor"], target["vendor_color_name"])

    psa["cap_colors"].add_color(
        target["vendor"], target["vendor_color_name"], "Teal", hex="#008080"
    )

    rows = palette.load().rows
    assert len(rows) == len(shipped), "an upsert must not grow the palette"
    assert (rows[0]["vendor"], rows[0]["vendor_color_name"]) == key
    assert rows[0]["hex"] == "#008080"


def test_an_unreadable_override_object_is_treated_as_absent(psa, monkeypatch):
    """`_read_override_bytes` catches `StorageError`. If it did not, an object-store outage
    would turn every palette read into a 500 rather than a degraded answer."""
    palette = psa["palette"]

    class Broken:
        def exists(self, key):
            return True

        def open(self, key):
            raise psa["storage_base"].StorageError("the store is unreachable")

    monkeypatch.setattr(palette, "get_object_store", lambda: Broken())
    palette._CACHE = None

    state = palette.load()

    assert state.override_invalid is False, "unreadable is 'no override', not 'a bad override'"
    assert len(state.rows) == len(palette.default_rows())


# ── cap_colors.py: the validation and code-normalisation edges ───────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        pytest.param("6043", "6043", id="a-datwyler-code-is-unchanged"),
        pytest.param("L9320", "9320", id="a-west-L-prefix-is-stripped"),
        pytest.param("2063C", "2063", id="a-pantone-C-suffix-is-stripped"),
        pytest.param("l9320c", "9320", id="either-affix-in-lower-case"),
        pytest.param("  6043  ", "6043", id="surrounding-whitespace"),
        pytest.param(None, "", id="a-null-cell"),
    ],
)
def test_a_colour_code_is_canonicalised_across_the_two_suppliers_schemes(psa, code, expected):
    """Datwyler writes 6043, West writes L9320 and Pantone writes 2063C. Comparing them raw is
    how a product's cap silently fails to match its own palette row."""
    assert psa["cap_colors"]._norm_code(code) == expected


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        pytest.param({"vendor": "", "vendor_color_name": "Teal"}, "required", id="no-vendor"),
        pytest.param({"vendor": "Datwyler", "vendor_color_name": " "}, "required", id="blank-name"),
        pytest.param(
            {"vendor": "Datwyler", "vendor_color_name": "Teal", "canonical_color": ""},
            "Canonical",
            id="no-canonical-colour",
        ),
    ],
)
def test_adding_a_colour_without_its_identifying_fields_is_refused_with_a_reason(
    psa, kwargs, fragment
):
    """The screen shows the message. A silent `(False, "")` would leave the user staring at a
    form that did nothing, and a row with no vendor or name could never be removed again."""
    kwargs.setdefault("canonical_color", "Teal")

    ok, message = psa["cap_colors"].add_color(**kwargs)

    assert ok is False
    assert fragment in message
    assert psa["palette"].load().is_override is False, "a refused add must write nothing"


def test_removing_a_colour_without_naming_one_is_refused(psa):
    """The DELETE endpoint passes its query parameters straight through, so an empty name has to
    be caught here rather than removing an arbitrary row."""
    ok, message = psa["cap_colors"].remove_color("Datwyler", "")

    assert ok is False
    assert "Pick a colour" in message


def test_a_palette_row_marked_not_off_the_shelf_is_enriched_as_such(psa):
    """`off_the_shelf` arrives from the CSV as the string '0' or '1', and a plain truthiness
    test on '0' would mark every custom colour as stock."""
    enriched = psa["cap_colors"]._enrich({"off_the_shelf": "0", "vendor_code": "L9320"})

    assert enriched["off_the_shelf"] is False
    assert enriched["_code_norm"] == "9320"
    assert enriched["sizes"] == [], "an absent sizes_mm is an empty list, not None"


@pytest.mark.parametrize(
    "sizes_mm,expected",
    [
        pytest.param("13;20", ["13", "20"], id="semicolon-separated"),
        pytest.param("13,20", ["13", "20"], id="comma-separated"),
        pytest.param("", [], id="not-transcribed-yet"),
    ],
)
def test_the_stated_diameters_are_split_on_either_separator(psa, sizes_mm, expected):
    """The CSV is hand-maintained and uses both. Failing to split one would make the whole
    field a single unmatchable token and drop every row from `available_for`."""
    assert psa["cap_colors"]._enrich({"sizes_mm": sizes_mm})["sizes"] == expected


def test_the_cap_colours_demo_prints_the_palette_summary_and_a_normalisation_sample(psa, capsys):
    """The documented spot-check for the normaliser. It runs `normalize` over the real sheet's
    spellings, so a crash here means the whole cap layer is unusable from the command line."""
    psa["cap_colors"]._demo()

    out = capsys.readouterr().out
    assert "rows total" in out
    assert "Datwyler pp_disc=" in out and "West pp_disc=" in out
    assert "Normalisation of sample free-text values:" in out
    assert "'Blue 6016'" in out
    assert "[CUSTOM]" in out, "the Magenta 2063C sample must be flagged"
    assert "[unknown]" in out, "the TBD sample must be flagged"
    # The ΔE sanity check at the end: close must print smaller than far.
    close = float(out.split("(close):")[1].split("\n")[0])
    far = float(out.split("(far):")[1].split("\n")[0])
    assert close < far


# ── storage/base.py: the object-store port ───────────────────────────────────


def test_the_object_store_protocol_is_structural_rather_than_inherited(psa):
    """It is a runtime-checkable Protocol, so a local or an S3 implementation satisfies it
    without importing it. A nominal ABC would force every backend to subclass."""
    base = psa["storage_base"]

    class Enough:
        def put(self, key, data):
            return 0

        def open(self, key):
            raise base.StorageError(key)

        def exists(self, key):
            return False

        def delete(self, key):
            return None

    assert isinstance(Enough(), base.ObjectStore)


def test_an_incomplete_implementation_is_not_an_object_store(psa):
    """The isinstance check is what a host uses to reject a half-written backend, so a class
    missing `delete` must not pass it."""

    class NotEnough:
        def put(self, key, data):
            return 0

        def open(self, key):
            raise RuntimeError

    assert not isinstance(NotEnough(), psa["storage_base"].ObjectStore)


def test_a_storage_error_is_a_runtime_error(psa):
    """Callers that only guard `except Exception` still catch it, and `except RuntimeError`
    around a store call is meant to work — so the base class is part of the contract."""
    assert issubclass(psa["storage_base"].StorageError, RuntimeError)


@pytest.mark.parametrize(
    "prefix,relative,expected",
    [
        ("psa", "palette/x.json", "psa/palette/x.json"),
        ("/psa/", "/palette/x.json/", "psa/palette/x.json"),
        ("psa", "x.json", "psa/x.json"),
    ],
)
def test_an_asset_key_is_normalised_to_a_single_slash(psa, prefix, relative, expected):
    """A key is the identity of an object. `psa//palette/x` and `psa/palette/x` addressing two
    different blobs is how an override becomes invisible after a caller adds a slash."""
    assert psa["storage_base"].asset_key(prefix, relative) == expected


# ── aria.py: the platform-hosted config ──────────────────────────────────────


def test_the_aria_config_tolerates_a_missing_credential_when_asked_to(psa, monkeypatch):
    """The router builds leniently so `GET /health` can REPORT `smartsheet_live: false`. Raising
    instead would 500 the one endpoint an operator uses to diagnose the missing token."""
    aria = psa["aria"]
    from api.backend.da_platform import credentials

    def missing():
        raise credentials.MissingCredential("no smartsheet token")

    monkeypatch.setattr(credentials, "smartsheet_credentials", missing)

    cfg = aria.build(require_credentials=False)

    assert cfg.smartsheet_token is None
    assert cfg.smartsheet_sheet_id is None
    # Even without a credential the PATHS must be redirected out of the source tree.
    assert cfg.db_path.startswith(aria._scratch_dir())
    assert cfg.ingest_source == "api"


def test_a_stage_still_fails_loudly_when_the_credential_is_absent(psa, monkeypatch):
    """`fetch` exists to read Smartsheet; running it tokenless can only produce a broken run, so
    the strict build must propagate rather than quietly configure a dead source."""
    aria = psa["aria"]
    from api.backend.da_platform import credentials

    def missing():
        raise credentials.MissingCredential("no smartsheet token")

    monkeypatch.setattr(credentials, "smartsheet_credentials", missing)

    with pytest.raises(credentials.MissingCredential):
        aria.build(require_credentials=True)


def test_the_scratch_directory_is_one_per_process(psa):
    """Each stage rebuilds the database from its own snapshot before reading it, which is only
    safe because both stages of a run share this directory."""
    aria = psa["aria"]

    first = aria._scratch_dir()

    assert os.path.isdir(first)
    assert aria._scratch_dir() == first


def test_installing_is_idempotent_while_a_credential_is_present(psa, psa_config):
    """`install()` sits at the top of every stage. Rebuilding each time would hand out a fresh
    scratch directory and the stage would read an empty database."""
    aria = psa["aria"]
    assert psa["config"].installed()

    first = aria.install(require_credentials=False)
    second = aria.install(require_credentials=False)

    assert first is second
    # The already-installed test config wins: install must not overwrite injected paths.
    assert first.db_path == psa["config"].config().db_path


def test_a_lenient_install_does_not_satisfy_a_later_strict_one(psa, monkeypatch):
    """Idempotence is keyed on whether the installed config HAS credentials, not on a "have I
    run" flag — otherwise a router request would let a later stage run tokenless and silent."""
    aria = psa["aria"]
    from api.backend.da_platform import credentials

    monkeypatch.setattr(psa["config"], "_INJECTED", None)
    monkeypatch.setattr(
        credentials,
        "smartsheet_credentials",
        lambda: (_ for _ in ()).throw(credentials.MissingCredential("none")),
    )

    lenient = aria.install(require_credentials=False)
    assert lenient.smartsheet_token is None

    with pytest.raises(credentials.MissingCredential):
        aria.install(require_credentials=True)


# ── router.py: configuring the API process ───────────────────────────────────


def test_the_configuration_dependency_returns_early_when_a_host_already_configured(psa):
    """The cheap first branch. If it did not return, a test that injected its own paths would
    have them replaced by ARIA's scratch directory on the first request."""
    from da_silos.psa import router as psa_router

    before = psa["config"].config().db_path

    psa_router._ensure_configured()

    assert psa["config"].config().db_path == before


def test_the_configuration_dependency_swallows_an_absent_platform(psa, monkeypatch):
    """Someone importing this router directly has no `da_platform`, and the repo defaults are
    the correct answer for them — an ImportError here would 500 every endpoint instead."""
    from da_silos.psa import aria, router as psa_router

    monkeypatch.setattr(psa["config"], "_INJECTED", None)
    monkeypatch.setattr(
        aria, "install", lambda **kw: (_ for _ in ()).throw(ImportError("no da_platform"))
    )

    psa_router._ensure_configured()  # must not raise

    assert psa["config"].installed() is False, "nothing was configured, and that is the answer"


def test_the_configuration_dependency_installs_the_aria_config_on_a_first_request(
    psa, monkeypatch
):
    """Inside ARIA nothing configures the API process, so without this a single request would
    have created `da-backend/silos/Database/psa.db` in the source tree."""
    from da_silos.psa import aria, router as psa_router

    monkeypatch.setattr(psa["config"], "_INJECTED", None)
    called = {}

    def fake_install(*, require_credentials=True):
        called["require_credentials"] = require_credentials
        return None

    monkeypatch.setattr(aria, "install", fake_install)

    psa_router._ensure_configured()

    assert called == {"require_credentials": False}, "/health must report a missing token, not 500"


def test_the_run_endpoints_are_mounted_when_the_platform_is_present(psa):
    """`runs.py` needs `da_platform`, and the ImportError fallback leaves the three run routes
    simply absent. Under ARIA they must be there, or a run can never be queued."""
    from da_silos.psa import router as psa_router

    # `include_router` on this fastapi version keeps the sub-router as one opaque entry rather
    # than flattening its routes, so the sub-router itself is what has to be inspected.
    assert hasattr(psa_router, "_runs_router"), "the ImportError fallback swallowed runs.py"
    sub = {getattr(r, "path", "") for r in psa_router._runs_router.routes}

    assert "/runs" in sub, sorted(sub)
    assert "/runs/{run_id}/result" in sub, sorted(sub)


# ── build_db.py, paths.py, __init__.py ───────────────────────────────────────


def test_building_the_database_reports_what_it_created(psa, capsys):
    """First step of every run and the only place the schema is applied — a silent build gives
    an operator nothing to check when a later stage finds no tables."""
    psa["build_db"].main()

    out = capsys.readouterr().out
    assert f"Created {psa['paths'].db_path()}" in out
    assert "template_field_map rows:" in out
    assert " 0\n" not in out.split("template_field_map rows:")[-1], "the map must not be empty"


def test_rebuilding_replaces_the_previous_database_rather_than_appending(psa):
    """`main` removes the file first. Re-running it without that would double every
    `template_field_map` row and the populator would write each cell twice."""
    build_db, paths = psa["build_db"], psa["paths"]

    def count():
        # Closed explicitly rather than with a `with` block: `sqlite3.connect` as a context
        # manager commits but does NOT close, and on Windows an open handle makes the
        # `os.remove` inside `main()` raise PermissionError.
        con = sqlite3.connect(paths.db_path())
        try:
            return con.execute("SELECT count(*) FROM template_field_map").fetchone()[0]
        finally:
            con.close()

    build_db.main()
    first = count()
    build_db.main()
    second = count()

    assert first == second == len(build_db.TEMPLATE_FIELDS)


def test_the_named_locations_all_sit_under_the_configured_workspace(psa, psa_config):
    """`config.py` is the only place configuration is read. A path accessor that ignored the
    injected config would write into the source tree from a test or a deployed silo."""
    paths = psa["paths"]

    assert paths.db_path() == psa_config.db_path
    assert paths.output_dir() == psa_config.output_dir
    assert paths.uploads_dir() == psa_config.uploads_dir
    assert paths.image_dir().startswith(psa_config.output_dir)


def test_ensuring_the_directories_creates_every_writable_location(psa, psa_config):
    """Called on every run: a missing output directory turns report generation into an
    IOError halfway through, after the database work has already been done."""
    paths = psa["paths"]

    paths.ensure_dirs()

    assert os.path.isdir(os.path.dirname(paths.db_path()))
    assert os.path.isdir(paths.output_dir())
    assert os.path.isdir(paths.uploads_dir())
    assert os.path.isdir(paths.image_dir())


def test_the_package_init_imports_nothing_so_the_cap_layer_stays_standalone(psa):
    """"Nothing is imported here on purpose": an `import` in `__init__.py` would mean
    `import da_silos.psa.cap_colors` dragged in fastapi, and the cap layer would stop being
    usable from a plain script."""
    source = (SILO_DIR / "__init__.py").read_text(encoding="utf-8")

    assert "Product Similarity Assessment" in source
    code = [
        line for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    # The whole file is one docstring: the only non-comment lines are its own two delimiters.
    assert [line for line in code if line.strip().startswith('"""')] == code[:1] + code[-1:]
    assert len(code) > 2, "the docstring is the file; nothing else may be in it"
    assert not any(
        line.startswith(("import ", "from ")) for line in code
    ), "no top-level import may appear in the package __init__"


def test_the_silo_loader_registers_the_package_as_a_namespace(psa):
    """`_load_module` synthesises `da_silos.psa` from the directory rather than executing its
    `__init__.py`, which is why nothing in that file can be relied on at runtime — including its
    docstring. Recording the ACTUAL behaviour: `__doc__` is None and `__file__` is absent.

    This looks like a latent trap rather than a bug today (the file is only a docstring), but a
    future `__init__.py` that defined a constant would silently not exist for importers.
    """
    import da_silos.psa as pkg

    assert pkg.__doc__ is None
    assert getattr(pkg, "__file__", None) is None
    assert str(SILO_DIR) in [str(p) for p in pkg.__path__]
