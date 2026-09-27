"""BOP's prompt override precedence.

`memry/petra_prompt.json` wins over `prompts/petra.txt`, matching the app being replaced.
That override object exists in S3 today, so reading the default instead would silently
change what the model is told — which is why this is worth pinning rather than assuming.

The other half is fail-soft: a malformed override must fall back to the default rather
than stop generation. A single bad edit would otherwise break every document.

ctx.assets is faked; these functions only ever call exists() and read_text().
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
def prompts():
    _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import prompts as module

    return module


class FakeAssets:
    """Stands in for ctx.assets, recording what was read."""

    def __init__(self, objects: dict[str, str], *, raise_on: str | None = None):
        self.objects = objects
        self.raise_on = raise_on
        self.reads: list[str] = []

    def exists(self, relative: str) -> bool:
        if self.raise_on == "exists":
            raise RuntimeError("storage unavailable")
        return relative in self.objects

    def read_text(self, relative: str, cache: bool = True) -> str:
        self.reads.append(relative)
        if self.raise_on == relative:
            raise RuntimeError("unreadable object")
        return self.objects[relative]


class Ctx:
    def __init__(self, assets: FakeAssets):
        self.assets = assets


def ctx_with(objects: dict[str, str], **kwargs) -> Ctx:
    return Ctx(FakeAssets(objects, **kwargs))


# ── the override wins ─────────────────────────────────────────────────────────


def test_the_petra_override_is_preferred_over_the_default(prompts):
    ctx = ctx_with(
        {
            prompts.PETRA_DEFAULT: "the default prompt",
            prompts.PETRA_OVERRIDE: json.dumps({"prompt": "the saved override"}),
        }
    )

    assert prompts.load_petra(ctx) == "the saved override"


def test_the_reviewer_override_is_preferred_over_the_default(prompts):
    ctx = ctx_with(
        {
            prompts.REVIEWER_DEFAULT: "default reviewer",
            prompts.REVIEWER_OVERRIDE: json.dumps({"prompt": "override reviewer"}),
        }
    )

    assert prompts.load_reviewer(ctx) == "override reviewer"


def test_the_default_is_used_when_no_override_exists(prompts):
    ctx = ctx_with({prompts.PETRA_DEFAULT: "the default prompt"})

    assert prompts.load_petra(ctx) == "the default prompt"


def test_the_override_is_read_uncached(prompts):
    """An edit must take effect on the next run, not after a redeploy."""
    ctx = ctx_with(
        {
            prompts.PETRA_DEFAULT: "d",
            prompts.PETRA_OVERRIDE: json.dumps({"prompt": "o"}),
        }
    )

    prompts.load_petra(ctx)

    assert prompts.PETRA_OVERRIDE in ctx.assets.reads


# ── fail-soft on a bad override ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "{",
        json.dumps({"wrong_key": "x"}),
        json.dumps({"prompt": ""}),
        json.dumps({"prompt": "   "}),
        json.dumps({"prompt": None}),
        json.dumps({"prompt": 42}),
        json.dumps({"prompt": ["a"]}),
        json.dumps(["not", "a", "dict"]),
    ],
)
def test_an_unusable_override_falls_back_to_the_default(prompts, body):
    """Generation must survive a bad override; the alternative is every run failing."""
    ctx = ctx_with({prompts.PETRA_DEFAULT: "the default", prompts.PETRA_OVERRIDE: body})

    assert prompts.load_petra(ctx) == "the default"


def test_an_unreadable_override_falls_back_to_the_default(prompts):
    ctx = ctx_with(
        {prompts.PETRA_DEFAULT: "the default", prompts.PETRA_OVERRIDE: "{}"},
        raise_on=prompts.PETRA_OVERRIDE,
    )

    assert prompts.load_petra(ctx) == "the default"


def test_a_storage_failure_checking_for_the_override_falls_back(prompts):
    ctx = ctx_with({prompts.PETRA_DEFAULT: "the default"}, raise_on="exists")

    assert prompts.load_petra(ctx) == "the default"


def test_a_reviewer_override_that_is_unusable_falls_back(prompts):
    ctx = ctx_with(
        {prompts.REVIEWER_DEFAULT: "default reviewer", prompts.REVIEWER_OVERRIDE: "{"}
    )

    assert prompts.load_reviewer(ctx) == "default reviewer"


# ── the *_default loaders ignore overrides ───────────────────────────────────


def test_load_petra_default_ignores_a_saved_override(prompts):
    """The config screen shows the default so a user can compare and reset to it."""
    ctx = ctx_with(
        {
            prompts.PETRA_DEFAULT: "the default prompt",
            prompts.PETRA_OVERRIDE: json.dumps({"prompt": "the override"}),
        }
    )

    assert prompts.load_petra_default(ctx) == "the default prompt"


def test_load_reviewer_default_ignores_a_saved_override(prompts):
    ctx = ctx_with(
        {
            prompts.REVIEWER_DEFAULT: "default reviewer",
            prompts.REVIEWER_OVERRIDE: json.dumps({"prompt": "override"}),
        }
    )

    assert prompts.load_reviewer_default(ctx) == "default reviewer"


def test_the_two_prompts_use_separate_objects(prompts):
    # A reviewer override must not change what PETRA is told, and vice versa.
    assert prompts.PETRA_DEFAULT != prompts.REVIEWER_DEFAULT
    assert prompts.PETRA_OVERRIDE != prompts.REVIEWER_OVERRIDE

    ctx = ctx_with(
        {
            prompts.PETRA_DEFAULT: "petra default",
            prompts.REVIEWER_DEFAULT: "reviewer default",
            prompts.REVIEWER_OVERRIDE: json.dumps({"prompt": "reviewer override"}),
        }
    )

    assert prompts.load_petra(ctx) == "petra default"
    assert prompts.load_reviewer(ctx) == "reviewer override"
