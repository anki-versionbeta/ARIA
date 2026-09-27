"""ATR report shaping.

Characterisation tests, not specification: the port is byte-identical to the source from
its first constant onward, so these pin the behaviour that already exists rather than
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
    not (SILO_DIR / "atr_report.py").is_file(), reason="the ATR pipeline is not present"
)


@pytest.fixture(scope="module")
def report_module():
    _load_module("mfg_atr", SILO_DIR / "silo.py")
    return _load_module("mfg_atr", SILO_DIR / "atr_report.py", name="atr_report")


def result_row(**over):
    row = {
        "experiment_id": "EXP-1",
        "technique": "SoloVPE",
        "result_name": "Protein content",
        "analyte_name": None,
        "sample_id": "S1",
        "sample_name": "Day0",
        "batch_id": "BA00006111",
        "detail_context": None,
        "reported_value": "12.345",
        "value_source": "reported",
        "units": "mg/mL",
        "timepoint": None,
        "temperature": None,
        "particle_size_um": None,
        "timepoint_parsed": None,
        "storage_condition": None,
    }
    row.update(over)
    return row


def raw_for(results, **over):
    base = {
        "header": [
            {"project_code": "ABBV-000", "batch_sample_ids": "BA00006111"}
        ],
        "methods": [
            {
                "eln_unique_id": "EXP-1",
                "test_description": "SoloVPE",
                "test_method_reference": "P-500488-E-B V8.0",
            }
        ],
        "results": results,
        "scope_summary": [{"scope": "Scope text", "summary_conclusion": "Conforms"}],
        "validation": [{"result_rows": len(results)}],
        "batch_lots": [],
    }
    base.update(over)
    return base


# ── value rounding, as documented ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("177.456", "177.46"),   # more than 2 decimals — rounded
        ("0.21345", "0.21"),
        ("0.20", "0.20"),        # already <=2 decimals — exact form preserved
        ("5.8", "5.8"),
        ("42", "42"),            # whole number — untouched
        ("3,111", "3.11"),       # European decimal comma
        (".85", "0.85"),         # leading zero added
        ("-.85", "-0.85"),
        ("Conforms", "Conforms"),  # non-numeric — untouched
        ("", ""),
        (None, ""),
    ],
)
def test_value_rounding(report_module, raw_value, expected):
    assert report_module._round_value(raw_value) == expected


# ── German terms and mojibake ─────────────────────────────────────────────────


def test_german_analytical_terms_become_english(report_module):
    """The finished ATR must be entirely in English even when the source is not."""
    assert report_module.translate_terms("Viskosität") == "Viscosity"
    assert report_module.translate_terms("Sichtbare Partikel") == "Visible Particles"


def test_compound_tokens_translate_on_both_halves(report_module):
    """A plain word boundary would treat '_' as a word character and miss both halves."""
    assert report_module.translate_terms("Dichte_Viskosität") == "Density_Viscosity"


def test_mojibake_is_repaired_before_translating(report_module):
    """UTF-8 mis-decoded as Latin-1 arrives as 'ViskositÃ¤t'."""
    assert report_module.translate_terms("ViskositÃ¤t") == "Viscosity"


def test_correctly_encoded_text_is_left_alone(report_module):
    assert report_module.translate_terms("Appearance") == "Appearance"
    assert report_module.translate_terms(None) == ""


# ── method references ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_ref,expected",
    [
        ("P-500488-E-B V8.0", "P-500488-E-B"),
        ("ARM-19-01273, V 3.0", "ARM-19-01273"),
        ("ARM-20-00231 (v2)", "ARM-20-00231"),
        ("P-500488-E-B", "P-500488-E-B"),
        (None, ""),
    ],
)
def test_method_versions_are_stripped(report_module, raw_ref, expected):
    assert report_module.strip_method_version(raw_ref) == expected


# ── the ambiguous "Compendial" header ─────────────────────────────────────────


def test_compendial_results_are_disambiguated_by_the_parameter(report_module):
    """pH and Clarity share experiment ids, so the parameter is the only discriminator."""
    assert report_module.resolve_result_technique("Compendial", "pH value") == ("pH", True)
    assert report_module.resolve_result_technique("Compendial", "Opalescence NTU") == (
        "Clarity and Opalescence",
        True,
    )


def test_compendial_methods_are_disambiguated_by_the_method_reference(report_module):
    """The methods table has no parameter, so the reference prefix is used instead."""
    assert report_module.resolve_method_technique("Compendial", "P-500488-E-B") == ("pH", True)
    assert report_module.resolve_method_technique("Compendial", "ARM-20-00231") == (
        "Clarity and Opalescence",
        True,
    )


def test_an_unresolvable_compendial_row_is_marked_unconfident(report_module):
    """Reported rather than guessed — it becomes a review flag for a human."""
    technique, confident = report_module.resolve_result_technique("Compendial", "something else")

    assert technique == "Compendial"
    assert confident is False


def test_technique_aliases_are_applied(report_module):
    assert report_module.resolve_result_technique("MFI", "count")[0] == "Sub-Visible Particles - MFI"
    assert report_module.resolve_result_technique("SoloVPE", "content")[0] == "Protein content"


# ── the whole report ──────────────────────────────────────────────────────────


def test_the_report_carries_all_three_id_forms(report_module):
    report = report_module.build_atr_report("CMC-10352", raw_for([result_row()]))

    assert report.request_id_short == "10352"
    assert report.request_id_display == "CMC-10352"
    assert report.request_id_query == "PEGA-PROD-CMC-10352"
    assert report.header["cmc_request_id"] == "10352"
    assert report.header["project_code"] == "ABBV-000"


def test_scope_and_conclusion_come_from_the_summary_row(report_module):
    report = report_module.build_atr_report("10352", raw_for([result_row()]))

    assert report.scope == "Scope text"
    assert report.summary_conclusion == "Conforms"


def test_unmeasured_cells_are_filled_with_na(report_module):
    """§4 is a variable-schema pivot: a sample that never measured a parameter still gets a
    cell, and it must say N/A rather than being blank or absent."""
    results = [
        result_row(sample_id="S1", result_name="Protein content"),
        result_row(sample_id="S2", result_name="Osmolality", units="mOsm/kg"),
    ]

    report = report_module.build_atr_report("10352", raw_for(results))

    assert report.sample_columns == ["S1", "S2"]
    values = {p.label: p.values for tech in report.results for p in tech.params}
    assert len(values) == 2
    for cells in values.values():
        assert set(cells) == {"S1", "S2"}
        assert report_module.NA in cells.values()


def test_a_missing_value_raises_a_review_flag(report_module):
    report = report_module.build_atr_report(
        "10352", raw_for([result_row(reported_value=None)])
    )

    messages = [f.message for f in report.flags]
    assert any("Missing value" in m for m in messages)
    assert all(f.severity in ("warn", "info") for f in report.flags)


def test_a_method_with_no_reference_is_flagged_for_manual_entry(report_module):
    raw = raw_for(
        [result_row()],
        methods=[
            {"eln_unique_id": "EXP-1", "test_description": "SoloVPE", "test_method_reference": None}
        ],
    )

    report = report_module.build_atr_report("10352", raw)

    assert any("No method reference recorded" in f.message for f in report.flags)


def test_analyst_and_date_prefixes_are_stripped_from_the_technique(report_module):
    """§3 and §4 derived the technique differently in the source; the port keeps the
    realignment that drops 'gosekmx_17Apr2026_' style prefixes."""
    raw = raw_for(
        [result_row()],
        methods=[
            {
                "eln_unique_id": "EXP-1",
                "test_description": "gosekmx_17Apr2026_VisibleParticles",
                "test_method_reference": "ARM-1",
            }
        ],
    )

    report = report_module.build_atr_report("10352", raw)

    assert report.methods[0].technique == "VisibleParticles"


def test_eln_ids_are_aggregated_across_experiments_sharing_a_method(report_module):
    raw = raw_for(
        [result_row()],
        methods=[
            {"eln_unique_id": "EXP-2", "test_description": "SoloVPE", "test_method_reference": "P-1 V1.0"},
            {"eln_unique_id": "EXP-1", "test_description": "SoloVPE", "test_method_reference": "P-1 V2.0"},
        ],
    )

    report = report_module.build_atr_report("10352", raw)

    assert len(report.methods) == 1, "same technique and base reference — one row"
    assert report.methods[0].eln_unique_id == "EXP-1, EXP-2"
    assert report.methods[0].method_reference == "P-1"


# ── stability layout ──────────────────────────────────────────────────────────


def test_a_request_without_timepoints_is_not_a_stability_report(report_module):
    report = report_module.build_atr_report("10352", raw_for([result_row()]))
    assert report.is_stability is False


def test_a_parsed_timepoint_makes_it_a_stability_report(report_module):
    report = report_module.build_atr_report(
        "10352", raw_for([result_row(timepoint_parsed="12M", storage_condition="5C")])
    )

    assert report.is_stability is True
    matrix = report.batch_matrices[0]
    assert matrix.lead_headers == ["Sample", "Timepoint", "Storage"]


def test_every_request_gets_a_batch_matrix(report_module):
    """Not only stability ones — the source builds a per-batch matrix universally."""
    report = report_module.build_atr_report("10352", raw_for([result_row()]))

    assert len(report.batch_matrices) == 1
    matrix = report.batch_matrices[0]
    assert matrix.lead_headers == ["Sample"], "no timepoint or storage columns here"
    assert len(matrix.rows) == 1
    assert len(matrix.rows[0]) == len(matrix.lead_headers) + len(matrix.sub_headers)
    assert len(matrix.actual_flags) == len(matrix.rows)


def test_a_value_taken_from_actual_is_flagged_in_the_matrix(report_module):
    """The PDF marks these for manual review, because the reported value was unavailable."""
    report = report_module.build_atr_report(
        "10352", raw_for([result_row(value_source="actual")])
    )

    flags = report.batch_matrices[0].actual_flags[0]
    assert any(flags), "the actual-sourced cell should be flagged"


def test_metadata_rows_are_excluded_from_the_matrix(report_module):
    """EAV bookkeeping rows are not measurements."""
    results = [result_row(), result_row(result_name="analysisid", reported_value="999")]

    report = report_module.build_atr_report("10352", raw_for(results))

    assert not any("analysisid" in h.lower() for h in report.batch_matrices[0].sub_headers)


def test_a_per_volume_duplicate_is_dropped_only_when_per_container_exists(report_module):
    """Sub-visible particle counts are stored twice. Blanket-dropping per-volume made whole
    MFI sections vanish for requests that only record per volume, so the drop is
    conditional on a per-container value for the same measurement."""
    both = [
        result_row(technique="MFI", particle_size_um=10, detail_context="per container", reported_value="5"),
        result_row(technique="MFI", particle_size_um=10, detail_context="per volume", reported_value="7"),
    ]
    only_volume = [
        result_row(technique="MFI", particle_size_um=10, detail_context="per volume", reported_value="7")
    ]

    kept_both = report_module.build_atr_report("10352", raw_for(both)).batch_matrices[0]
    kept_volume = report_module.build_atr_report("10352", raw_for(only_volume)).batch_matrices[0]

    assert kept_both.rows[0][-1] == "5", "per container wins when both exist"
    assert kept_volume.rows[0][-1] == "7", "per volume survives when it is all there is"


# ── batch labels ──────────────────────────────────────────────────────────────


def test_a_real_lot_number_is_shown_with_the_system_id(report_module):
    """PEGA batches carry the DP/SAP lot number used in the manual report."""
    assert (
        report_module._batch_label("pega-prod-BA00006111", "1001707466")
        == "1001707466 (pega-prod-BA00006111)"
    )


def test_a_process_step_name_is_not_mistaken_for_a_lot_number(report_module):
    """NEST/IDBS put a process step in the same column, so only a long run of digits
    counts as a lot."""
    assert report_module._batch_label("BAX000584", "Filling") == "BAX000584"
    assert report_module._batch_label("BAX000584", None) == "BAX000584"


def test_lot_labels_replace_the_raw_batch_list_in_the_header(report_module):
    raw = raw_for(
        [result_row()],
        batch_lots=[{"batch_id": "pega-prod-BA00006111", "lot_display": "1001707466"}],
    )

    report = report_module.build_atr_report("10352", raw)

    assert report.header["batch_sample_id"] == "1001707466 (pega-prod-BA00006111)"


def test_the_header_keeps_the_raw_batch_list_when_no_lots_are_returned(report_module):
    """The captured ATR fixture predates the batch_lots query, so this path must work."""
    raw = raw_for([result_row()])
    del raw["batch_lots"]

    report = report_module.build_atr_report("10352", raw)

    assert report.header["batch_sample_id"] == "BA00006111"
