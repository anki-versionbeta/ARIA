"""The ISO silo contract: what each of the four stages hands to the next (`silos/iso/silo.py`).

`tests/test_iso_plumbing.py` proves the silo is *discoverable* and that the range picker
parks and resumes a run. It never calls a stage, because the real stages want PyMuPDF, an
LLM and Textract. This file calls all four, with every module they delegate to replaced.

That substitution is the point, not a convenience. `silo.py` owns exactly four decisions
and nothing else:

  * which of `garble` / `vision` / `toc` / `rag` / `generate` is asked, in what order, and
    on which of the two branches (text layer vs vision);
  * what goes into each checkpoint, because a checkpoint is the contract between stages
    and a resume on another worker has nothing else;
  * the three fallbacks the original endpoints had -- a vision outline that finds nothing
    falls back to fitz bookmarks, an unreadable page dump degrades to the text path rather
    than failing, and the document title falls back RAG -> PDF metadata -> filename;
  * that the base64 page images are stripped before the page dump is stored.

`toc`, `rows`, `body`, `generate` and `garble` are separately tested at or near 100%, so
letting the real ones run here would mean a failure could be theirs. With them stubbed, a
failure is the wiring -- which is the only thing this file is for.

The storage, checkpoint and progress seams are real: a genuine `StageContext` over the
test session and object store. Faking those would fake the thing most likely to drift.
"""

from __future__ import annotations

import json
import logging

import fitz
import pytest

from api.backend.da_platform.engine import queue
from api.backend.da_platform.engine.context import StageContext
from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from api.backend.da_platform.storage import get_object_store
from tests.conftest import login, make_run
from tests.iso_fakes import FakeLlm

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "silo.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def silo():
    return _load_module("iso", ISO_DIR / "silo.py")


@pytest.fixture
def delegates(silo, monkeypatch):
    """Every ported module a stage imports, with a recording stub in place of each.

    Returns the module objects so a test can override one reply, plus a `calls` list that
    records the order the stages reached them in -- which is the ordering half of the
    contract.
    """
    from da_silos.iso import garble, generate, rag, toc, vision

    calls: list[str] = []

    def record(name, result):
        def stub(*args, **kwargs):
            calls.append(name)
            return result

        return stub

    monkeypatch.setattr(garble, "is_pdf_garbled", record("is_pdf_garbled", False))
    monkeypatch.setattr(vision, "extract_all_pages", record("extract_all_pages", []))
    monkeypatch.setattr(toc, "build_toc_from_llm_pages", record("build_toc_from_llm_pages", []))
    monkeypatch.setattr(toc, "extract_toc", record("extract_toc", []))
    monkeypatch.setattr(toc, "deduplicate_toc", lambda entries: list(entries))
    monkeypatch.setattr(rag, "get_iso_number", record("get_iso_number", None))
    monkeypatch.setattr(
        generate,
        "build_document",
        record("build_document", {"docx": b"PK\x03\x04docx", "filename": "out.docx"}),
    )
    return {
        "garble": garble,
        "vision": vision,
        "toc": toc,
        "rag": rag,
        "generate": generate,
        "calls": calls,
    }


# ── fixtures the stages need ──────────────────────────────────────────────────


def pdf_bytes(pages: int = 3, *, title: str = "") -> bytes:
    """A real PDF, because `ingest` and `outline` both open one with fitz to count its
    pages and read its metadata."""
    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=595, height=842)
    if title:
        document.set_metadata({"title": title})
    data = document.tobytes()
    document.close()
    return data


def iso_run(session, owner_id, *, body=None, status="running", stage="ingest"):
    run = make_run(session, owner_id=owner_id, title="SOP.pdf", silo="iso", status=status)
    run.stage = stage
    session.commit()
    record = next(item for item in run.files if item.kind == "input")
    get_object_store().put(record.storage_key, body if body is not None else pdf_bytes())
    return run


def context_for(session, run, silo, *, llm=None) -> StageContext:
    return StageContext(
        session,
        run,
        llm=llm or FakeLlm(),
        storage_prefix=silo.STORAGE_PREFIX,
        storage_folders=silo.STORAGE_FOLDERS,
    )


def outputs_of(run):
    return [item for item in run.files if item.kind == "output"]


# ── the contract itself ───────────────────────────────────────────────────────


def test_the_four_stage_names_each_resolve_to_a_two_argument_callable(silo):
    """The runner calls `getattr(module, stage)(run, ctx)`; a rename or an extra
    parameter strands every run at that stage."""
    import inspect

    for stage in silo.STAGES:
        function = getattr(silo, stage)
        assert list(inspect.signature(function).parameters) == ["run", "ctx"], stage


def test_the_range_prompt_the_user_sees_is_the_one_the_stage_parks_with(silo):
    assert silo.RANGE_PROMPT == "Select the sections to generate"
    assert silo.DOCX_CONTENT_TYPE.endswith("wordprocessingml.document")
    assert silo.LLM_PAGES_MEDIA == "llm.json"


# ── ingest ────────────────────────────────────────────────────────────────────


def test_ingest_on_the_text_path_counts_the_pages_and_stores_no_page_dump(
    client, session, silo, delegates
):
    """The cheap branch: a PDF with a usable text layer never calls vision, so there is
    nothing to checkpoint but the page count and the name to fall back on later."""
    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes(7))

    output = silo.ingest(run, context_for(session, run, silo))

    assert output == {
        "use_vision": False,
        "llm_pages_key": None,
        "total_pages": 7,
        "source_name": "source.pdf",
    }
    assert delegates["calls"] == ["is_pdf_garbled"]
    session.refresh(run)
    assert run.progress_pct == 20
    assert "text layer" in run.progress_message


def test_ingest_on_the_vision_path_stores_the_pages_and_strips_their_images(
    client, session, silo, delegates
):
    """Vision extraction of a 200-page standard is the most expensive thing this silo
    does, so its result is checkpointed and never repeated on a resume. The base64
    renders are the bulk of that payload and nothing downstream reads them -- keeping
    them would put tens of megabytes per run into the object store for nothing."""
    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes(2))
    ctx = context_for(session, run, silo)

    pages = [
        {"page": 1, "text": "Scope", "b64": "A" * 5000},
        {"page": 2, "text": "Terms", "b64": "B" * 5000},
    ]
    delegates["garble"].is_pdf_garbled = lambda *a, **k: True
    delegates["vision"].extract_all_pages = lambda *a, **k: pages

    output = silo.ingest(run, ctx)

    assert output["use_vision"] is True
    assert output["total_pages"] == 2
    with ctx.storage.open_key(output["llm_pages_key"]) as handle:
        stored = json.loads(handle.read().decode("utf-8"))
    assert stored == [{"page": 1, "text": "Scope"}, {"page": 2, "text": "Terms"}]
    session.refresh(run)
    assert "by vision" in run.progress_message


def test_ingest_falls_back_to_a_generic_name_when_the_input_file_has_none(
    client, session, silo, delegates, monkeypatch
):
    """`source_name` is what the document title degrades to, so it must never be None."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, silo)
    monkeypatch.setattr(ctx.storage, "input_filename", lambda _run: None)

    assert silo.ingest(run, ctx)["source_name"] == "source.pdf"


def test_ingest_reports_progress_before_it_reads_anything(client, session, silo, delegates):
    """`is_pdf_garbled` samples pages with the LLM and is silent for minutes; the reaper
    re-queues a run whose heartbeat is 900s old, so the message has to come first."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    seen: list[tuple[str, int | None]] = []

    ctx = context_for(session, run, silo)

    def spy(*args, **kwargs):
        seen.append((run.progress_message, run.progress_pct))
        return False

    delegates["garble"].is_pdf_garbled = spy
    silo.ingest(run, ctx)

    assert seen == [("Reading the PDF", 5)]


# ── the page-dump round trip ──────────────────────────────────────────────────


def test_a_run_with_no_stored_page_dump_reloads_nothing(client, session, silo):
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, silo)

    assert silo._load_llm_pages(ctx, {}) is None
    assert silo._load_llm_pages(ctx, {"llm_pages_key": None}) is None


def test_a_stored_page_dump_is_reloaded_verbatim(client, session, silo):
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, silo)
    pages = [{"page": 1, "text": "Scope", "b64": "drop me"}]

    key = silo._save_llm_pages(run, ctx, pages)
    assert silo._load_llm_pages(ctx, {"llm_pages_key": key}) == [
        {"page": 1, "text": "Scope"}
    ]


def test_an_unreadable_page_dump_degrades_to_the_text_path_instead_of_failing(
    client, session, silo, caplog
):
    """A lost or corrupt object costs the vision text, which the run can survive; failing
    the run instead would throw away everything ingest paid for."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    ctx = context_for(session, run, silo)

    with caplog.at_level(logging.WARNING):
        assert silo._load_llm_pages(ctx, {"llm_pages_key": "no/such/key"}) is None
    assert "unreadable extracted pages" in caplog.text


# ── outline ───────────────────────────────────────────────────────────────────


def test_outline_on_the_text_path_reads_the_toc_from_the_pdf_itself(
    client, session, silo, delegates
):
    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes(9))
    queue.save_checkpoint(
        session, run.id, "ingest", {"use_vision": False, "source_name": "ISO-7010.pdf"}
    )
    entries = [{"title": "1 Scope", "page": 1}, {"title": "2 Terms", "page": 3}]
    delegates["toc"].extract_toc = lambda *a, **k: entries

    output = silo.outline(run, context_for(session, run, silo))

    assert output == {"toc": entries, "doc_title": "ISO-7010", "total_pages": 9}
    # The vision parser is not consulted at all on this branch.
    assert "build_toc_from_llm_pages" not in delegates["calls"]


def test_outline_on_the_vision_path_parses_only_the_contents_pages(
    client, session, silo, delegates
):
    """The vision outline is built from the standard's own contents page, so it produces
    no dot-leader artefacts or table-row noise -- and the PDF is then not consulted."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    key = "ISO_Doc_Generation/memry/pages.json"
    get_object_store().put(key, json.dumps([{"page": 2, "text": "Contents"}]).encode())
    queue.save_checkpoint(
        session,
        run.id,
        "ingest",
        {"use_vision": True, "llm_pages_key": key, "source_name": "scan.pdf"},
    )
    seen: list = []

    def from_pages(pages):
        seen.append(pages)
        return [{"title": "1 Scope", "page": 1}]

    delegates["toc"].build_toc_from_llm_pages = from_pages

    output = silo.outline(run, context_for(session, run, silo))

    assert output["toc"] == [{"title": "1 Scope", "page": 1}]
    assert seen == [[{"page": 2, "text": "Contents"}]], "the stored dump is what is parsed"
    assert "extract_toc" not in delegates["calls"]


def test_a_vision_outline_that_finds_nothing_falls_back_to_the_pdf_bookmarks(
    client, session, silo, delegates
):
    """A garbled scan sometimes still carries usable bookmarks. Without this the range
    picker would show an empty list and the run could not be steered at all."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    queue.save_checkpoint(session, run.id, "ingest", {"use_vision": True})
    delegates["toc"].build_toc_from_llm_pages = lambda pages: []
    delegates["toc"].extract_toc = lambda *a, **k: [{"title": "1 Scope", "page": 1}]

    output = silo.outline(run, context_for(session, run, silo))

    assert output["toc"] == [{"title": "1 Scope", "page": 1}]


def test_the_outline_deduplicates_on_both_branches(client, session, silo, delegates):
    """The guardrail is applied after the branch, not inside it, so a body-text hit
    duplicating a contents entry is removed however the entries were found."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    queue.save_checkpoint(session, run.id, "ingest", {"use_vision": False})
    delegates["toc"].extract_toc = lambda *a, **k: [{"title": "1 Scope"}] * 3
    delegates["toc"].deduplicate_toc = lambda entries: entries[:1]

    assert silo.outline(run, context_for(session, run, silo))["toc"] == [{"title": "1 Scope"}]


@pytest.mark.parametrize(
    "rag_title,metadata_title,expected",
    [
        ("ISO 20417:2021", "Medical devices", "ISO 20417:2021"),
        (None, "Medical devices", "Medical devices"),
        (None, "   ", "source"),
        ("", "", "source"),
    ],
)
def test_the_document_title_prefers_rag_then_the_pdf_metadata_then_the_filename(
    client, session, silo, delegates, rag_title, metadata_title, expected
):
    """This title names the FILE the user downloads. Three sources because a standard's
    number is often only in its body text, often only in its metadata, and sometimes only
    in the name it was uploaded under."""
    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes(2, title=metadata_title))
    queue.save_checkpoint(session, run.id, "ingest", {"source_name": "source.pdf"})
    delegates["rag"].get_iso_number = lambda *a, **k: rag_title

    assert silo.outline(run, context_for(session, run, silo))["doc_title"] == expected


def test_outline_without_an_ingest_checkpoint_uses_the_fallback_name(
    client, session, silo, delegates
):
    """Belt and braces for a hand-requeued run: a missing checkpoint must not raise."""
    asha = login(client)
    run = iso_run(session, asha["id"], body=pdf_bytes(4))

    output = silo.outline(run, context_for(session, run, silo))

    assert output["doc_title"] == "source"
    assert output["total_pages"] == 4


def test_outline_identifies_the_standard_from_the_uploaded_bytes(
    client, session, silo, delegates
):
    """RAG is asked with the whole input document, not the local scratch path: the source
    it indexes has to be the same bytes the user uploaded."""
    asha = login(client)
    body = pdf_bytes(2)
    run = iso_run(session, asha["id"], body=body)
    queue.save_checkpoint(session, run.id, "ingest", {"source_name": "ISO-7010.pdf"})
    seen: list = []

    def spy(data, name, **kwargs):
        seen.append((data, name, run.progress_message, run.progress_pct))
        return "ISO 7010"

    delegates["rag"].get_iso_number = spy
    silo.outline(run, context_for(session, run, silo))

    assert seen[0][0] == body
    assert seen[0][1] == "ISO-7010.pdf"
    assert seen[0][2:] == ("Identifying the standard", 26)


# ── await_range ───────────────────────────────────────────────────────────────


def test_await_range_parks_the_run_with_the_prompt_the_endpoint_answers(
    client, session, silo
):
    """The wait that used to be a thread blocked between two HTTP requests. Returning a
    Pause is how the worker is released; the router writes the checkpoint."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    queue.save_checkpoint(
        session, run.id, "outline", {"toc": [{"title": "1 Scope"}, {"title": "2 Terms"}]}
    )

    paused = silo.await_range(run, context_for(session, run, silo))

    assert paused.message == silo.RANGE_PROMPT
    session.refresh(run)
    assert run.progress_message == f"{silo.RANGE_PROMPT} — 2 found"
    assert run.progress_pct == 30


@pytest.mark.parametrize(
    "checkpoint", [None, {}, {"toc": None}, {"toc": []}], ids=["missing", "empty", "null", "none"]
)
def test_await_range_still_parks_when_the_outline_produced_no_sections(
    client, session, silo, checkpoint
):
    """Parking with "0 found" beats failing: the user can see the outline is empty and
    fork or re-upload, whereas a failed stage loses the ingest."""
    asha = login(client)
    run = iso_run(session, asha["id"])
    if checkpoint is not None:
        queue.save_checkpoint(session, run.id, "outline", checkpoint)

    assert silo.await_range(run, context_for(session, run, silo)) is not None
    session.refresh(run)
    assert run.progress_message.endswith("0 found")


# ── build ─────────────────────────────────────────────────────────────────────


def parked(session, owner_id, *, ingest=None, outline=None, chosen=None, body=None):
    """A run with all three earlier stages checkpointed, ready for `build`."""
    run = iso_run(session, owner_id, status="running", stage="build", body=body)
    for stage, output in (
        ("ingest", ingest),
        ("outline", outline),
        ("await_range", chosen),
    ):
        if output is not None:
            queue.save_checkpoint(session, run.id, stage, output)
    return run


def test_build_hands_the_generator_everything_the_three_earlier_stages_decided(
    client, session, silo, delegates
):
    """`build` is a funnel: three checkpoints in, one DOCX out. Every argument here came
    from a different stage, and a run that resumes on a fresh worker has nothing else."""
    asha = login(client)
    key = "ISO_Doc_Generation/memry/pages.json"
    get_object_store().put(key, json.dumps([{"page": 1, "text": "Scope"}]).encode())
    run = parked(
        session,
        asha["id"],
        ingest={"use_vision": True, "llm_pages_key": key, "total_pages": 40},
        outline={"toc": [{"title": "1 Scope"}], "doc_title": "ISO 7010", "total_pages": 42},
        chosen={"start_idx": 1, "end_idx": 3},
    )
    seen: list = []

    def spy(ctx, ws, **kwargs):
        seen.append(kwargs)
        assert ws.pdf_path.endswith(".pdf"), "the generator needs a real local PDF path"
        return {"docx": b"PK\x03\x04", "filename": "ISO 7010.docx"}

    delegates["generate"].build_document = spy
    output = silo.build(run, context_for(session, run, silo))

    assert seen[0] == {
        "toc": [{"title": "1 Scope"}],
        "doc_title": "ISO 7010",
        "start_idx": 1,
        "end_idx": 3,
        "use_vision": True,
        "llm_pages": [{"page": 1, "text": "Scope"}],
        # The outline's count wins over ingest's: outline is the later measurement.
        "total_pages": 42,
    }
    assert output == {"bytes": 4}


def test_build_attaches_the_document_as_a_docx_the_browser_will_download(
    client, session, silo, delegates
):
    asha = login(client)
    run = parked(session, asha["id"], chosen={"start_idx": 0, "end_idx": 0})
    delegates["generate"].build_document = lambda *a, **k: {
        "docx": b"PK\x03\x04body",
        "filename": "Assessment.docx",
    }

    assert silo.build(run, context_for(session, run, silo)) == {"bytes": 8}

    session.refresh(run)
    attached = outputs_of(run)
    assert len(attached) == 1
    assert attached[0].filename == "Assessment.docx"
    assert attached[0].content_type == silo.DOCX_CONTENT_TYPE
    with get_object_store().open(attached[0].storage_key) as handle:
        assert handle.read() == b"PK\x03\x04body"
    assert run.progress_pct == 100
    assert run.progress_message == "Document ready"


def test_build_honours_a_content_type_the_generator_chose_for_itself(
    client, session, silo, delegates
):
    """The default is only a default; a generator emitting something else must not have
    it relabelled as a Word document."""
    asha = login(client)
    run = parked(session, asha["id"], chosen={"start_idx": 0, "end_idx": 0})
    delegates["generate"].build_document = lambda *a, **k: {
        "docx": b"%PDF-1.4",
        "filename": "Assessment.pdf",
        "content_type": "application/pdf",
    }

    silo.build(run, context_for(session, run, silo))

    session.refresh(run)
    assert outputs_of(run)[0].content_type == "application/pdf"


@pytest.mark.parametrize(
    "ingest,outline,expected",
    [
        ({"total_pages": 40}, {"total_pages": 42}, 42),
        ({"total_pages": 40}, {}, 40),           # outline never completed its count
        ({"total_pages": 40}, {"total_pages": 0}, 40),   # a zero is treated as absent
        ({}, {}, 0),
    ],
)
def test_the_page_count_falls_back_from_the_outline_to_the_ingest_to_zero(
    client, session, silo, delegates, ingest, outline, expected
):
    asha = login(client)
    run = parked(session, asha["id"], ingest=ingest, outline=outline)
    seen: list = []
    delegates["generate"].build_document = lambda ctx, ws, **kwargs: (
        seen.append(kwargs) or {"docx": b"x", "filename": "a.docx"}
    )

    silo.build(run, context_for(session, run, silo))

    assert seen[0]["total_pages"] == expected


def test_build_defaults_to_the_first_section_when_no_range_was_recorded(
    client, session, silo, delegates
):
    """The router validates the range before it re-queues, so a missing `await_range`
    checkpoint means a hand-requeued run. Section 0 to 0 is the safe reading."""
    asha = login(client)
    run = parked(session, asha["id"], outline={"toc": [], "doc_title": ""})
    seen: list = []
    delegates["generate"].build_document = lambda ctx, ws, **kwargs: (
        seen.append(kwargs) or {"docx": b"x", "filename": "a.docx"}
    )

    silo.build(run, context_for(session, run, silo))

    assert (seen[0]["start_idx"], seen[0]["end_idx"]) == (0, 0)
    assert seen[0]["toc"] == []
    assert seen[0]["doc_title"] == ""
    assert seen[0]["use_vision"] is False
    assert seen[0]["llm_pages"] is None


def test_a_generator_failure_leaves_no_half_written_output_attached(
    client, session, silo, delegates
):
    """The stage raises, the runner marks the run failed and the platform deletes the
    scratch. What must not happen is a truncated DOCX in the downloads folder that the
    UI would then offer for download."""
    asha = login(client)
    run = parked(session, asha["id"], chosen={"start_idx": 0, "end_idx": 1})

    def explode(*args, **kwargs):
        raise RuntimeError("no section matched the chosen range")

    delegates["generate"].build_document = explode
    with pytest.raises(RuntimeError, match="no section matched"):
        silo.build(run, context_for(session, run, silo))

    session.refresh(run)
    assert outputs_of(run) == []


def test_the_range_the_user_picked_survives_a_worker_change(
    client, session, silo, delegates
):
    """The whole reason the pause is a checkpoint rather than a blocked thread: `build`
    reads the answer from storage, so the worker that generates need not be the one that
    uploaded."""
    asha = login(client)
    run = parked(session, asha["id"], chosen={"start_idx": 2, "end_idx": 5})
    seen: list = []
    delegates["generate"].build_document = lambda ctx, ws, **kwargs: (
        seen.append(kwargs) or {"docx": b"x", "filename": "a.docx"}
    )

    # A second, independent context stands in for a second replica.
    silo.build(run, context_for(session, run, silo))

    assert (seen[0]["start_idx"], seen[0]["end_idx"]) == (2, 5)
