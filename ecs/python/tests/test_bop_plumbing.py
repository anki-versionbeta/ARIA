"""BOP's orchestration, its own endpoints, and the few-shot examples.

Three modules that had no dedicated tests between them: `silo.py` (29%), `router.py` (49%)
and `examples.py` (34%). They are grouped because each is thin glue whose bugs are all of
the same kind — a stage that forgets to report progress, an endpoint that persists to the
wrong key, a cache that never invalidates. None of that shows up in the modules those
three delegate to, which are separately at 100%.

`silo.py` calls itself "orchestration only", and these tests take that at its word: the
stage functions are exercised with `ingest`, `generate`, `review`, `editing` and `emit`
stubbed out, so a failure here means the wiring is wrong rather than the payload. Testing
the real emitter through this seam would need the actual SOP template out of S3, and it is
already covered directly.

`examples._cache` is a **process-global**. It is reset around every test in this file,
because leaking a populated cache would make whichever test ran next pass for the wrong
reason -- and other BOP test files share the same process.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

BOP_DIR = BACKEND_ROOT / "silos" / "bop"

pytestmark = pytest.mark.skipif(
    not (BOP_DIR / "silo.py").is_file(), reason="the BOP silo is not present"
)


@pytest.fixture(scope="module")
def bop():
    module = _load_module("bop", BOP_DIR / "silo.py")
    from da_silos.bop import editing, emit, examples, ingest, prompts, router, schema

    return {
        "silo": module,
        "editing": editing,
        "emit": emit,
        "examples": examples,
        "ingest": ingest,
        "prompts": prompts,
        "router": router,
        "schema": schema,
    }


@pytest.fixture(autouse=True)
def clean_example_cache(bop):
    """The example cache outlives a test, and other BOP files run in this process."""
    bop["examples"].reset_cache()
    yield
    bop["examples"].reset_cache()


# ── test doubles ─────────────────────────────────────────────────────────────


class FakeStorage:
    """The `ctx.storage` surface BOP's stages touch, backed by a dict."""

    def __init__(self, *, filename="manual.pdf", input_bytes=b"raw", media=None):
        self._filename = filename
        self._input = input_bytes
        self.objects = dict(media or {})
        self.outputs = []

    def input_filename(self, _run):
        return self._filename

    def read_input(self, _run):
        return self._input

    def put_media(self, _run, name, data):
        key = f"BOP/memry/{name}"
        self.objects[key] = data
        return key

    def open_key(self, key):
        return io.BytesIO(self.objects[key])

    def attach_output(self, _run, filename, data, content_type):
        self.outputs.append({"filename": filename, "bytes": data, "type": content_type})


class FakeSections:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.written = None

    def write(self, flattened):
        self.written = flattened
        return len(flattened)

    def rows(self):
        return list(self._rows)


class FakeAssets:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.reads = []

    def read(self, relative):
        self.reads.append(relative)
        return self.objects.get(relative, b"template-bytes")


class FakeCtx:
    def __init__(self, *, storage=None, sections=None, assets=None, checkpoints=None):
        self.storage = storage or FakeStorage()
        self.sections = sections or FakeSections()
        self.assets = assets or FakeAssets()
        self._checkpoints = dict(checkpoints or {})
        self.progress_calls = []
        self.paused_with = None

    def progress(self, message, pct=None):
        self.progress_calls.append((message, pct))

    def checkpoint(self, stage):
        return self._checkpoints.get(stage)

    def pause_for_user(self, message=None):
        self.paused_with = message
        return {"paused": message}

    @property
    def percentages(self):
        return [pct for _message, pct in self.progress_calls]


class Run:
    id = "run-1"


class FakeStore:
    """In-memory `ObjectStore`, which is all `router.py` needs of one."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.deleted = []

    def open(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return io.BytesIO(self.objects[key])

    def exists(self, key):
        return key in self.objects

    def put(self, key, data):
        self.objects[key] = data
        return len(data)

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


# ── silo.py: the contract the registry reads ─────────────────────────────────


def test_the_silo_declares_the_pipeline_it_replaces(bop):
    """The stage list is the pipeline; reordering it reorders the run."""
    silo = bop["silo"]
    assert silo.LABEL == "Equipment BOP"
    assert silo.STAGES == ["extract", "generate", "review", "await_review", "build"]


def test_the_silo_accepts_only_the_two_manual_formats_it_can_read(bop):
    """`ingest` dispatches on these extensions, so accepting more would fail mid-run."""
    assert sorted(bop["silo"].ACCEPTS) == [".docx", ".pdf"]


def test_every_declared_stage_exists_as_a_callable(bop):
    """The registry fails discovery on a missing stage, which is a deploy-time outage."""
    silo = bop["silo"]
    for stage in silo.STAGES:
        assert callable(getattr(silo, stage)), stage


def test_the_storage_folders_match_the_layout_the_old_app_already_uses(bop):
    """One S3 layout serves both apps during migration, so these names are a contract."""
    assert bop["silo"].STORAGE_FOLDERS == {
        "input": "uploads",
        "output": "downloads",
        "media": "memry",
    }


def test_the_silo_exposes_a_storage_prefix_and_section_order(bop):
    """Both are read by the platform rather than called, so absence is silent."""
    silo = bop["silo"]
    assert silo.STORAGE_PREFIX
    assert silo.SECTION_ORDER == bop["editing"].SECTION_ORDER


# ── silo.py: extract ─────────────────────────────────────────────────────────


def test_extract_stores_the_manual_text_as_media_and_reports_its_size(bop, monkeypatch):
    """The text can be 180k characters, so it goes to object storage, not a checkpoint."""
    monkeypatch.setattr(
        bop["ingest"], "extract", lambda *_a, **_k: {"raw_text": "hello manual", "format": "pdf"}
    )
    ctx = FakeCtx(storage=FakeStorage(filename="pump.pdf"))

    result = bop["silo"].extract(Run(), ctx)

    assert result["chars"] == len("hello manual")
    assert result["format"] == "pdf"
    assert result["source_name"] == "pump.pdf"
    assert ctx.storage.objects[result["text_key"]] == b"hello manual"


def test_extract_falls_back_to_a_default_filename(bop, monkeypatch):
    """A run whose input name was never recorded still has to pick an ingest strategy."""
    seen = {}

    def fake_extract(filename, _payload, _ctx):
        seen["filename"] = filename
        return {"raw_text": "text", "format": "pdf"}

    monkeypatch.setattr(bop["ingest"], "extract", fake_extract)
    ctx = FakeCtx(storage=FakeStorage(filename=None))

    assert bop["silo"].extract(Run(), ctx)["source_name"] == "manual.pdf"
    assert seen["filename"] == "manual.pdf"


@pytest.mark.parametrize("empty", ["", "   \n\t ", None])
def test_a_manual_yielding_no_text_fails_the_run_loudly(bop, monkeypatch, empty):
    """Silently generating a BOP from nothing would produce a plausible, invented SOP."""
    monkeypatch.setattr(
        bop["ingest"], "extract", lambda *_a, **_k: {"raw_text": empty, "format": "pdf"}
    )
    with pytest.raises(ValueError, match="No text could be extracted"):
        bop["silo"].extract(Run(), FakeCtx())


def test_extract_reports_progress_before_and_after_reading(bop, monkeypatch):
    """The first stage is the slowest, and a silent stage gets re-queued by the reaper."""
    monkeypatch.setattr(
        bop["ingest"], "extract", lambda *_a, **_k: {"raw_text": "text", "format": "pdf"}
    )
    ctx = FakeCtx()
    bop["silo"].extract(Run(), ctx)
    assert ctx.percentages == [5, 25]


# ── silo.py: generate, review, await_review ──────────────────────────────────


def test_generate_writes_the_draft_sections_before_anyone_reviews_them(bop, monkeypatch):
    """Durable and reviewable before the user touches anything, per the stage comment."""
    monkeypatch.setattr(bop["silo"].generation, "generate", lambda _ctx, _payload: {"purpose": "P"})
    monkeypatch.setattr(bop["editing"], "flatten", lambda payload: [("purpose", payload["purpose"])])

    storage = FakeStorage(media={"BOP/memry/extracted.txt": b"manual text"})
    sections = FakeSections()
    ctx = FakeCtx(
        storage=storage,
        sections=sections,
        checkpoints={"extract": {"text_key": "BOP/memry/extracted.txt"}},
    )

    result = bop["silo"].generate(Run(), ctx)

    assert result == {"bop_json": {"purpose": "P"}}
    assert sections.written == [("purpose", "P")]
    assert ctx.percentages == [30, 75]


def test_generate_passes_the_stored_manual_text_to_the_model(bop, monkeypatch):
    """The text comes back from object storage, not from the checkpoint row."""
    seen = {}
    monkeypatch.setattr(
        bop["silo"].generation, "generate", lambda _ctx, payload: seen.setdefault("payload", payload)
    )
    monkeypatch.setattr(bop["editing"], "flatten", lambda _payload: [])

    ctx = FakeCtx(
        storage=FakeStorage(media={"k": b"the manual body"}),
        checkpoints={"extract": {"text_key": "k"}},
    )
    bop["silo"].generate(Run(), ctx)

    assert seen["payload"]["raw_text"] == "the manual body"


def test_undecodable_manual_bytes_are_replaced_rather_than_raising(bop, monkeypatch):
    """A vision transcript can carry stray bytes; losing a glyph beats losing the run."""
    seen = {}
    monkeypatch.setattr(
        bop["silo"].generation, "generate", lambda _ctx, payload: seen.setdefault("payload", payload)
    )
    monkeypatch.setattr(bop["editing"], "flatten", lambda _payload: [])

    ctx = FakeCtx(
        storage=FakeStorage(media={"k": b"caf\xff manual"}),
        checkpoints={"extract": {"text_key": "k"}},
    )
    bop["silo"].generate(Run(), ctx)

    assert "manual" in seen["payload"]["raw_text"]


def test_review_sees_both_the_manual_and_the_generated_draft(bop, monkeypatch):
    """Reviewing the draft against itself would never find a missing section."""
    seen = {}

    def fake_review(_ctx, manual, generated):
        seen.update(manual=manual, generated=generated)
        return {"status": "ok", "issues": []}

    monkeypatch.setattr(bop["silo"].reviewing, "review", fake_review)
    ctx = FakeCtx(
        storage=FakeStorage(media={"k": b"manual body"}),
        checkpoints={"extract": {"text_key": "k"}, "generate": {"bop_json": {"purpose": "P"}}},
    )

    assert bop["silo"].review(Run(), ctx) == {"status": "ok", "issues": []}
    assert seen["manual"] == "manual body"
    assert seen["generated"] == {"purpose": "P"}


def test_review_tolerates_a_generate_checkpoint_that_never_ran(bop, monkeypatch):
    """Every checkpoint read here is `or {}` because a retried run can arrive part-built."""
    monkeypatch.setattr(
        bop["silo"].reviewing, "review", lambda _ctx, _manual, generated: {"seen": generated}
    )
    ctx = FakeCtx(
        storage=FakeStorage(media={"k": b"manual"}),
        checkpoints={"extract": {"text_key": "k"}},
    )
    assert bop["silo"].review(Run(), ctx) == {"seen": {}}


def test_awaiting_review_names_how_many_points_were_flagged(bop):
    """The message is the whole notification a reviewer gets, so the count matters."""
    ctx = FakeCtx(checkpoints={"review": {"issues": [{"a": 1}, {"b": 2}]}})
    bop["silo"].await_review(Run(), ctx)
    assert "2 point(s) flagged" in ctx.paused_with


def test_awaiting_review_stays_quiet_when_the_draft_looked_clean(bop):
    ctx = FakeCtx(checkpoints={"review": {"issues": []}})
    bop["silo"].await_review(Run(), ctx)
    assert ctx.paused_with == "Ready for your review"


def test_awaiting_review_parks_the_run_rather_than_holding_a_worker(bop):
    """The point of the stage: a human review can take an hour."""
    ctx = FakeCtx(checkpoints={"review": {"issues": []}})
    assert bop["silo"].await_review(Run(), ctx) == {"paused": "Ready for your review"}
    assert ctx.percentages == [85]


# ── silo.py: build ───────────────────────────────────────────────────────────


def test_build_prefers_the_users_edits_over_the_generated_draft(bop, monkeypatch):
    """Otherwise the review stage would be decorative."""
    seen = {}

    def fake_apply(generated, rows):
        seen.update(generated=generated, rows=rows)
        return {"merged": True}

    monkeypatch.setattr(bop["editing"], "apply_edits", fake_apply)
    monkeypatch.setattr(bop["emit"], "build_docx", lambda *_a, **_k: b"docx-bytes")

    ctx = FakeCtx(
        sections=FakeSections(rows=[{"section_key": "purpose", "content": "edited"}]),
        checkpoints={"generate": {"bop_json": {"purpose": "drafted"}}, "extract": {}},
    )
    bop["silo"].build(Run(), ctx)

    assert seen["generated"] == {"purpose": "drafted"}
    assert seen["rows"] == [{"section_key": "purpose", "content": "edited"}]


def test_the_built_document_is_named_after_the_uploaded_manual(bop, monkeypatch):
    """A downloads folder full of `BOP_manual.docx` would be unusable."""
    monkeypatch.setattr(bop["editing"], "apply_edits", lambda *_a: {})
    monkeypatch.setattr(bop["emit"], "build_docx", lambda *_a, **_k: b"docx-bytes")

    ctx = FakeCtx(checkpoints={"extract": {"source_name": "Filling Pump 3.pdf"}})
    bop["silo"].build(Run(), ctx)

    assert ctx.storage.outputs[0]["filename"] == "BOP_Filling Pump 3.docx"


@pytest.mark.parametrize(
    "source_name,expected",
    [
        ("pump.pdf", "BOP_pump.docx"),
        ("folder/nested/pump.docx", "BOP_pump.docx"),
        (None, "BOP_manual.docx"),
        ("", "BOP_manual.docx"),
        (".pdf", "BOP_.pdf.docx"),
    ],
)
def test_the_output_stem_survives_awkward_source_names(bop, monkeypatch, source_name, expected):
    """A dotfile-shaped name has an empty `splitext` stem, hence the `or "manual"`.

    The `.pdf` case is characterisation, not endorsement: `splitext(".pdf")` returns
    `(".pdf", "")`, so the extension becomes the stem and the output is `BOP_.pdf.docx`.
    Harmless, and no real upload looks like this.
    """
    monkeypatch.setattr(bop["editing"], "apply_edits", lambda *_a: {})
    monkeypatch.setattr(bop["emit"], "build_docx", lambda *_a, **_k: b"docx-bytes")

    ctx = FakeCtx(checkpoints={"extract": {"source_name": source_name}})
    bop["silo"].build(Run(), ctx)

    assert ctx.storage.outputs[0]["filename"] == expected


def test_the_output_is_attached_as_a_word_document(bop, monkeypatch):
    """The browser downloads this; a wrong content type makes it open as a zip."""
    monkeypatch.setattr(bop["editing"], "apply_edits", lambda *_a: {})
    monkeypatch.setattr(bop["emit"], "build_docx", lambda *_a, **_k: b"docx-bytes")

    ctx = FakeCtx(checkpoints={"extract": {}})
    result = bop["silo"].build(Run(), ctx)

    attached = ctx.storage.outputs[0]
    assert attached["type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert result == {"bytes": len(b"docx-bytes")}
    assert ctx.percentages == [90, 100]


def test_build_reads_the_sop_template_from_the_silos_own_assets(bop, monkeypatch):
    """The template is a silo asset, not something the platform knows about."""
    monkeypatch.setattr(bop["editing"], "apply_edits", lambda *_a: {})
    monkeypatch.setattr(bop["emit"], "build_docx", lambda *_a, **_k: b"docx-bytes")

    ctx = FakeCtx(checkpoints={"extract": {}})
    bop["silo"].build(Run(), ctx)

    assert bop["emit"].TEMPLATE_ASSET in ctx.assets.reads


# ── examples.py ──────────────────────────────────────────────────────────────


def template_docx(sections) -> bytes:
    """A stand-in SOP template: `sections` maps an H1 to a list of (level, text) items."""
    from docx import Document

    document = Document()
    for heading, items in sections:
        document.add_heading(heading, level=1)
        for level, text in items:
            if level == 0:
                document.add_paragraph(text)
            else:
                document.add_heading(text, level=level)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def examples_ctx(payload):
    return FakeCtx(assets=FakeAssets({"templates/SOP template.docx": payload}))


def test_template_headings_are_mapped_to_the_json_keys(bop):
    """The model is keyed by JSON name; an unmapped H1 has nowhere to go."""
    payload = template_docx([("PURPOSE", [(0, "State the purpose.")])])
    assert bop["examples"].get_template_examples(examples_ctx(payload)) == {
        "purpose": "State the purpose."
    }


def test_heading_matching_ignores_case_and_surrounding_space(bop):
    """Templates are hand-edited, so the heading's case and padding drift."""
    payload = template_docx([("  Operating Procedure  ", [(0, "Do the thing.")])])
    assert "operating_procedure" in bop["examples"].get_template_examples(examples_ctx(payload))


def test_a_heading_the_schema_does_not_know_is_skipped(bop):
    """A template section with no JSON counterpart must not invent a key."""
    payload = template_docx([("REVISION HISTORY", [(0, "v1 initial.")])])
    assert bop["examples"].get_template_examples(examples_ctx(payload)) == {}


def test_a_mapped_heading_with_no_body_produces_no_example(bop):
    """An empty example would tell the model the section is meant to be empty."""
    payload = template_docx([("PURPOSE", [])])
    assert bop["examples"].get_template_examples(examples_ctx(payload)) == {}


def test_subsection_headings_are_marked_and_their_text_indented(bop):
    """The shape carries the structure, which is the register the model copies."""
    payload = template_docx(
        [("SAFETY", [(0, "Top line."), (2, "Hazards"), (0, "Wear gloves.")])]
    )
    body = bop["examples"].get_template_examples(examples_ctx(payload))["safety"]
    assert body.splitlines() == ["Top line.", "## Hazards", "  Wear gloves."]


def test_a_level_three_subsection_gets_a_deeper_marker(bop):
    payload = template_docx([("SAFETY", [(3, "Deep hazard"), (0, "Careful.")])])
    body = bop["examples"].get_template_examples(examples_ctx(payload))["safety"]
    assert body.splitlines() == ["### Deep hazard", "  Careful."]


def test_an_unreadable_template_leaves_generation_without_few_shot_rather_than_broken(bop):
    """Fail-soft on purpose: no examples means a worse SOP, not no SOP."""

    class Exploding(FakeAssets):
        def read(self, relative):
            raise RuntimeError("S3 unavailable")

    ctx = FakeCtx(assets=Exploding())
    assert bop["examples"].get_template_examples(ctx) == {}


def test_a_template_that_is_not_a_docx_is_also_survived(bop):
    assert bop["examples"].get_template_examples(examples_ctx(b"not a docx")) == {}


def test_the_template_is_read_once_per_process_not_once_per_call(bop):
    """It is a multi-megabyte S3 object and generation asks for examples repeatedly."""
    payload = template_docx([("PURPOSE", [(0, "Purpose text.")])])
    ctx = examples_ctx(payload)

    first = bop["examples"].get_template_examples(ctx)
    second = bop["examples"].get_template_examples(ctx)

    assert first == second
    assert len(ctx.assets.reads) == 1


def test_resetting_the_cache_forces_the_template_to_be_read_again(bop):
    """Only the tests need this, which is why it exists as a separate entry point."""
    payload = template_docx([("PURPOSE", [(0, "Purpose text.")])])
    ctx = examples_ctx(payload)

    bop["examples"].get_template_examples(ctx)
    bop["examples"].reset_cache()
    bop["examples"].get_template_examples(ctx)

    assert len(ctx.assets.reads) == 2


def test_an_empty_result_is_still_cached_rather_than_retried(bop):
    """A failing template must not be re-fetched on every one of the 15 generate tasks."""
    ctx = examples_ctx(b"not a docx")

    bop["examples"].get_template_examples(ctx)
    bop["examples"].get_template_examples(ctx)

    assert len(ctx.assets.reads) == 1


# ── router.py ────────────────────────────────────────────────────────────────


@pytest.fixture
def prompt_api(bop, monkeypatch):
    """The BOP router mounted alone.

    The shared conftest client points SILOS_DIR at the probe fixture, so BOP's router is
    not mounted there; and these endpoints reach `get_object_store()` directly rather than
    through `ctx`, so the store is patched in the router's own namespace.
    """
    from api.backend.da_platform.auth.deps import current_user

    router_module = bop["router"]
    prompts = bop["prompts"]

    def build(objects=None, *, user="asha.rao"):
        store = FakeStore(objects)
        monkeypatch.setattr(router_module, "get_object_store", lambda: store)

        class FakeUser:
            username = user

        app = FastAPI()
        app.include_router(router_module.router)
        app.dependency_overrides[current_user] = lambda: FakeUser()
        return TestClient(app), store, prompts

    return build


def keys_for(bop, *relatives):
    from api.backend.da_platform.storage.base import asset_key

    return [asset_key(bop["silo"].STORAGE_PREFIX, relative) for relative in relatives]


def test_the_defaults_are_served_when_nothing_has_been_overridden(bop, prompt_api):
    prompts = bop["prompts"]
    default_petra, default_reviewer = keys_for(
        bop, prompts.PETRA_DEFAULT, prompts.REVIEWER_DEFAULT
    )
    client, _store, _p = prompt_api(
        {default_petra: b"default petra", default_reviewer: b"default reviewer"}
    )

    body = client.get("/config/prompts").json()

    assert body == {
        "petra": "default petra",
        "reviewer": "default reviewer",
        "petra_is_override": False,
        "reviewer_is_override": False,
    }


def test_an_override_wins_over_the_default_and_says_so(bop, prompt_api):
    """The screen has to show which of the two the model is actually being given."""
    prompts = bop["prompts"]
    default_petra, default_reviewer, override_petra = keys_for(
        bop, prompts.PETRA_DEFAULT, prompts.REVIEWER_DEFAULT, prompts.PETRA_OVERRIDE
    )
    client, _store, _p = prompt_api(
        {
            default_petra: b"default petra",
            default_reviewer: b"default reviewer",
            override_petra: json.dumps({"prompt": "edited petra"}).encode(),
        }
    )

    body = client.get("/config/prompts").json()

    assert body["petra"] == "edited petra"
    assert body["petra_is_override"] is True
    assert body["reviewer_is_override"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        json.dumps({"prompt": ""}).encode(),
        json.dumps({"prompt": "   "}).encode(),
        json.dumps({"prompt": 42}).encode(),
        json.dumps({"other": "key"}).encode(),
    ],
)
def test_an_unusable_override_falls_back_to_the_default(bop, prompt_api, payload):
    """One bad edit would otherwise change what every future document is generated from."""
    prompts = bop["prompts"]
    default_petra, default_reviewer, override_petra = keys_for(
        bop, prompts.PETRA_DEFAULT, prompts.REVIEWER_DEFAULT, prompts.PETRA_OVERRIDE
    )
    client, _store, _p = prompt_api(
        {
            default_petra: b"default petra",
            default_reviewer: b"default reviewer",
            override_petra: payload,
        }
    )

    body = client.get("/config/prompts").json()

    assert body["petra"] == "default petra"
    assert body["petra_is_override"] is False


def test_saving_a_prompt_writes_the_override_object(bop, prompt_api):
    client, store, prompts = prompt_api()

    assert client.post("/config/prompts/petra", json={"prompt": "new petra"}).json() == {
        "ok": True
    }

    (override_key,) = keys_for(bop, prompts.PETRA_OVERRIDE)
    assert json.loads(store.objects[override_key]) == {"prompt": "new petra"}


def test_a_saved_prompt_keeps_non_ascii_characters_readable(bop, prompt_api):
    """`ensure_ascii=False`, so the object stays human-editable outside the app."""
    client, store, prompts = prompt_api()
    client.post("/config/prompts/petra", json={"prompt": "café — naïve"})

    (override_key,) = keys_for(bop, prompts.PETRA_OVERRIDE)
    assert "café — naïve" in store.objects[override_key].decode("utf-8")


def test_resetting_deletes_the_override_and_returns_the_default(bop, prompt_api):
    prompts = bop["prompts"]
    default_petra, override_petra = keys_for(bop, prompts.PETRA_DEFAULT, prompts.PETRA_OVERRIDE)
    client, store, _p = prompt_api(
        {default_petra: b"default petra", override_petra: b'{"prompt": "edited"}'}
    )

    body = client.post("/config/prompts/petra/reset").json()

    assert body == {"prompt": "default petra"}
    assert override_petra in store.deleted


def test_resetting_a_prompt_that_was_never_overridden_is_not_an_error(bop, prompt_api):
    """Best effort by design: the screen offers Reset whether or not one exists."""
    prompts = bop["prompts"]
    (default_petra,) = keys_for(bop, prompts.PETRA_DEFAULT)
    client, _store, _p = prompt_api({default_petra: b"default petra"})

    assert client.post("/config/prompts/petra/reset").status_code == 200


def test_the_reviewer_prompt_has_the_same_save_and_reset_behaviour(bop, prompt_api):
    prompts = bop["prompts"]
    default_reviewer, override_reviewer = keys_for(
        bop, prompts.REVIEWER_DEFAULT, prompts.REVIEWER_OVERRIDE
    )
    client, store, _p = prompt_api({default_reviewer: b"default reviewer"})

    client.post("/config/prompts/reviewer", json={"prompt": "new reviewer"})
    assert json.loads(store.objects[override_reviewer]) == {"prompt": "new reviewer"}

    body = client.post("/config/prompts/reviewer/reset").json()
    assert body == {"prompt": "default reviewer"}
    assert override_reviewer in store.deleted


def test_saving_a_prompt_requires_a_body(bop, prompt_api):
    """`PromptIn` is the whole validation; without it an empty POST would store `None`."""
    client, _store, _p = prompt_api()
    assert client.post("/config/prompts/petra", json={}).status_code == 422


def test_the_endpoints_are_closed_to_anonymous_callers(bop, monkeypatch):
    """The override is global, so an unauthenticated write would change everyone's output."""
    router_module = bop["router"]
    monkeypatch.setattr(router_module, "get_object_store", lambda: FakeStore())

    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    assert client.get("/config/prompts").status_code == 401
    assert client.post("/config/prompts/petra", json={"prompt": "x"}).status_code == 401


def test_a_reviewer_override_is_served_independently_of_petra(bop, prompt_api):
    """The two prompts override separately; a shared flag would misreport one of them."""
    prompts = bop["prompts"]
    default_petra, default_reviewer, override_reviewer = keys_for(
        bop, prompts.PETRA_DEFAULT, prompts.REVIEWER_DEFAULT, prompts.REVIEWER_OVERRIDE
    )
    client, _store, _p = prompt_api(
        {
            default_petra: b"default petra",
            default_reviewer: b"default reviewer",
            override_reviewer: json.dumps({"prompt": "edited reviewer"}).encode(),
        }
    )

    body = client.get("/config/prompts").json()

    assert body["reviewer"] == "edited reviewer"
    assert body["reviewer_is_override"] is True
    assert body["petra"] == "default petra"
    assert body["petra_is_override"] is False


# ── the last uncovered edges of editing and schema ───────────────────────────
# Not this file's main subject, but these were the only statements left in the BOP
# silo once everything else reached 100%, and each is a real input the editor produces.


def test_a_line_break_inside_a_bullet_becomes_a_space(bop):
    """The editor emits `<br>` for a soft wrap inside one bullet; it is still one entry."""
    assert bop["editing"].html_to_list("<ul><li>first line<br>second line</li></ul>") == [
        "first line second line"
    ]


def test_a_line_break_outside_a_bullet_does_not_join_anything(bop):
    """Outside a list item the break is loose text, handled by the block path instead."""
    assert bop["editing"].html_to_list("<p>alpha<br>beta</p>") == ["alpha", "beta"]


@pytest.mark.parametrize(
    "value,expected", [("already a string", "already a string"), (None, ""), (7, "7")]
)
def test_rendering_a_non_list_as_html_stringifies_it_instead(bop, value, expected):
    """A section that used to hold a list can be saved as a plain string by the editor."""
    assert bop["editing"].list_to_html(value) == expected


def test_setting_a_dotted_path_creates_the_missing_levels(bop):
    """The editor saves `operating_procedure.startup` into a draft that may lack the parent."""
    document = {}
    bop["schema"].dotted_set(document, "operating_procedure.startup", "Open the valve")
    assert document == {"operating_procedure": {"startup": "Open the valve"}}


def test_a_non_container_in_the_middle_of_a_path_is_replaced(bop):
    """A model reply can put a string where the schema wants an object."""
    document = {"operating_procedure": "a bare string"}
    bop["schema"].dotted_set(document, "operating_procedure.startup", "Open the valve")
    assert document == {"operating_procedure": {"startup": "Open the valve"}}


def test_descending_through_a_list_is_refused_rather_than_guessed(bop):
    """Which element would the path mean? Raising beats writing to an arbitrary index."""
    document = {"safety": [{"hazard": "heat"}]}
    with pytest.raises(KeyError, match="non-dict"):
        bop["schema"].dotted_set(document, "safety.0.hazard", "cold")


def test_setting_a_leaf_on_a_non_dict_is_refused(bop):
    with pytest.raises(KeyError, match="non-dict"):
        bop["schema"].dotted_set(["not", "a", "dict"], "purpose", "text")
