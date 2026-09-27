"""The BOP reviewer pass, which is advisory and must never block a document.

`review()` wraps one LLM critique call. The behaviour worth pinning is the fail-soft
path: a reviewer that errors, times out, or answers in an unexpected shape reports "ok"
rather than failing the run. If that regressed, one flaky model call would stop every
document from being built — and the symptom would look like a generation failure rather
than a review failure.

ctx.llm is faked, so nothing here touches Iliad.
"""

from __future__ import annotations

import json

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "silo.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def bop():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import prompts, review

    return {"review": review, "prompts": prompts}


class FakeLlm:
    """Returns a canned response, or raises, and records the call."""

    def __init__(self, response: str | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def chat(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return self.response


class FakeAssets:
    def __init__(self, objects: dict[str, str]):
        self.objects = objects

    def exists(self, relative: str) -> bool:
        return relative in self.objects

    def read_text(self, relative: str, cache: bool = True) -> str:
        return self.objects[relative]


class Ctx:
    def __init__(self, llm: FakeLlm, prompts_module):
        self.llm = llm
        self.assets = FakeAssets({prompts_module.REVIEWER_DEFAULT: "be critical"})


def ctx_for(bop, llm: FakeLlm) -> Ctx:
    return Ctx(llm, bop["prompts"])


# ── the happy path ────────────────────────────────────────────────────────────


def test_a_well_formed_verdict_is_returned(bop):
    llm = FakeLlm(json.dumps({"status": "issues", "issues": ["Section 4 is vague"]}))

    outcome = bop["review"].review(ctx_for(bop, llm), "manual text", {"purpose": "x"})

    assert outcome["status"] == "issues"
    assert outcome["issues"] == ["Section 4 is vague"]


def test_a_verdict_without_issues_gets_an_empty_list(bop):
    """Callers read outcome["issues"] unguarded, so the key has to exist."""
    llm = FakeLlm(json.dumps({"status": "ok"}))

    outcome = bop["review"].review(ctx_for(bop, llm), "manual", {})

    assert outcome == {"status": "ok", "issues": []}


def test_a_fenced_verdict_is_parsed(bop):
    llm = FakeLlm('```json\n{"status": "ok", "issues": []}\n```')

    assert bop["review"].review(ctx_for(bop, llm), "manual", {})["status"] == "ok"


# ── fail-soft ─────────────────────────────────────────────────────────────────


def test_an_llm_error_is_reported_as_ok(bop):
    llm = FakeLlm(error=RuntimeError("Iliad timed out"))

    outcome = bop["review"].review(ctx_for(bop, llm), "manual", {})

    # Advisory: a failed review must not fail the document.
    assert outcome == {"status": "ok", "issues": []}


@pytest.mark.parametrize(
    "response",
    [
        json.dumps({"verdict": "fine"}),   # no status key
        json.dumps(["a", "list"]),          # not an object
        json.dumps("a bare string"),
        "not json at all",
        "",
    ],
)
def test_an_unexpected_shape_is_reported_as_ok(bop, response):
    llm = FakeLlm(response)

    assert bop["review"].review(ctx_for(bop, llm), "manual", {}) == {
        "status": "ok",
        "issues": [],
    }


def test_a_none_response_is_reported_as_ok(bop):
    llm = FakeLlm(None)

    assert bop["review"].review(ctx_for(bop, llm), "manual", {}) == {
        "status": "ok",
        "issues": [],
    }


# ── what gets sent ────────────────────────────────────────────────────────────


def test_the_manual_is_truncated_to_the_excerpt_budget(bop):
    """Ported verbatim from the app being replaced: 8000 characters.

    Sending the whole manual would blow the input budget on a 180k-character extract.
    """
    llm = FakeLlm(json.dumps({"status": "ok"}))
    long_manual = "x" * (bop["review"].MANUAL_EXCERPT_CHARS + 5_000)

    bop["review"].review(ctx_for(bop, llm), long_manual, {})

    sent = json.loads(llm.calls[0]["user"])
    assert len(sent["manual_excerpt"]) == bop["review"].MANUAL_EXCERPT_CHARS


def test_the_generated_document_is_sent_for_critique(bop):
    llm = FakeLlm(json.dumps({"status": "ok"}))
    document = {"purpose": "the purpose", "scope": "the scope"}

    bop["review"].review(ctx_for(bop, llm), "manual", document)

    assert json.loads(llm.calls[0]["user"])["generated"] == document


def test_the_reviewer_prompt_is_used_as_the_system_prompt(bop):
    llm = FakeLlm(json.dumps({"status": "ok"}))

    bop["review"].review(ctx_for(bop, llm), "manual", {})

    assert llm.calls[0]["system"] == "be critical"


def test_the_token_budget_is_the_documented_one(bop):
    llm = FakeLlm(json.dumps({"status": "ok"}))

    bop["review"].review(ctx_for(bop, llm), "manual", {})

    assert llm.calls[0]["max_tokens"] == bop["review"].REVIEW_MAX_TOKENS


def test_non_ascii_content_is_not_escaped_away(bop):
    """ensure_ascii=False keeps CO₂ readable to the model rather than \\u2082."""
    llm = FakeLlm(json.dumps({"status": "ok"}))

    bop["review"].review(ctx_for(bop, llm), "CO₂ vessel", {})

    assert "CO₂" in llm.calls[0]["user"]
