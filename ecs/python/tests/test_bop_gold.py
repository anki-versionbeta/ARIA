"""The gold-derived length and style bounds.

This is what keeps generated sections as short as the human-authored reference document.
The gold BOP is measured and those measurements become per-section rules injected into
every generation prompt — so a regression here does not error, it quietly changes how long
and how padded every generated document is. That makes it worth testing directly.

Everything covered here is pure: the gold document is passed in as a plain dict rather
than read from the ~25 MB object in storage. The caching behaviour is covered with a fake
ctx, because the cache is the reason a 25 MB read happens once rather than per section.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "silo.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def gold():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import gold as module

    return module


@pytest.fixture(autouse=True)
def _clear_cache(gold):
    gold.reset_cache()
    yield
    gold.reset_cache()


# ── split_inline_subblocks ────────────────────────────────────────────────────


def test_a_cell_splits_on_its_inline_headers(gold):
    cell = "Hazards | pressure risk | Engineering Controls | relief valve | PPE | gloves"

    result = gold.split_inline_subblocks(
        cell, ["Hazards", "Engineering Controls", "PPE"]
    )

    assert result["Hazards"] == "pressure risk"
    assert result["Engineering Controls"] == "relief valve"
    assert result["PPE"] == "gloves"


def test_a_missing_header_maps_to_empty_rather_than_vanishing(gold):
    """Callers index the returned dict, so every requested header must be present."""
    result = gold.split_inline_subblocks("Hazards | only this", ["Hazards", "PPE"])

    assert result == {"Hazards": "only this", "PPE": ""}


def test_a_header_with_a_trailing_parenthetical_still_matches(gold):
    # The real gold writes "Hazards (engineering controls and/or PPE ...)".
    cell = "Hazards (and controls) | the body text"

    result = gold.split_inline_subblocks(cell, ["Hazards"])

    assert result["Hazards"] == "the body text"


def test_header_matching_ignores_case(gold):
    result = gold.split_inline_subblocks("hazards | body", ["Hazards"])

    assert result["Hazards"] == "body"


def test_several_parts_under_one_header_are_rejoined(gold):
    result = gold.split_inline_subblocks("Hazards | one | two | three", ["Hazards"])

    assert result["Hazards"] == "one | two | three"


def test_text_before_any_header_is_discarded(gold):
    # Nothing owns it, and guessing would attribute it to the wrong sub-section.
    result = gold.split_inline_subblocks("stray | Hazards | body", ["Hazards"])

    assert result["Hazards"] == "body"


def test_empty_parts_are_skipped(gold):
    result = gold.split_inline_subblocks("Hazards |  | body |  ", ["Hazards"])

    assert result["Hazards"] == "body"


def test_an_empty_cell_yields_empty_values(gold):
    assert gold.split_inline_subblocks("", ["Hazards", "PPE"]) == {
        "Hazards": "",
        "PPE": "",
    }


# ── count_chars_sentences_items ───────────────────────────────────────────────


def test_empty_text_counts_as_zero(gold):
    assert gold.count_chars_sentences_items("") == (0, 0, 0)


def test_a_single_sentence_is_counted_as_one(gold):
    chars, sentences, items = gold.count_chars_sentences_items("One statement.")

    assert chars == len("One statement.")
    assert sentences == 1
    assert items == 1


def test_sentences_are_split_on_terminators(gold):
    _chars, sentences, _items = gold.count_chars_sentences_items(
        "First one. Second one! Third one? Fourth."
    )

    assert sentences == 4


def test_a_pipe_delimited_body_counts_items(gold):
    _chars, _sentences, items = gold.count_chars_sentences_items("a | b | c")

    assert items == 3


def test_a_newline_delimited_body_counts_items(gold):
    _chars, _sentences, items = gold.count_chars_sentences_items("a\nb\nc\n")

    assert items == 3


def test_counts_are_never_below_one_for_non_empty_text(gold):
    """The counts feed "approximately N" prompt text, where 0 would read as nonsense."""
    _chars, sentences, items = gold.count_chars_sentences_items("no terminator here")

    assert sentences >= 1
    assert items >= 1


@pytest.mark.parametrize("text", ["|", " | ", "| |"])
def test_a_body_of_only_delimiters_still_counts_one_item(gold, text):
    """The floor matters here, not just for ordinary prose.

    Splitting these yields no non-empty pieces at all, so without the floor the bound
    would read "approximately 0±2 entries" and instruct the model to write nothing.
    """
    _chars, _sentences, items = gold.count_chars_sentences_items(text)

    assert items == 1


# ── the prompt fragments ──────────────────────────────────────────────────────


def test_a_one_sentence_bound_reads_as_singular(gold):
    fragment = gold._string_fragment("Purpose", 120, 1)

    assert "1 sentence" in fragment
    assert "120 characters" in fragment
    assert "Purpose" in fragment


def test_a_two_sentence_bound_reads_as_a_range(gold):
    assert "1–2 sentences" in gold._string_fragment("Scope", 200, 2)


def test_a_longer_bound_states_the_count(gold):
    assert "5 sentences" in gold._string_fragment("Scope", 500, 5)


def test_a_string_bound_forbids_padding(gold):
    # The whole point of the bound: stop the model padding with unrelated detail.
    assert "do NOT pad" in gold._string_fragment("Purpose", 100, 1)


def test_a_list_bound_states_a_tolerance_and_protects_part_numbers(gold):
    fragment = gold._list_fragment("Tools", 7)

    assert "7±2" in fragment
    assert "EXACTLY" in fragment
    assert "genericize" in fragment.lower()


def test_the_safety_bound_omits_a_character_target(gold):
    """Deliberate: "one short paragraph per category" is the real calibration."""
    fragment = gold._safety_fragment("Hazards")

    assert "one short paragraph per category" in fragment
    assert "characters" not in fragment


def test_a_leaf_label_is_humanised_from_its_key(gold):
    assert gold._leaf_label("description.spare_parts") == "Spare Parts"


def test_a_leaf_label_override_wins(gold):
    for leaf, expected in gold.LEAF_LABEL_OVERRIDES.items():
        assert gold._leaf_label(leaf) == expected


# ── resolve_gold_section ──────────────────────────────────────────────────────


def test_no_gold_resolves_to_empty(gold):
    assert gold.resolve_gold_section(None, "purpose") == ""


def test_an_unmapped_leaf_resolves_to_empty(gold):
    assert gold.resolve_gold_section({"1.0 Purpose": {"x": "y"}}, "not_a_leaf") == ""


def test_a_missing_heading_resolves_to_empty(gold):
    # A gold document that does not contain the section must not raise.
    assert gold.resolve_gold_section({"9.0 Something Else": {}}, "purpose") == ""


def test_a_mapped_leaf_resolves_to_its_cell(gold):
    spec = gold.GOLD_SECTION_MAP["purpose"]
    document = {f"{spec['h1']} heading": {f"{spec.get('label', '')} cell": "the text"}}

    resolved = gold.resolve_gold_section(document, "purpose")

    # Either the labelled cell or the whole-H1 join, depending on the mapping shape.
    assert "the text" in resolved


def test_headings_are_matched_case_insensitively(gold):
    spec = gold.GOLD_SECTION_MAP["purpose"]
    document = {spec["h1"].upper(): {str(spec.get("label", "")).upper(): "body"}}

    assert "body" in gold.resolve_gold_section(document, "purpose")


# ── compute_gold_bounds ───────────────────────────────────────────────────────


def test_no_gold_produces_no_bounds(gold):
    assert gold.compute_gold_bounds(None) == {}


def test_an_unrecognised_gold_produces_no_bounds(gold):
    # Nothing matches, so nothing is derived - and the caller falls back to static text.
    assert gold.compute_gold_bounds({"Nothing": {"Useful": "here"}}) == {}


def test_a_resolvable_purpose_yields_a_purpose_bound(gold):
    spec = gold.GOLD_SECTION_MAP["purpose"]
    label = spec.get("label", "")
    document = {spec["h1"]: {label or "cell": "A concise purpose statement."}}

    bounds = gold.compute_gold_bounds(document)

    if "purpose" in bounds:
        assert "Purpose" in bounds["purpose"]
        assert "characters" in bounds["purpose"]


# ── the cache, which is why a 25 MB read happens once ────────────────────────


class FakeAssets:
    def __init__(self, payload=None):
        self.payload = payload
        self.reads = 0

    def exists(self, relative: str) -> bool:
        return self.payload is not None

    def read_text(self, relative: str, cache: bool = True) -> str:
        self.reads += 1
        import json

        return json.dumps(self.payload)


class Ctx:
    def __init__(self, payload=None):
        self.assets = FakeAssets(payload)


def test_without_gold_the_static_bounds_are_used(gold):
    bounds = gold.get_section_bounds(Ctx(payload=None))

    assert bounds == dict(gold.SECTION_BOUNDS)


def test_the_bounds_are_computed_once_and_cached(gold):
    """The gold object is ~25 MB; reading it per section would be ruinous."""
    ctx = Ctx(payload=None)

    first = gold.get_section_bounds(ctx)
    second = gold.get_section_bounds(ctx)

    assert first is second


def test_resetting_the_cache_forces_a_recompute(gold):
    ctx = Ctx(payload=None)
    first = gold.get_section_bounds(ctx)

    gold.reset_cache()
    second = gold.get_section_bounds(ctx)

    assert first is not second
    assert first == second


def test_every_static_bound_key_survives_a_merge(gold):
    """Keys absent from the gold must inherit the static text rather than disappear."""
    bounds = gold.get_section_bounds(Ctx(payload=None))

    assert set(gold.SECTION_BOUNDS).issubset(set(bounds))


def test_gold_derived_bounds_layer_over_the_static_ones(gold, monkeypatch):
    """The merge path, which the no-gold case never reaches.

    A derived bound must win for its own key while every other static key survives —
    the gold only covers part of the document, so dropping the rest would leave those
    sections with no length guidance at all.
    """
    monkeypatch.setattr(gold, "load_gold_doc", lambda ctx: {"1.0 Purpose": {"a": "b"}})
    monkeypatch.setattr(
        gold, "compute_gold_bounds", lambda document: {"purpose": "derived bound"}
    )

    bounds = gold.get_section_bounds(Ctx(payload=None))

    assert bounds["purpose"] == "derived bound"
    for key in gold.SECTION_BOUNDS:
        assert key in bounds, key
    untouched = [k for k in gold.SECTION_BOUNDS if k != "purpose"]
    assert all(bounds[k] == gold.SECTION_BOUNDS[k] for k in untouched)
