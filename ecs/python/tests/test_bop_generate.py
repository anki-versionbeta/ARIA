"""BOP's pure generation helpers: parsing and coercing what the model returns.

These are the functions standing between an LLM's output and the stored document, so they
are the ones that decide whether a slightly-off response becomes a usable section or an
exception. All four are pure, so they are tested directly rather than through a generate
run that would need Iliad.

Loaded through the silo registry, the same way the silo is loaded in production, so the
`da_silos.*` import name is exercised too.
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
def generate():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import generate as module

    return module


# ── parse_json_response ───────────────────────────────────────────────────────


def test_plain_json_is_parsed(generate):
    assert generate.parse_json_response('{"purpose": "text"}') == {"purpose": "text"}


def test_a_json_array_is_parsed(generate):
    assert generate.parse_json_response('["a", "b"]') == ["a", "b"]


def test_a_fenced_block_is_unwrapped(generate):
    """Models routinely wrap JSON in a markdown fence even when told not to."""
    assert generate.parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_a_fence_without_a_language_is_unwrapped(generate):
    assert generate.parse_json_response('```\n{"a": 1}\n```') == {"a": 1}


def test_surrounding_whitespace_is_tolerated(generate):
    assert generate.parse_json_response('\n\n  {"a": 1}  \n') == {"a": 1}


def test_prose_around_the_json_is_discarded(generate):
    """The fallback that salvages a response with a preamble.

    Without it a chatty model costs the whole section.
    """
    text = 'Certainly! Here is the JSON you asked for:\n{"purpose": "x"}\nLet me know.'

    assert generate.parse_json_response(text) == {"purpose": "x"}


def test_prose_around_an_array_is_discarded(generate):
    assert generate.parse_json_response('Here you go: ["one"] done') == ["one"]


def test_nested_objects_survive_the_salvage_path(generate):
    text = 'Sure:\n{"a": {"b": [1, 2]}, "c": "d"}\nthanks'

    assert generate.parse_json_response(text) == {"a": {"b": [1, 2]}, "c": "d"}


def test_none_is_rejected_with_a_named_error(generate):
    with pytest.raises(ValueError, match="text is None"):
        generate.parse_json_response(None)


@pytest.mark.parametrize("text", ["", "   ", "```json\n```", "```\n\n```"])
def test_an_empty_response_is_rejected(generate, text):
    with pytest.raises(ValueError, match="empty after stripping"):
        generate.parse_json_response(text)


def test_a_response_with_no_json_at_all_raises(generate):
    # Better to fail the section loudly than to store a guess.
    with pytest.raises(Exception):
        generate.parse_json_response("I am afraid I cannot help with that.")


# ── looks_truncated ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ['{"a": 1}', '["a"]', '{"a": 1}  \n'])
def test_a_complete_response_does_not_look_truncated(generate, text):
    assert generate.looks_truncated(text) is False


@pytest.mark.parametrize("text", ['{"a": 1', '{"a": "unfinished', "", "   ", None])
def test_an_incomplete_response_looks_truncated(generate, text):
    # This is what triggers the retry at a larger token budget.
    assert generate.looks_truncated(text) is True


# ── _unwrap ───────────────────────────────────────────────────────────────────


def test_the_named_key_is_taken_out(generate):
    assert generate._unwrap({"purpose": "text"}, "purpose") == "text"


def test_a_single_key_response_is_unwrapped_whatever_it_is_called(generate):
    """Models rename the wrapper; one key is unambiguous, so take it."""
    assert generate._unwrap({"Purpose": "text"}, "purpose") == "text"


def test_a_multi_key_response_without_the_named_key_is_returned_whole(generate):
    payload = {"a": 1, "b": 2}

    assert generate._unwrap(payload, "purpose") == payload


def test_a_bare_value_passes_through(generate):
    assert generate._unwrap("text", "purpose") == "text"
    assert generate._unwrap(["a"], "purpose") == ["a"]


def test_the_named_key_wins_over_the_single_key_rule(generate):
    assert generate._unwrap({"purpose": "right"}, "purpose") == "right"


# ── normalize_op_phase ────────────────────────────────────────────────────────


def test_a_paired_phase_keeps_both_groups(generate):
    value = {"equipment": ["e1"], "software": ["s1"]}

    assert generate.normalize_op_phase("setup", value) == value


def test_a_missing_group_becomes_an_empty_list(generate):
    assert generate.normalize_op_phase("setup", {"equipment": ["e1"]}) == {
        "equipment": ["e1"],
        "software": [],
    }


def test_a_scalar_group_is_wrapped_in_a_list(generate):
    # The emitter renders these as bullets, so a bare string must not be iterated
    # character by character.
    assert generate.normalize_op_phase("setup", {"equipment": "one step"}) == {
        "equipment": ["one step"],
        "software": [],
    }


def test_a_bare_list_is_treated_as_equipment(generate):
    assert generate.normalize_op_phase("setup", ["a", "b"]) == {
        "equipment": ["a", "b"],
        "software": [],
    }


def test_a_bare_string_becomes_one_equipment_step(generate):
    assert generate.normalize_op_phase("setup", "single") == {
        "equipment": ["single"],
        "software": [],
    }


@pytest.mark.parametrize("value", [None, "", "   ", 42, True])
def test_anything_unusable_becomes_empty_groups(generate, value):
    # Never None: the emitter and editing.flatten both expect the two keys to exist.
    assert generate.normalize_op_phase("setup", value) == {
        "equipment": [],
        "software": [],
    }


def test_troubleshooting_stays_a_list(generate):
    """Troubleshooting is a table of issue/resolution rows, not a paired phase."""
    rows = [{"issue": "No pressure", "resolution": "Check the seal"}]

    assert generate.normalize_op_phase("troubleshooting", rows) == rows


@pytest.mark.parametrize("value", [None, {}, "text", 7])
def test_troubleshooting_falls_back_to_an_empty_list(generate, value):
    assert generate.normalize_op_phase("troubleshooting", value) == []
