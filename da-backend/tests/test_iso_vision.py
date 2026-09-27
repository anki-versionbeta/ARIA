"""Reading pages with the vision model (`silos/iso/vision.py`).

Offline: the model is a stub. What is asserted is the request shape and the two
fallbacks, because whether the model returns good text for a real ISO page is a
golden-file question, not a unit-test one.
"""

from __future__ import annotations

import json

import pytest

from da_platform.settings import REPO_ROOT
from da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

ISO_DIR = REPO_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "vision.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def vision():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "vision.py", name="vision")


def write_pdf(path, pages: int = 3):
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page {index}", fontsize=12)
    document.save(str(path))
    document.close()
    return str(path)


def page_reply(text: str = "Some page text") -> str:
    return json.dumps({"text": text, "images": [], "tables": []})


# ── rendering ─────────────────────────────────────────────────────────────────


def test_render_returns_base64_and_the_page_size_in_points(vision, tmp_path):
    import fitz

    document = fitz.open(write_pdf(tmp_path / "a.pdf", pages=1))
    try:
        b64, width, height = vision._render_page_to_base64(document, 0)
    finally:
        document.close()

    assert isinstance(b64, str) and len(b64) > 100
    # A4 in points, which is what the normalised bboxes are relative to.
    assert round(width) == 595
    assert round(height) == 842


def test_render_honours_the_dpi(vision, tmp_path):
    """150 dpi is the source's value and it sets the model's effective resolution."""
    import fitz

    document = fitz.open(write_pdf(tmp_path / "a.pdf", pages=1))
    try:
        small, _, _ = vision._render_page_to_base64(document, 0, dpi=72)
        large, _, _ = vision._render_page_to_base64(document, 0, dpi=150)
    finally:
        document.close()

    assert len(large) > len(small)


# ── the request shape ─────────────────────────────────────────────────────────


def test_the_page_call_pins_the_model_budget_and_timeout(vision, tmp_path):
    """ISO uses gpt-5.2 for vision while BOP uses a different model, so the client's
    own default would be wrong here."""
    llm = FakeLlm(chat_vision=[page_reply()])

    vision._extract_page_via_llm(write_pdf(tmp_path / "a.pdf", pages=1), 0, llm)

    call = llm.calls_of("chat_vision")[0]
    assert call["model"] == "gpt-5.2"
    assert call["max_tokens"] == 4096
    assert call["timeouts"] == (120,)
    assert call["images"] == 1
    assert "Return ONLY a valid JSON object" in call["prompt"]


def test_the_page_dict_has_exactly_the_expected_keys(vision, tmp_path):
    """Downstream code and the persisted llm.json both depend on this shape."""
    llm = FakeLlm(chat_vision=[page_reply("Hello")])

    page = vision._extract_page_via_llm(write_pdf(tmp_path / "a.pdf", pages=1), 0, llm)

    assert set(page) == {
        "page_idx",
        "text",
        "images",
        "tables",
        "page_w_pts",
        "page_h_pts",
        "b64",
    }
    assert page["page_idx"] == 0
    assert page["text"] == "Hello"


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"text": "Fenced", "images": [], "tables": []}\n```',
        '```\n{"text": "Fenced", "images": [], "tables": []}\n```',
    ],
    ids=["language-tagged", "bare"],
)
def test_a_markdown_fence_is_stripped(vision, tmp_path, reply):
    """The gateway wraps JSON in a fence despite being told not to."""
    llm = FakeLlm(chat_vision=[reply])
    page = vision._extract_page_via_llm(write_pdf(tmp_path / "a.pdf", pages=1), 0, llm)
    assert page["text"] == "Fenced"


def test_the_bbox_and_caption_payload_survives(vision, tmp_path):
    reply = json.dumps(
        {
            "text": "Body",
            "images": [{"caption": "Figure 1 — A diagram", "bbox_norm": [0.1, 0.2, 0.9, 0.5]}],
            "tables": [{"caption": "Table E.1 — Data", "bbox_norm": [0.1, 0.6, 0.9, 0.8], "data": [["a"]]}],
        }
    )
    llm = FakeLlm(chat_vision=[reply])

    page = vision._extract_page_via_llm(write_pdf(tmp_path / "a.pdf", pages=1), 0, llm)

    assert page["images"][0]["caption"] == "Figure 1 — A diagram"
    assert page["tables"][0]["bbox_norm"] == [0.1, 0.6, 0.9, 0.8]


# ── the two fallbacks ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("reply", ["not json at all", "", "[1, 2, 3]"])
def test_an_unparseable_reply_becomes_an_empty_page(vision, tmp_path, reply):
    """Losing one page of two hundred beats losing the document."""
    llm = FakeLlm(chat_vision=[reply])

    page = vision._extract_page_via_llm(write_pdf(tmp_path / "a.pdf", pages=1), 0, llm)

    assert page["text"] == ""
    assert page["images"] == [] and page["tables"] == []
    # The render succeeded, so the real page size is still reported.
    assert round(page["page_w_pts"]) == 595


def test_a_failed_page_falls_back_to_an_a4_placeholder(vision, tmp_path):
    """The per-page worker guard (backend.py:742-752). The A4 size is a guess, but a
    missing one would break every normalised bbox calculation downstream."""
    llm = FakeLlm(chat_vision=[StubLlmError("gateway down")] * 3)

    pages = vision.extract_all_pages(write_pdf(tmp_path / "a.pdf", pages=3), 3, llm=llm)

    assert [page["page_idx"] for page in pages] == [0, 1, 2]
    assert all(page["text"] == "" for page in pages)
    assert all(page["page_w_pts"] == 595.0 for page in pages)
    assert all(page["page_h_pts"] == 842.0 for page in pages)


# ── the whole document ────────────────────────────────────────────────────────


def test_every_page_is_read_once_and_in_order(vision, tmp_path):
    """Page order is load-bearing for the golden-file comparison, so it is asserted
    rather than assumed. `ex.map` yields in submission order; `as_completed` would not."""
    replies = [page_reply(f"Text of page {index}") for index in range(5)]
    llm = FakeLlm(chat_vision=replies)

    pages = vision.extract_all_pages(write_pdf(tmp_path / "a.pdf", pages=5), 5, llm=llm)

    assert len(pages) == 5
    assert [page["page_idx"] for page in pages] == [0, 1, 2, 3, 4]
    assert len(llm.calls_of("chat_vision")) == 5


def test_a_single_bad_page_does_not_lose_the_others(vision, tmp_path):
    """`max_workers=1` on purpose: the stub hands out replies in queue order, so which
    page receives which reply is otherwise up to thread scheduling. Page *ordering* under
    real concurrency is covered by the test above."""
    llm = FakeLlm(
        chat_vision=[page_reply("first"), "unparseable", page_reply("third")]
    )

    pages = vision.extract_all_pages(
        write_pdf(tmp_path / "a.pdf", pages=3), 3, llm=llm, max_workers=1
    )

    assert [page["text"] for page in pages] == ["first", "", "third"]


def test_extraction_reports_progress_per_page(vision, tmp_path):
    """Without this the reaper would re-queue a live 200-page run: the fan-out is
    otherwise silent for far longer than 900 seconds."""
    messages: list[str] = []
    llm = FakeLlm(chat_vision=[page_reply()] * 4)

    vision.extract_all_pages(
        write_pdf(tmp_path / "a.pdf", pages=4), 4, llm=llm, progress=messages.append
    )

    assert len(messages) == 4
    assert "page 1 of 4" in messages[0]
    assert "page 4 of 4" in messages[-1]


def test_the_base64_image_is_kept_on_the_page_dict(vision, tmp_path):
    """It is stripped before the pages are persisted, but the in-memory dict carries it
    because the pre-scan reuses the render."""
    llm = FakeLlm(chat_vision=[page_reply()])
    pages = vision.extract_all_pages(write_pdf(tmp_path / "a.pdf", pages=1), 1, llm=llm)
    assert pages[0]["b64"]
