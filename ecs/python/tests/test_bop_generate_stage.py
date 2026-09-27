"""The generate *stage* of BOP: prompt assembly, the retry, and the fan-out.

Complements `tests/test_bop_generate.py`, which covers the four pure helpers
(`parse_json_response`, `looks_truncated`, `_unwrap`, `normalize_op_phase`) in isolation.
Everything here needs a `ctx`: the two system-prompt builders, the truncation retry, the
two per-task entry points, and `generate()` itself. Nothing is duplicated between the two
files — where a pure helper is touched here it is only as part of a larger path.

Three things shape how these tests are built.

**The prompt IS the product.** `generate.py` is a verbatim port whose whole job is to
concatenate PETRA, a few-shot example, a gold-derived length bound, a style note and a
JSON footer in a fixed order. A refactor that drops one block would not fail any
functional test, so the blocks are asserted by content and by *relative order*.

**The fan-out is keyed off the prompt, not off call order.** Fifteen tasks run in a seven
worker pool, so a positional reply queue would hand a reply to whichever thread won the
race. `KeyedLlm` reads the section/phase key back out of the system prompt it was handed
and answers accordingly, which makes a full `generate()` run deterministic without
pinning the pool to one worker and losing the concurrency being tested.

**The token budgets are the bill.** 4000 on the first attempt, 16000 on the retry, and
`iso_fakes.FakeLlm` records both — so these are asserted as observable numbers rather
than trusted to a constant that a later edit could change on both sides at once.
"""

from __future__ import annotations

import json
import re
import threading

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "generate.py").is_file(), reason="the BOP silo is not present"
)

PETRA = "PETRA SYSTEM PROMPT BODY"


@pytest.fixture(scope="module")
def generate():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import generate as module

    return module


@pytest.fixture(autouse=True)
def _neutral_prompt_inputs(generate, monkeypatch):
    """Blank out the three asset-backed prompt inputs unless a test opts in.

    All three read S3 through `ctx.assets` and memoise into module-global caches, so left
    alone they would make these tests order-dependent and couple them to the SOP template.
    Tests that care about a block install their own value with `with_examples`/`with_bounds`.
    """
    monkeypatch.setattr(generate.examples, "get_template_examples", lambda ctx: {})
    monkeypatch.setattr(generate.gold, "get_section_bounds", lambda ctx: {})
    monkeypatch.setattr(generate.prompts, "load_petra", lambda ctx: PETRA)


def with_examples(generate, monkeypatch, mapping):
    monkeypatch.setattr(generate.examples, "get_template_examples", lambda ctx: mapping)


def with_bounds(generate, monkeypatch, mapping):
    monkeypatch.setattr(generate.gold, "get_section_bounds", lambda ctx: mapping)


# ── test doubles ─────────────────────────────────────────────────────────────


class FakeCtx:
    """Only `llm` and `progress` are reached from this module.

    Shaped like the FakeCtx in `test_bop_ingest.py`; the rest of `StageContext`
    (storage, assets, checkpoint, pause_for_user) is never touched by the generate stage.
    """

    def __init__(self, llm=None):
        self.llm = llm or FakeLlm()
        self.progress_calls = []

    def progress(self, message, pct=None):
        self.progress_calls.append((message, pct))


_KEY_RE = re.compile(r'generate ONLY the "([a-z_]+)" (?:(section)|phase)')


def key_of(system: str) -> tuple[str, str]:
    """Recover which task a system prompt belongs to, from its JSON footer."""
    match = _KEY_RE.search(system)
    assert match, f"no task key in the system prompt: {system[-300:]!r}"
    return match.group(1), "section" if match.group(2) else "phase"


class KeyedLlm:
    """Answers `chat` from the task key embedded in the system prompt it is given.

    Deterministic under the thread pool, unlike a positional queue: the reply is a
    function of the request, so it does not matter which worker gets there first.

    `values` supplies the unwrapped value for a key (wrapped in `{key: value}` on the way
    out), `raw` bypasses that and returns a literal string, `fail` raises, and
    `truncate_once` returns unterminated JSON on the first call for that key only — which
    is how the retry path is driven without also breaking the retry's own reply.
    """

    def __init__(self, *, values=None, raw=None, fail=(), truncate_once=()):
        self.values = dict(values or {})
        self.raw = dict(raw or {})
        self.fail = set(fail)
        self.truncate_once = set(truncate_once)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def chat(self, *, system, user, max_tokens=8000, **kwargs):
        key, kind = key_of(system)
        with self._lock:
            self.calls.append(
                {"key": key, "kind": kind, "max_tokens": max_tokens, "user": user,
                 "system": system, **kwargs}
            )
            first_attempt = sum(1 for call in self.calls if call["key"] == key) == 1

        if key in self.fail:
            raise StubLlmError(f"{key} exploded")
        if key in self.truncate_once and first_attempt:
            return '{"%s": [' % key
        if key in self.raw:
            return self.raw[key]
        if key in self.values:
            return json.dumps({key: self.values[key]})
        return json.dumps({key: _plausible(key, kind)})

    def keys_called(self) -> list[str]:
        return [call["key"] for call in self.calls]


def _plausible(key: str, kind: str):
    """A well-shaped default answer, so a 15-task run needs no per-key setup."""
    if kind == "section":
        return f"{key} content"
    if key == "troubleshooting":
        return [{"issue": f"{key} issue", "resolution": "do the thing"}]
    return {"equipment": [f"{key} equipment step"], "software": [f"{key} software step"]}


def one_reply(text: str) -> FakeLlm:
    return FakeLlm(chat=[text])


# ── _section_system_prompt: the blocks and their order ───────────────────────


def test_the_section_prompt_opens_with_petra(generate):
    # `description` has no style note, so with the asset blocks blanked out the footer is
    # all that follows PETRA -- which pins that PETRA is rstrip'd and each block owns its
    # own leading blank line.
    system = generate._section_system_prompt(FakeCtx(), PETRA + "\n\n", "description", "S")

    assert system.startswith(PETRA + "\n\nFor THIS call")


def test_the_section_prompt_names_the_key_and_embeds_the_schema(generate):
    system = generate._section_system_prompt(FakeCtx(), PETRA, "safety", '"safety": {}')

    assert 'generate ONLY the "safety" section' in system
    assert '{\n  "safety": {}\n}' in system


def test_the_section_prompt_forbids_fabrication_when_the_manual_is_silent(generate):
    """The empty-string instruction is what makes the per-section empty fallback honest."""
    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "do NOT fabricate" in system
    assert "empty strings or empty lists" in system


def test_a_template_example_is_quoted_between_markers(generate, monkeypatch):
    """Unmarked, the example's prose is indistinguishable from PETRA's own instructions."""
    with_examples(generate, monkeypatch, {"purpose": "  The existing purpose text.  "})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "--- TEMPLATE EXAMPLE ---\nThe existing purpose text.\n--- END TEMPLATE" in system


def test_the_example_is_labelled_as_placeholders_not_literal_text(generate, monkeypatch):
    """The template is full of "…"; a model that echoed them would ship them to the SOP."""
    with_examples(generate, monkeypatch, {"purpose": "System … at site …"})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "PLACEHOLDERS" in system
    assert 'Do NOT echo the "…" character' in system


@pytest.mark.parametrize("examples", [{}, {"purpose": ""}, {"purpose": "   "}, {"purpose": None}])
def test_no_usable_example_leaves_the_block_out_entirely(generate, monkeypatch, examples):
    """An empty marker pair would tell the model the template example was blank."""
    with_examples(generate, monkeypatch, examples)

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "TEMPLATE EXAMPLE" not in system


def test_an_example_for_another_section_is_not_borrowed(generate, monkeypatch):
    with_examples(generate, monkeypatch, {"scope": "scope example"})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "scope example" not in system


def test_the_gold_derived_bound_is_presented_as_a_hard_constraint(generate, monkeypatch):
    with_bounds(generate, monkeypatch, {"purpose": "  At most 80 words.  "})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "LENGTH AND STYLE RULES for this section" in system
    # The source wraps this phrase across a line, so compare against unwrapped text.
    assert "hard constraints" in system.replace("\n", " ")
    assert "At most 80 words." in system


@pytest.mark.parametrize("bounds", [{}, {"purpose": ""}, {"purpose": "  "}])
def test_no_bound_leaves_the_length_rules_out(generate, monkeypatch, bounds):
    with_bounds(generate, monkeypatch, bounds)

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "LENGTH AND STYLE RULES" not in system


@pytest.mark.parametrize("key", ["purpose", "scope"])
def test_the_two_html_sections_carry_their_authoritative_style_note(generate, key):
    """purpose/scope are emitted as HTML, so their tag list is not advisory."""
    system = generate._section_system_prompt(FakeCtx(), PETRA, key, "S")

    assert "STRUCTURE AND STYLE for this section" in system
    assert generate.SECTION_STYLE_NOTES[key][:60] in system


@pytest.mark.parametrize("key", ["description", "safety", "abbreviations", "related_documents"])
def test_a_section_with_no_style_note_gets_no_style_block(generate, key):
    assert "STRUCTURE AND STYLE" not in generate._section_system_prompt(
        FakeCtx(), PETRA, key, "S"
    )


def test_the_style_note_is_declared_to_override_the_template_example(generate, monkeypatch):
    """Both blocks describe shape and they can disagree; the tie-break must be stated."""
    with_examples(generate, monkeypatch, {"purpose": "a single flat paragraph"})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "S")

    assert "overriding any differing template example above" in system


def test_the_blocks_appear_in_petra_example_bound_style_footer_order(generate, monkeypatch):
    """Later blocks are meant to win, and only their position says so."""
    with_examples(generate, monkeypatch, {"purpose": "EXAMPLEBLOCK"})
    with_bounds(generate, monkeypatch, {"purpose": "BOUNDBLOCK"})

    system = generate._section_system_prompt(FakeCtx(), PETRA, "purpose", "SCHEMABLOCK")

    positions = [
        system.index(marker)
        for marker in (PETRA, "EXAMPLEBLOCK", "BOUNDBLOCK", "STRUCTURE AND STYLE", "SCHEMABLOCK")
    ]
    assert positions == sorted(positions)


# ── _op_phase_system_prompt ──────────────────────────────────────────────────


def test_the_phase_prompt_names_the_phase_and_its_human_label(generate):
    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "operation", "SCHEMA")

    assert 'generate ONLY the "operation" phase' in system
    assert '("Routine Operation")' in system
    assert "{\n  SCHEMA\n}" in system


def test_an_unknown_phase_falls_back_to_its_own_name_as_the_label(generate):
    """No KeyError: a phase added to the schemas but not the labels still generates."""
    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "decontamination", "S")

    assert '("decontamination")' in system


def test_every_phase_prompt_asks_for_empty_lists_rather_than_invention(generate):
    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "setup", "S")

    assert "return empty lists" in system.replace("\n", " ")
    assert "do NOT fabricate" in system


def test_the_phase_prompt_reuses_the_operating_procedure_example(generate, monkeypatch):
    """There is one template example for the whole procedure, shared by all nine phases."""
    with_examples(generate, monkeypatch, {"operating_procedure": "OP EXAMPLE"})

    for phase in ("setup", "troubleshooting"):
        system = generate._op_phase_system_prompt(FakeCtx(), PETRA, phase, "S")
        assert "OP EXAMPLE" in system


def test_a_phase_specific_example_key_is_not_consulted(generate, monkeypatch):
    with_examples(generate, monkeypatch, {"setup": "SETUP ONLY EXAMPLE"})

    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "setup", "S")

    assert "SETUP ONLY EXAMPLE" not in system
    assert "TEMPLATE EXAMPLE" not in system


@pytest.mark.parametrize(
    "phase", ["setup", "startup", "operation", "shutdown", "emergency_shutdown",
              "cleaning", "storage", "maintenance"]
)
def test_a_paired_phase_gets_the_operating_procedure_bound(generate, monkeypatch, phase):
    with_bounds(generate, monkeypatch, {"operating_procedure": "EQUIPMENT/SOFTWARE RULES"})

    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, phase, "S")

    assert "LENGTH AND STYLE RULES for this phase" in system
    assert "EQUIPMENT/SOFTWARE RULES" in system


def test_troubleshooting_is_excluded_from_the_equipment_software_bound(generate, monkeypatch):
    """Its rows are {issue, resolution}; the paired bound would ask for the wrong shape."""
    with_bounds(generate, monkeypatch, {"operating_procedure": "EQUIPMENT/SOFTWARE RULES"})

    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "troubleshooting", "S")

    assert "EQUIPMENT/SOFTWARE RULES" not in system
    assert "LENGTH AND STYLE RULES" not in system


def test_no_operating_procedure_bound_leaves_the_phase_rules_out(generate, monkeypatch):
    with_bounds(generate, monkeypatch, {"operating_procedure": "   "})

    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "setup", "S")

    assert "LENGTH AND STYLE RULES" not in system


def test_a_phase_prompt_never_carries_a_section_style_note(generate):
    """SECTION_STYLE_NOTES is keyed by section; the phase builder must not reach into it."""
    system = generate._op_phase_system_prompt(FakeCtx(), PETRA, "setup", "S")

    assert "STRUCTURE AND STYLE" not in system


# ── _call_with_truncation_retry: the token budgets ───────────────────────────


def test_a_complete_response_costs_exactly_one_call_at_the_small_budget(generate):
    llm = one_reply('{"purpose": "done"}')
    ctx = FakeCtx(llm)

    result = generate._call_with_truncation_retry(ctx, "SYS", "USER", "section purpose")

    assert result == '{"purpose": "done"}'
    assert [call["max_tokens"] for call in llm.calls] == [generate.SECTION_MAX_TOKENS]
    assert generate.SECTION_MAX_TOKENS == 4000


def test_a_truncated_response_is_retried_at_the_large_budget(generate):
    llm = FakeLlm(chat=['{"purpose": "cut off', '{"purpose": "complete"}'])

    result = generate._call_with_truncation_retry(FakeCtx(llm), "SYS", "USER", "label")

    assert result == '{"purpose": "complete"}'
    assert [call["max_tokens"] for call in llm.calls] == [4000, 16000]
    assert generate.LARGE_SECTION_MAX_TOKENS == 16000


def test_the_retry_sends_the_identical_system_and_user_text(generate):
    """Re-deriving the prompt would let the two attempts diverge; it is reused as-is."""
    llm = FakeLlm(chat=["truncated", '{"a": 1}'])

    generate._call_with_truncation_retry(FakeCtx(llm), "SYS TEXT", "MANUAL TEXT", "label")

    assert [call["system"] for call in llm.calls] == ["SYS TEXT", "SYS TEXT"]
    assert [call["user"] for call in llm.calls] == ["MANUAL TEXT", "MANUAL TEXT"]


def test_there_is_only_ever_one_retry(generate):
    """A response that truncates twice is returned truncated, not retried indefinitely."""
    llm = FakeLlm(chat=["still cut off", "cut off again"])

    result = generate._call_with_truncation_retry(FakeCtx(llm), "SYS", "USER", "label")

    assert result == "cut off again"
    assert len(llm.calls) == 2


def test_the_stage_sets_no_model_temperature_or_timeout_of_its_own(generate):
    """A verbatim port: these are the client's defaults and the port must not shift them."""
    llm = one_reply('{"a": 1}')

    generate._call_with_truncation_retry(FakeCtx(llm), "SYS", "USER", "label")

    assert llm.calls[0]["model"] is None
    assert llm.calls[0]["temperature"] is None
    assert llm.calls[0]["timeouts"] == (120, 240)


def test_a_fenced_but_complete_response_still_pays_for_a_retry(generate):
    """CHARACTERIZATION of a suspected defect at generate.py:166.

    `looks_truncated` only looks at the last character, and a markdown fence ends in a
    backtick — so a complete, parseable ```json block is diagnosed as truncated and the
    section is billed a second 16000-token call it does not need. `parse_json_response`
    exists precisely because models fence "even when told not to", so this is the common
    case, not an edge one. Correct behaviour would be to strip fences before the check.
    """
    llm = FakeLlm(chat=['```json\n{"purpose": "complete"}\n```', '{"purpose": "complete"}'])

    generate._call_with_truncation_retry(FakeCtx(llm), "SYS", "USER", "label")

    assert len(llm.calls) == 2, "a fenced-but-complete reply no longer costs a retry"


# ── generate_one_section / generate_one_op_phase ─────────────────────────────


def test_a_section_result_is_unwrapped_from_its_named_key(generate):
    llm = one_reply('{"purpose": "<p>lead</p>"}')

    value = generate.generate_one_section(FakeCtx(llm), PETRA, "MANUAL", "purpose", "S")

    assert value == "<p>lead</p>"


def test_a_section_call_is_handed_the_manual_as_the_user_message(generate):
    """The system prompt is the rules; the manual has to arrive as the user turn."""
    llm = one_reply('{"purpose": "x"}')

    generate.generate_one_section(FakeCtx(llm), PETRA, "THE MANUAL TEXT", "purpose", "S")

    assert llm.calls[0]["user"] == "THE MANUAL TEXT"
    assert PETRA in llm.calls[0]["system"]


def test_a_section_reply_that_is_not_json_raises_to_the_caller(generate):
    """It must raise rather than return prose: `generate()`'s fallback depends on it."""
    llm = FakeLlm(chat=["I cannot help with that.", "Still cannot help."])

    with pytest.raises(Exception):
        generate.generate_one_section(FakeCtx(llm), PETRA, "MANUAL", "purpose", "S")


def test_a_failing_section_reply_is_logged_with_the_head_of_the_response(generate, caplog):
    """"Why did purpose come back empty" is otherwise unanswerable after the fact."""
    llm = FakeLlm(chat=["<html>gateway timeout</html>"] * 2)

    with caplog.at_level("WARNING"), pytest.raises(Exception):
        generate.generate_one_section(FakeCtx(llm), PETRA, "MANUAL", "purpose", "S")

    assert "gateway timeout" in caplog.text
    assert "section purpose" in caplog.text or "purpose" in caplog.text


def test_a_phase_result_is_unwrapped_from_its_phase_key(generate):
    llm = one_reply('{"setup": {"equipment": ["e"], "software": []}}')

    value = generate.generate_one_op_phase(FakeCtx(llm), PETRA, "MANUAL", "setup", "S")

    assert value == {"equipment": ["e"], "software": []}


def test_a_phase_reply_wrapped_under_a_renamed_key_is_still_unwrapped(generate):
    """A single top-level key is unambiguous, so the model's naming does not matter."""
    llm = one_reply('{"Setup Steps": {"equipment": ["e"], "software": []}}')

    value = generate.generate_one_op_phase(FakeCtx(llm), PETRA, "MANUAL", "setup", "S")

    assert value == {"equipment": ["e"], "software": []}


def test_a_phase_reply_that_is_not_json_raises_to_the_caller(generate):
    llm = FakeLlm(chat=["not json", "not json either"])

    with pytest.raises(Exception):
        generate.generate_one_op_phase(FakeCtx(llm), PETRA, "MANUAL", "setup", "S")


def test_a_phase_call_that_truncates_is_retried_before_it_is_given_up_on(generate):
    """The retry is why an 8k-token operating_procedure phase is recoverable at all."""
    llm = FakeLlm(chat=['{"setup": {"equipment": ["a"', '{"setup": {"equipment": ["a"]}}'])

    value = generate.generate_one_op_phase(FakeCtx(llm), PETRA, "MANUAL", "setup", "S")

    assert value == {"equipment": ["a"]}
    assert [call["max_tokens"] for call in llm.calls] == [4000, 16000]


# ── generate: the fan-out ────────────────────────────────────────────────────


def test_a_full_run_returns_every_generated_section_plus_the_procedure(generate):
    result = generate.generate(FakeCtx(KeyedLlm()), {"raw_text": "MANUAL"})

    assert set(result) == set(generate.SECTION_SCHEMAS) | {"operating_procedure"}
    assert set(result["operating_procedure"]) == set(generate.OP_PHASE_SCHEMAS)


def test_a_full_run_makes_one_call_per_task_and_no_more(generate):
    """The task count is the per-document bill; 15 tasks, not 15-plus-a-retry-each."""
    llm = KeyedLlm()

    generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    expected = len(generate.SECTION_SCHEMAS) + len(generate.OP_PHASE_SCHEMAS)
    assert len(llm.calls) == expected
    assert sorted(llm.keys_called()) == sorted(
        list(generate.SECTION_SCHEMAS) + list(generate.OP_PHASE_SCHEMAS)
    )


def test_the_run_covers_fifteen_tasks_although_the_docstring_says_fourteen(generate):
    """CHARACTERIZATION of a stale comment at generate.py:7.

    Six sections plus nine operating-procedure phases is fifteen calls, not fourteen. The
    number is load-bearing when someone is costing a run, so it is pinned here; the module
    docstring is what needs correcting, not the code.
    """
    total = len(generate.SECTION_SCHEMAS) + len(generate.OP_PHASE_SCHEMAS)

    assert total == 15


def test_each_section_result_lands_under_its_own_key(generate):
    llm = KeyedLlm(values={"purpose": "<p>the purpose</p>", "abbreviations": [["SFE", "x"]]})

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["purpose"] == "<p>the purpose</p>"
    assert result["abbreviations"] == [["SFE", "x"]]


def test_phase_results_are_normalised_on_the_way_into_the_procedure(generate):
    """The raw reply is a bare list here; the emitter needs the two-group shape."""
    llm = KeyedLlm(values={"setup": ["step one", "step two"]})

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["operating_procedure"]["setup"] == {
        "equipment": ["step one", "step two"],
        "software": [],
    }


def test_troubleshooting_stays_a_list_of_rows_in_the_procedure(generate):
    rows = [{"issue": "no pressure", "resolution": "check the seal"}]
    llm = KeyedLlm(values={"troubleshooting": rows})

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["operating_procedure"]["troubleshooting"] == rows


def test_every_call_receives_the_same_petra_and_the_same_manual(generate, monkeypatch):
    """PETRA is loaded once, outside the pool: fifteen S3 reads per run would be absurd."""
    loads = []

    def counting_load_petra(ctx):
        loads.append(ctx)
        return PETRA

    monkeypatch.setattr(generate.prompts, "load_petra", counting_load_petra)
    llm = KeyedLlm()

    generate.generate(FakeCtx(llm), {"raw_text": "THE MANUAL"})

    assert len(loads) == 1
    assert {call["user"] for call in llm.calls} == {"THE MANUAL"}
    assert all(PETRA in call["system"] for call in llm.calls)


# ── generate: one failure must not lose the others ───────────────────────────


def test_a_section_whose_call_explodes_falls_back_to_its_empty_default(generate):
    llm = KeyedLlm(fail=["description"])

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["description"] == generate.SECTION_EMPTY["description"]
    assert result["purpose"] == "purpose content"  # the other thirteen survive


def test_a_section_whose_reply_is_unparseable_falls_back_too(generate):
    llm = KeyedLlm(raw={"safety": "The manual does not say."})

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["safety"] == {"hazards": "", "engineering_controls": "", "ppe": ""}


def test_the_empty_fallback_is_a_copy_so_the_module_constant_cannot_be_mutated(generate):
    """`SECTION_EMPTY` is process-global; a shared reference would leak into the next run."""
    llm = KeyedLlm(fail=["description"])

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})
    result["description"]["tools"].append("leaked")

    assert generate.SECTION_EMPTY["description"]["tools"] == []


def test_a_phase_whose_call_explodes_falls_back_to_empty_groups(generate):
    llm = KeyedLlm(fail=["cleaning"])

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["operating_procedure"]["cleaning"] == {"equipment": [], "software": []}
    assert result["operating_procedure"]["setup"]["equipment"] == ["setup equipment step"]


def test_a_failing_troubleshooting_phase_falls_back_to_an_empty_list(generate):
    llm = KeyedLlm(fail=["troubleshooting"])

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["operating_procedure"]["troubleshooting"] == []


def test_the_phase_empty_fallback_is_a_copy_as_well(generate):
    llm = KeyedLlm(fail=["troubleshooting"])

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})
    result["operating_procedure"]["troubleshooting"].append("leaked")

    assert generate.OP_PHASE_EMPTY["troubleshooting"] == []


def test_every_task_failing_still_returns_a_well_formed_document(generate):
    """The emitter and the editor both index into this shape unconditionally."""
    all_keys = list(generate.SECTION_SCHEMAS) + list(generate.OP_PHASE_SCHEMAS)
    llm = KeyedLlm(fail=all_keys)

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    for key in generate.SECTION_SCHEMAS:
        assert result[key] == generate.SECTION_EMPTY[key]
    assert result["operating_procedure"] == generate.OP_PHASE_EMPTY


def test_a_truncated_first_attempt_inside_a_full_run_is_retried_not_dropped(generate):
    llm = KeyedLlm(truncate_once=["operation"], values={"operation": {"equipment": ["a"]}})

    result = generate.generate(FakeCtx(llm), {"raw_text": "MANUAL"})

    assert result["operating_procedure"]["operation"] == {"equipment": ["a"], "software": []}
    retries = [call for call in llm.calls if call["max_tokens"] == 16000]
    assert [call["key"] for call in retries] == ["operation"]


# ── generate: the manual cap ─────────────────────────────────────────────────


def test_a_manual_under_the_cap_is_sent_verbatim(generate):
    llm = KeyedLlm()
    text = "x" * (generate.MAX_MANUAL_CHARS - 1)

    generate.generate(FakeCtx(llm), {"raw_text": text})

    assert {call["user"] for call in llm.calls} == {text}
    assert "[MANUAL TRUNCATED]" not in llm.calls[0]["user"]


def test_a_manual_over_the_cap_is_capped_and_says_so(generate):
    """The marker is the model's only clue that the procedure it was given is incomplete."""
    llm = KeyedLlm()

    generate.generate(FakeCtx(llm), {"raw_text": "y" * (generate.MAX_MANUAL_CHARS + 5000)})

    sent = llm.calls[0]["user"]
    assert sent.endswith("\n\n[MANUAL TRUNCATED]")
    assert sent.count("y") == generate.MAX_MANUAL_CHARS


def test_the_capped_manual_is_slightly_longer_than_the_cap(generate):
    """CHARACTERIZATION of an off-by-marker at generate.py:239.

    The marker is appended *after* slicing to MAX_MANUAL_CHARS, so the text actually sent
    is 20 characters over the budget the constant names. Harmless at 180k against the
    model's context, but it means the cap is not the bound it claims to be; a correct
    version would reserve room for the marker inside the slice.
    """
    llm = KeyedLlm()

    generate.generate(FakeCtx(llm), {"raw_text": "y" * (generate.MAX_MANUAL_CHARS + 1)})

    assert len(llm.calls[0]["user"]) == generate.MAX_MANUAL_CHARS + len("\n\n[MANUAL TRUNCATED]")


@pytest.mark.parametrize("extracted", [{}, {"raw_text": None}, {"raw_text": ""}])
def test_a_missing_manual_still_runs_every_task_with_empty_text(generate, extracted):
    """Ingest can legitimately produce nothing; the run must not crash on `len(None)`."""
    llm = KeyedLlm()

    result = generate.generate(FakeCtx(llm), extracted)

    assert {call["user"] for call in llm.calls} == {""}
    assert result["operating_procedure"]


# ── generate: progress reporting ─────────────────────────────────────────────


def test_progress_is_reported_once_per_completed_task(generate):
    """A silent 15-call fan-out gets the run re-queued by the reaper."""
    ctx = FakeCtx(KeyedLlm())

    generate.generate(ctx, {"raw_text": "MANUAL"})

    assert len(ctx.progress_calls) == 15


def test_the_progress_percentage_climbs_from_thirty_three_to_seventy_five(generate):
    """The stage owns 30-75%; ingest ends at 20 and review picks up after."""
    ctx = FakeCtx(KeyedLlm())

    generate.generate(ctx, {"raw_text": "MANUAL"})

    percentages = [pct for _message, pct in ctx.progress_calls]
    assert percentages == sorted(percentages)
    assert percentages[0] == 33
    assert percentages[-1] == 75


def test_the_progress_message_counts_completed_tasks_out_of_the_total(generate):
    ctx = FakeCtx(KeyedLlm())

    generate.generate(ctx, {"raw_text": "MANUAL"})

    assert ctx.progress_calls[0][0] == "Generated 1 of 15 sections"
    assert ctx.progress_calls[-1][0] == "Generated 15 of 15 sections"


def test_a_failed_task_still_reports_progress(generate):
    """Otherwise the bar stalls short of 75% and looks like a hung run."""
    ctx = FakeCtx(KeyedLlm(fail=["purpose", "troubleshooting"]))

    generate.generate(ctx, {"raw_text": "MANUAL"})

    assert ctx.progress_calls[-1][1] == 75


def test_the_pool_is_bounded_so_a_run_cannot_open_fifteen_connections(generate):
    """Seven workers is the ported value and the concurrency the gateway is sized for."""
    assert generate.GENERATE_MAX_WORKERS == 7
