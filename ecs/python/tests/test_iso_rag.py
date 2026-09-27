"""Naming the standard via Ask a Source (`silos/iso/rag.py`).

Offline: the LLM is a stub and the 60-second indexing wait is monkeypatched away.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubEmptyCompletion, StubLlmError

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "rag.py").is_file(), reason="the ISO silo is not present"
)


@pytest.fixture(scope="module")
def rag():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "rag.py", name="rag")


@pytest.fixture(autouse=True)
def no_waiting(rag, monkeypatch) -> list[float]:
    """Record the indexing wait instead of serving it."""
    recorded: list[float] = []
    monkeypatch.setattr(rag.time, "sleep", recorded.append)
    return recorded


def test_the_happy_path_returns_the_title_and_cleans_up(rag):
    llm = FakeLlm(
        rag=["ISO 20417:2021 Medical devices — Information supplied"],
        documents=[[{"filename": "SOP.pdf", "id": "doc-9"}]],
    )

    title = rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)

    assert title == "ISO 20417:2021 Medical devices — Information supplied"
    assert llm.ensured == [("iso-docs-extraction", "ISO standard documents for number extraction")]
    assert llm.uploaded[0]["filename"] == "SOP.pdf"
    # The document must not be left behind in a source everyone shares.
    assert llm.deleted == [("iso-docs-extraction", "doc-9")]


def test_the_upload_does_not_retry(rag):
    """A retried multipart upload that already succeeded server-side would index the
    document twice, and the cleanup deletes only the first filename match."""
    llm = FakeLlm(rag=["ISO 1"])
    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)
    assert llm.uploaded[0]["timeouts"] == (120,)


def test_the_indexing_wait_totals_sixty_seconds(rag, no_waiting):
    """backend.py:206. Split into steps only so it can report progress."""
    llm = FakeLlm(rag=["ISO 1"])
    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)
    assert sum(no_waiting) == 60


def test_the_wait_reports_progress_so_the_reaper_leaves_the_run_alone(rag):
    """A stage silent for longer than stale_claim_timeout_s (900s) gets re-queued while
    it is still running, so a 60-second blocking wait must beat."""
    messages: list[str] = []
    llm = FakeLlm(rag=["ISO 1"])

    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm, progress=messages.append)

    assert any("indexed" in message for message in messages)


def test_the_primary_query_uses_k_of_three(rag):
    llm = FakeLlm(rag=["ISO 20417:2021"])
    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)

    queries = llm.calls_of("rag")
    assert len(queries) == 1, "a good primary answer must not trigger the fallback"
    assert queries[0]["k"] == 3
    assert "full title" in queries[0]["question"]


@pytest.mark.parametrize(
    "primary",
    ["", "   ", StubEmptyCompletion("no content"), StubLlmError("502 from Iliad")],
    ids=["empty", "whitespace", "empty-completion", "hard-failure"],
)
def test_an_unanswered_primary_falls_back_to_the_simpler_query(rag, primary):
    """ISO treated an empty answer and a non-ok response identically: try the simpler
    question. The shared client raises for both, so both are caught."""
    llm = FakeLlm(rag=[primary, "ISO 11608-4:2022"])

    assert rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm) == "ISO 11608-4:2022"

    queries = llm.calls_of("rag")
    assert [query["k"] for query in queries] == [3, 5]
    assert "just the number" in queries[1]["question"]


def test_both_queries_failing_yields_none(rag):
    """None is the signal the caller needs: it then falls back to the PDF's metadata
    title and finally to the filename."""
    llm = FakeLlm(rag=["", ""])
    assert rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm) is None


def test_a_failure_before_the_queries_yields_none_and_still_cleans_up(rag):
    class Exploding(FakeLlm):
        def upload_document(self, *args, **kwargs):
            raise StubLlmError("gateway down")

    llm = Exploding(documents=[[{"name": "SOP.pdf", "document_id": "doc-3"}]])
    assert rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm) is None
    # The finally block runs regardless, so a partial upload cannot leak.
    assert llm.deleted == [("iso-docs-extraction", "doc-3")]


def test_cleanup_matches_either_key_the_gateway_might_use(rag):
    llm = FakeLlm(rag=["ISO 1"], documents=[{"documents": [{"name": "SOP.pdf", "id": "d7"}]}])
    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)
    assert llm.deleted == [("iso-docs-extraction", "d7")]


def test_cleanup_failure_is_swallowed(rag):
    """Losing the cleanup must not lose the title the caller just paid for."""

    class Stubborn(FakeLlm):
        def list_documents(self, source):
            raise StubLlmError("cannot list")

    llm = Stubborn(rag=["ISO 20417:2021"])
    assert rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm) == "ISO 20417:2021"


def test_an_unknown_document_is_not_deleted(rag):
    llm = FakeLlm(rag=["ISO 1"], documents=[[{"filename": "someone-else.pdf", "id": "x"}]])
    rag.get_iso_number(b"%PDF-1.4", "SOP.pdf", llm=llm)
    assert llm.deleted == []
