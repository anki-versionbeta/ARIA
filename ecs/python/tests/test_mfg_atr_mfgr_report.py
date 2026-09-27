"""MFGR report shaping.

Characterisation tests, not specification: `mfgr_report.py` is byte-identical to the source
apart from one import line, so these pin the behaviour that already exists rather than
proposing what it should be. Every case below was read off the source's own rules and
docstrings, so a future edit that changes the output fails here first.

Pure functions over row dicts — no database, no filesystem, no environment.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

SILO_DIR = BACKEND_ROOT / "silos" / "mfg_atr"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "mfgr_report.py").is_file(), reason="the MFGR pipeline is not present"
)

MILLIL = "http://qudt.org/vocab/unit/MilliL"
MG_PER_ML = "http://qudt.org/vocab/unit/MilliGM-PER-MilliL"
FILLING_URI = "https://ontology.abbvienet.com/dsdt/DSDT_0000360"


@pytest.fixture(scope="module")
def mfgr():
    _load_module("mfg_atr", SILO_DIR / "silo.py")
    _load_module("mfg_atr", SILO_DIR / "mfgr_config.py", name="mfgr_config")
    return _load_module("mfg_atr", SILO_DIR / "mfgr_report.py", name="mfgr_report")


def stage_row(**over):
    row = {
        "batch_id_stage": "nest-br-prod-BAX000584-01",
        "batch_name": "ABBV-423 360 mg S.INJ 2 mL Vial",
        "project_code": "ABBV-423",
        "batch_type": "Clinical",
        "eln_url": "https://eln.abbvie.com/e?entityId=b988a640214011f18c8d00000a4818a8",
        "mfg_start": "2026-03-01",
        "mfg_stop": "2026-03-02",
        "manufactured_by": "AbbVie Ludwigshafen",
        "batch_size": "500",
        "batch_size_unit": MILLIL,
        "dose_form": "Solution",
        "dose_strength": "180",
        "dose_strength_unit": MG_PER_ML,
        "density": "1.02",
        "density_unit": "http://qudt.org/vocab/unit/GM-PER-MilliL",
        "ph": "6.0",
        "packaging_system": "6R Vial",
        "unit_operation_uri": FILLING_URI,
        "amount": "500",
        "amount_unit": MILLIL,
    }
    row.update(over)
    return row


def raw_for(**over):
    base = {
        "header": [stage_row()],
        "general_info": [stage_row()],
        "composition": [
            {
                "component_name": "ABBV-423",
                "component_function": "Active",
                "amount": "180",
                "amount_unit": MG_PER_ML,
            }
        ],
        "batch_components": [],
        "process_steps": [],
        "material_props": [],
        "quality_standard": [],
    }
    base.update(over)
    return base


# ── ontology decoding, and the review flag it raises ──────────────────────────


def test_a_known_unit_uri_decodes_confidently(mfgr):
    assert mfgr.decode_unit(MILLIL) == ("mL", True)
    assert mfgr.decode_unit(MG_PER_ML) == ("mg/mL", True)


def test_an_unknown_unit_uri_is_returned_raw_and_unconfident(mfgr):
    """Reported rather than guessed — the raw URI reaches the document and a flag reaches
    the author."""
    assert mfgr.decode_unit("urn:unit:nope") == ("urn:unit:nope", False)


def test_no_unit_is_confidently_empty(mfgr):
    """An absent unit is not a decode failure, so it must not raise a flag."""
    assert mfgr.decode_unit(None) == ("", True)
    assert mfgr.decode_unit_op(None) == ("", True)


def test_a_known_unit_operation_uri_decodes(mfgr):
    assert mfgr.decode_unit_op(FILLING_URI) == ("Filling", True)


def test_an_undecoded_unit_operation_becomes_a_review_flag(mfgr):
    """The DSDT ontology is owned by the client and incomplete. This is the mechanism that
    surfaces the gap to a human instead of silently accepting it.

    Characterisation of a deliberate behaviour change, not of a bug. This test previously
    asserted that the raw URI was carried into `unit_operation`, so a reviewer would see
    it in the report. `_build_stages` now prefers `HEURISTIC_STAGE_NAMES[stage_index]`
    (`mfgr_config.py:102`) in exactly this situation, so stage 1 reads "Thawing and
    Pooling" instead. The gap is still surfaced -- `confident` stays False and the flag
    names both the raw URI and the fact that a heuristic label was substituted -- so a
    reviewer gets strictly more information than before, just not in this field.

    Worth knowing: the heuristic names are ordered by the *typical* rep-batch sequence, so
    on an atypical batch a stage can carry a plausible-looking but wrong label. The
    `confident is False` assertion is what keeps that from being invisible.
    """
    raw = raw_for(general_info=[stage_row(unit_operation_uri="https://ontology.abbvienet.com/dsdt/DSDT_9999999")])

    report = mfgr.build_mfgr_report("BAX000584", raw)

    flag = next(
        (f for f in report.flags if "unit-operation URI not decoded" in f.message), None
    )
    assert flag is not None, [f.message for f in report.flags]
    assert "DSDT_9999999" in flag.message
    assert "heuristic label applied" in flag.message
    assert report.stages[0].confident is False
    assert report.stages[0].unit_operation == "Thawing and Pooling"


def test_an_undecoded_unit_uri_flags_the_batch_size(mfgr):
    raw = raw_for(general_info=[stage_row(batch_size_unit="urn:unit:nope")])

    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert any("Batch-size unit URI is not decoded" in f.message for f in report.flags)


def test_a_process_step_label_beats_the_uri_and_raises_no_flag(mfgr):
    """BATCH_PROCESS_STEPS.STAGE is readable and needs no ontology, so it wins — and an
    undecodable URI then costs nothing."""
    raw = raw_for(
        general_info=[stage_row(unit_operation_uri="https://ontology.abbvienet.com/dsdt/DSDT_9999999")],
        process_steps=[
            {"stage_id": "nest-br-prod-BAX000584-01", "unit_operation": "Sterilization by Filtration"}
        ],
    )

    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert report.stages[0].unit_operation == "Sterilization by Filtration"
    assert report.stages[0].confident is True
    assert not any("unit-operation URI not decoded" in f.message for f in report.flags)


# ── the header row merge ──────────────────────────────────────────────────────


def test_null_columns_are_backfilled_from_sibling_stage_rows(mfgr):
    """Different per-stage rows populate different columns: the mfg window is on the fill
    stage, the formulation attributes on the first. Returning one raw row loses half."""
    formulation = stage_row(mfg_start=None, mfg_stop=None, packaging_system="6R Vial", project_code=None)
    fill = stage_row(packaging_system=None, batch_name=None, project_code="ABBV-423")

    merged = mfgr._pick_header_row([formulation, fill])

    assert merged["mfg_start"] == "2026-03-01", "the mfg-window row is the base"
    assert merged["packaging_system"] == "6R Vial", "overlaid from the sibling"


def test_the_first_row_is_used_when_no_stage_has_a_mfg_window(mfgr):
    rows = [stage_row(mfg_start=None, mfg_stop=None, project_code="FIRST")]
    assert mfgr._pick_header_row(rows)["project_code"] == "FIRST"


def test_no_rows_gives_an_empty_header(mfgr):
    assert mfgr._pick_header_row([]) == {}


# ── the title line and the eLN id ─────────────────────────────────────────────


def test_the_title_line_is_assembled_from_the_parsed_batch_name(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for())

    assert report.header.title_line == "360 mg S.INJ 2 mL in Vial"
    assert report.header.dose_form_label == "Solution for Injection"


def test_the_eln_experiment_id_is_parsed_from_the_url(mfgr):
    """§10 References must show the experiment id, not just the URL."""
    report = mfgr.build_mfgr_report("BAX000584", raw_for())

    assert report.header.eln_experiment_id == "b988a640214011f18c8d00000a4818a8"


def test_a_url_without_an_entity_id_yields_no_experiment_id(mfgr):
    raw = raw_for(header=[stage_row(eln_url="https://eln.abbvie.com/notebook")])
    assert mfgr.build_mfgr_report("BAX000584", raw).header.eln_experiment_id == ""


# ── composition ───────────────────────────────────────────────────────────────


def test_per_stage_duplicate_ingredients_are_collapsed(mfgr):
    """`view_formulation_batches` returns one row per component x stage, and the
    LIKE :root||'%' filter matches every stage, so each ingredient repeats N times."""
    row = {
        "component_name": "Sucrose",
        "component_function": "Stabiliser",
        "amount": "80",
        "amount_unit": MG_PER_ML,
    }
    report = mfgr.build_mfgr_report("BAX000584", raw_for(composition=[row, dict(row), dict(row)]))

    assert len(report.composition) == 1
    assert report.composition[0].amount_per_unit == "80 mg/mL"


def test_packaging_and_bulk_solution_rows_are_not_ingredients(mfgr):
    rows = [
        {"component_name": "ABBV-423", "component_function": "Active", "amount": "180", "amount_unit": MG_PER_ML},
        {"component_name": "Bulk", "component_function": "Bulk solution", "amount": "", "amount_unit": ""},
        {"component_name": "Vial", "component_function": "Vial", "amount": "", "amount_unit": ""},
    ]
    report = mfgr.build_mfgr_report("BAX000584", raw_for(composition=rows))

    by_name = {r.component_name: r.is_ingredient for r in report.composition}
    assert by_name == {"ABBV-423": True, "Bulk": False, "Vial": False}


def test_the_quality_standard_is_filled_when_the_warehouse_has_one(mfgr):
    """MANUAL in the mapping doc; AUTO whenever qualityStandardRef is populated."""
    raw = raw_for(
        quality_standard=[{"component_name": "ABBV-423", "quality_standard": "Ph. Eur."}]
    )
    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert report.composition[0].quality_standard == "Ph. Eur."


def test_an_absent_quality_standard_is_blank_rather_than_missing(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for())
    assert report.composition[0].quality_standard == ""


def test_no_formulation_rows_raises_an_info_flag(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for(composition=[]))

    assert any("No formulation rows found" in f.message for f in report.flags)
    assert all(f.severity in ("warn", "info") for f in report.flags)


def test_the_ds_composition_narrative_omits_water_and_non_ingredients(mfgr):
    """Real MFGRs state Water for Injection separately as 'Ad x.xx mL'."""
    rows = [
        {"component_name": "ABBV-423", "component_function": "Active", "amount": "180", "amount_unit": MG_PER_ML},
        {"component_name": "Sucrose", "component_function": "Stabiliser", "amount": "80", "amount_unit": MG_PER_ML},
        {"component_name": "Water for Injection", "component_function": "Solvent", "amount": "", "amount_unit": ""},
    ]
    report = mfgr.build_mfgr_report("BAX000584", raw_for(composition=rows))

    assert report.ds_composition_narrative == "180 mg/mL ABBV-423, 80 mg/mL Sucrose"


# ── amount per unit ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conc,nominal,expected",
    [
        ("180 mg/mL", "2.00", "360 mg"),
        ("1.0 mg/mL", "2", "2 mg"),
        ("180 mg/mL", "", ""),
        ("", "2.00", ""),
        ("not numeric", "2.00", ""),
        ("180 mg/mL", "abc", ""),
    ],
)
def test_amount_per_unit_mg_is_concentration_times_nominal_volume(mfgr, conc, nominal, expected):
    assert mfgr._amount_per_unit_mg(conc, nominal) == expected


def test_the_nominal_volume_drives_both_the_title_and_the_per_unit_amount(mfgr):
    raw = raw_for(
        material_props=[
            {"parameter_ref": "urn:parameter:nominalVolume", "actual_value": "2.00", "actual_value_unit": ""},
            {"parameter_ref": "urn:parameter:fillVolume", "actual_value": "2.37", "actual_value_unit": ""},
        ]
    )
    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert report.header.withdraw_volume_ml == "2.00"
    assert report.general_info.fill_volume == "2.37", "fill volume includes overfill"
    assert report.composition[0].amount_per_unit_mg == "360 mg"


def test_the_fill_volume_stands_in_when_there_is_no_nominal_volume(mfgr):
    raw = raw_for(
        header=[stage_row(batch_name="ABBV-423 360 mg S.INJ Vial")],
        material_props=[
            {"parameter_ref": "urn:parameter:fillVolume", "actual_value": "2.37", "actual_value_unit": ""}
        ],
    )
    assert mfgr.build_mfgr_report("BAX000584", raw).header.withdraw_volume_ml == "2.37"


def test_material_properties_fill_ph_only_when_it_is_missing(mfgr):
    """view_batch_properties wins; MATERIAL_PROPERTIES is the fallback."""
    props = [{"parameter_ref": "urn:parameter:pH", "actual_value": "7.4", "actual_value_unit": ""}]

    kept = mfgr.build_mfgr_report("BAX000584", raw_for(material_props=props))
    filled = mfgr.build_mfgr_report(
        "BAX000584", raw_for(general_info=[stage_row(ph=None)], material_props=props)
    )

    assert kept.general_info.ph == "6.0", "the warehouse column wins"
    assert filled.general_info.ph == "7.4", "the material property fills the gap"


# ── the bulk-solution concentration ───────────────────────────────────────────


def test_the_active_ingredient_sets_the_bulk_concentration(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for())
    assert report.general_info.concentration == "180 mg/mL"


# ── yield ─────────────────────────────────────────────────────────────────────


def test_yield_is_the_last_stage_amount_over_the_first(mfgr):
    raw = raw_for(general_info=[stage_row(amount="500"), stage_row(amount="450")])
    assert mfgr.build_mfgr_report("BAX000584", raw).yield_percent == "90.0%"


def test_a_first_stage_with_no_amount_suppresses_the_yield(mfgr):
    """Characterisation of a deliberate narrowing, replacing an earlier batch-size fallback.

    This test used to assert that a first stage with no `amount` fell back to `batch_size`,
    giving 400/500 = 80.0%. `_compute_yield` no longer consults `batch_size` at all: it
    reads `stages[0].amount` and `stages[-1].amount`, and returns "" unless both parse to a
    number *and* their units belong to the same family.

    That is the safer behaviour, and the docstring says why -- dividing across unit families
    once reported 171.4% for BAX001675, because a PFS count was divided by an mL volume. A
    batch-size fallback reintroduces the same class of error, since `batch_size` and a
    stage `amount` need not share a unit family either. So a blank yield here is correct,
    and the number a reviewer wants instead is the post-VI unit count.
    """
    raw = raw_for(general_info=[stage_row(amount=None, batch_size="500"), stage_row(amount="400")])

    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert report.yield_percent == ""
    # The batch size is still reported; it is only the *yield* that will not use it.
    assert "500" in report.general_info.batch_size


def test_yield_is_blank_rather_than_wrong_when_the_amounts_are_not_numeric(mfgr):
    raw = raw_for(general_info=[stage_row(amount="n/a", batch_size=None), stage_row(amount="450")])
    assert mfgr.build_mfgr_report("BAX000584", raw).yield_percent == ""


# ── components: DS lots versus packaging ──────────────────────────────────────


def test_a_drug_substance_ref_is_classified_as_a_ds_lot(mfgr):
    raw = raw_for(
        batch_components=[
            {
                "display_name": "Drug Substance",
                "component_ref": "urn:batch:idbsnongxp-dev-1429707",
                "resolved_name": None,
            }
        ]
    )
    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert [c.lot for c in report.ds_components] == ["1429707"]
    assert report.packaging == []


def test_a_container_is_classified_as_packaging(mfgr):
    raw = raw_for(
        batch_components=[
            {"display_name": None, "component_ref": "urn:batch:L-0001113",
             "resolved_name": "Vial, Schott, 6R, NBB, clear glass"}
        ]
    )
    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert [c.display_name for c in report.packaging] == ["Vial, Schott, 6R, NBB, clear glass"]
    assert report.ds_components == []


def test_a_prior_stage_intermediate_is_neither(mfgr):
    """nest-br-prod refs are process intermediates — the previous stage's own bulk — not
    materials, so they belong in no table."""
    raw = raw_for(
        batch_components=[
            {"display_name": "Bulk solution vial", "component_ref": "urn:batch:nest-br-prod-BAX000584-01",
             "resolved_name": None}
        ]
    )
    report = mfgr.build_mfgr_report("BAX000584", raw)

    assert report.ds_components == []
    assert report.packaging == []


def test_a_ds_lot_repeated_per_stage_is_deduplicated(mfgr):
    row = {"display_name": "Drug Substance", "component_ref": "urn:batch:idbsnongxp-dev-1429707",
           "resolved_name": None}
    report = mfgr.build_mfgr_report("BAX000584", raw_for(batch_components=[row, dict(row)]))

    assert len(report.ds_components) == 1


def test_no_ds_component_raises_an_info_flag(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for())
    assert any("No DS component_refs matched" in f.message for f in report.flags)


# ── header backfill when batch_name does not parse ────────────────────────────


def test_the_title_pieces_are_derived_from_table_one_when_the_batch_name_is_absent(mfgr):
    """Live data often has batch_name null, and the page header would otherwise show
    <Dose>/<S.INJ>/<Volume>/<Vial> placeholders."""
    rows = [stage_row(batch_name=None, dose_form="Solution", dose_strength="180", packaging_system="6R Vial")]
    report = mfgr.build_mfgr_report("BAX000584", raw_for(header=rows, general_info=rows))

    assert report.header.dose_form_code == "S.INJ"
    assert report.header.dose_form_label == "Solution for Injection"
    assert report.header.container == "Vial"
    assert report.header.dose_mg == "180"


def test_the_withdraw_volume_is_not_guessed_from_stage_amounts(mfgr):
    """Stage amounts are batch/fill sizes (500 mL), not the per-unit withdraw volume — so
    the title must stay blank rather than claim 500 mL."""
    rows = [stage_row(batch_name=None)]
    report = mfgr.build_mfgr_report("BAX000584", raw_for(header=rows, general_info=rows))

    assert report.header.withdraw_volume_ml == ""


def test_the_container_is_inferred_from_component_names_as_a_last_resort(mfgr):
    rows = [stage_row(batch_name=None, packaging_system=None)]
    raw = raw_for(
        header=rows,
        general_info=rows,
        batch_components=[
            {"display_name": None, "component_ref": "urn:batch:L-1", "resolved_name": "Syringe, 1 mL"}
        ],
    )
    assert mfgr.build_mfgr_report("BAX000584", raw).header.container == "Syringe"


# ── registration ──────────────────────────────────────────────────────────────


def test_a_nest_registered_batch_carries_the_namespace_prefix(mfgr):
    report = mfgr.build_mfgr_report("BAX000584", raw_for())

    assert report.registration == "nest"
    assert report.batch_id_query == "nest-br-prod-BAX000584"
    assert report.batch_id_display == "BAX000584"


def test_a_manually_registered_batch_queries_the_bare_id_and_is_flagged(mfgr):
    """BA###### batches are not in the NEST namespace, and the source says so out loud
    rather than silently querying a prefix that cannot match."""
    report = mfgr.build_mfgr_report("BA259821", raw_for())

    assert report.registration == "manual"
    assert report.batch_id_query == "BA259821"
    assert any("manually-registered" in f.message for f in report.flags)


def test_a_stage_suffix_is_stripped_from_the_identifier(mfgr):
    assert mfgr.build_mfgr_report("BA259821-08", raw_for()).batch_id_short == "BA259821"
