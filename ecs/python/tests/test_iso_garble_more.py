"""The routing decision's unexercised branches (`silos/iso/garble.py`).

Companion to `test_iso_garble.py`, which pins the calibrated thresholds and the happy
paths of all three signals. This file adds only what that file leaves untouched: the
early exits of `_is_garbled_text` (checks 1, 3 and the all-punctuation page), and every
degraded path of `is_pdf_garbled` / `_llm_garble_check` — a reader that disagrees with
`fitz` about the page count, a page whose extraction raises, `fitz` refusing to open the
file, the thread pool failing, and signal C blowing up. Those paths matter more than
their line count suggests: each one decides between "read the text layer" and "one
Textract page-analysis per page", and every one of them is supposed to fail *towards*
the cheap answer.

`pypdf` is shimmed with a fake reader where the point is per-page control (a page that
raises, a reader that reports more pages than the file has); a real `fitz`-written PDF
supplies the file underneath, because signals B and C open the path themselves.
"""

from __future__ import annotations

import types

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "garble.py").is_file(), reason="the ISO silo is not present"
)

# 35 tokens, every one of them in `_ISO_COMMON_WORDS`, so checks 2a and 2b pass.
CLEAN_WORDS = (
    "the of and to in is that for are shall be this with or an as not from by which "
    "when if on at it have has all its each may used other than they"
)
# Lowercase cipher tokens: they carry `_CIPHER_ADJ` hits (signal B) but cannot match
# GARBLE_PAT, which needs uppercase letters around the symbol (signal A check 3). That
# is exactly the partially-ciphered document signal B was added for.
LOWER_CIPHER = "h?=>ji <kd9j?ed ^eh ?d^ehc7j?ed"
# Numeric/symbol soup: signal A's check 2a calls this garbled.
JUNK = "1 2 3 4 5 6 7 8 9 10 11 12 13 %% ++ == 14 15 16 17 18 19 20 21 22"

CONFIDENT_GARBLED = '{"verdict":"GARBLED","confidence":0.95}'
USABLE = '{"verdict":"USABLE","confidence":1.0}'


@pytest.fixture(scope="module")
def garble():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "garble.py", name="garble")


def write_pdf(path, *, pages=8, text=CLEAN_WORDS, blank_pages=()):
    """A real PDF for the code that opens the path itself (signals B and C)."""
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        if index in blank_pages:
            continue
        page.insert_textbox(fitz.Rect(72, 90, 523, 760), text, fontsize=9)
    document.save(str(path))
    document.close()
    return str(path)


class FakePypdfPage:
    def __init__(self, text="", error=None):
        self._text = text
        self._error = error

    def extract_text(self):
        if self._error is not None:
            raise self._error
        return self._text


def fake_pypdf(monkeypatch, garble, texts):
    """Shim `garble.pypdf` so the per-page verdicts of signal A can be dictated.

    An `Exception` instance in `texts` becomes a page that raises on extraction.
    """
    pages = [
        FakePypdfPage(error=t) if isinstance(t, Exception) else FakePypdfPage(text=t)
        for t in texts
    ]
    reader = types.SimpleNamespace(pages=pages)
    monkeypatch.setattr(garble, "pypdf", types.SimpleNamespace(PdfReader=lambda _p: reader))
    return reader


# ── the text heuristic's remaining exits ──────────────────────────────────────


def test_a_page_of_control_characters_is_garbled_by_the_printable_ratio(garble):
    """Check 1, the classic fully-encrypted symptom: half the characters do not print."""
    text = "\x01" * 30 + "the quick brown fox jumps"
    assert garble._is_garbled_text(text) is True


def test_the_printable_ratio_threshold_holds_on_both_sides(garble):
    """The knob is `threshold`; 0.4 of the characters may be unprintable, not more."""
    body = "the requirements of this document shall be applied " * 3   # 153 characters
    # 103/256 = 0.402, over the line.
    assert garble._is_garbled_text(body + "\x01" * 103, threshold=0.4) is True
    # 102/255 = 0.400 exactly, so check 1 declines and the word checks pass the page.
    assert garble._is_garbled_text(body + "\x01" * 102, threshold=0.4) is False


def test_a_page_of_nothing_but_punctuation_is_garbled(garble):
    """Every token strips down to nothing, so there is no word evidence either way and
    the only safe answer is "send it to vision"."""
    assert garble._is_garbled_text("!!! ??? ... --- *** ~~~ ^^^ &&& %%% $$$ ///") is True


@pytest.mark.parametrize(
    "cipher_tokens,garbled",
    [(1, False), (2, True)],
    ids=["one-in-21-passes", "two-in-22-fails"],
)
def test_check_three_fires_above_eight_percent_of_uppercase_cipher_tokens(
    garble, cipher_tokens, garbled
):
    """Check 3 counts `[A-Z]<sym>[A-Z]` tokens and fires above 8 %. Twenty real words
    plus two such tokens is 2/22 = 0.09; plus one is 1/21 = 0.05.

    The words are all in the ISO vocabulary so checks 2a and 2b cannot be what answers.
    """
    words = "the of and to in is that for are shall be this with or an as not from by which"
    text = words + " " + " ".join(["A>B", "C:D"][:cipher_tokens])
    assert garble._is_garbled_text(text) is garbled


# ── signal C in isolation ─────────────────────────────────────────────────────


def test_the_classifier_declines_to_call_the_model_on_a_near_empty_page(garble):
    """Under 20 characters is not a judgement the model can make, and asking anyway
    would cost one call per near-blank page in the document."""
    llm = FakeLlm(chat=[])
    assert garble._llm_classify_page_garbled("too short", llm) is None
    assert garble._llm_classify_page_garbled("", llm) is None
    assert llm.calls == [], "no request should have been made"


def test_signal_c_skips_indices_past_the_end_of_the_document(garble, tmp_path):
    """The sample indices come from pypdf's page count; a disagreement with fitz must
    not raise."""
    path = write_pdf(tmp_path / "short.pdf", pages=3)
    llm = FakeLlm(chat=[CONFIDENT_GARBLED, CONFIDENT_GARBLED])

    assert garble._llm_garble_check(path, [1, 2, 99, 400], llm) is True
    # Only the two pages that exist were judged.
    assert len(llm.calls_of("chat")) == 2


def test_signal_c_returns_clean_when_fitz_cannot_open_the_file(garble, tmp_path):
    """Fail-safe: an unreadable file falls back to the heuristic verdict rather than
    routing the document to the expensive path."""
    llm = FakeLlm(chat=[CONFIDENT_GARBLED] * 4)
    assert garble._llm_garble_check(str(tmp_path / "absent.pdf"), [1, 2], llm) is False
    assert llm.calls == []


def test_signal_c_returns_clean_when_no_sampled_page_has_enough_text(garble, tmp_path):
    """An image-only PDF gives the classifier nothing to read."""
    path = write_pdf(tmp_path / "blank.pdf", pages=4, blank_pages=(0, 1, 2, 3))
    llm = FakeLlm(chat=[])
    assert garble._llm_garble_check(path, [1, 2, 3], llm) is False
    assert llm.calls == []


def test_signal_c_treats_a_worker_that_raises_as_an_unjudged_page(garble, tmp_path, monkeypatch):
    """`_llm_classify_page_garbled` swallows its own failures, so a raising worker means
    something unforeseen; the pool result must still not propagate."""
    path = write_pdf(tmp_path / "clean.pdf", pages=4)
    monkeypatch.setattr(
        garble,
        "_llm_classify_page_garbled",
        lambda text, llm: (_ for _ in ()).throw(RuntimeError("worker exploded")),
    )
    assert garble._llm_garble_check(path, [1, 2, 3], FakeLlm()) is False


def test_signal_c_returns_clean_when_the_thread_pool_cannot_start(garble, tmp_path, monkeypatch):
    """A worker-starvation failure inside the pool is not a reason to bill Textract for
    every page."""
    path = write_pdf(tmp_path / "clean.pdf", pages=4)

    def no_pool(*_args, **_kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(garble, "ThreadPoolExecutor", no_pool)
    assert garble._llm_garble_check(path, [1, 2, 3], FakeLlm()) is False


# ── the whole-document verdict, degraded paths ────────────────────────────────


def test_a_pdf_with_zero_pages_is_routed_to_vision(garble, monkeypatch):
    """fitz cannot write a page-less PDF, so the reader is shimmed to report one."""
    fake_pypdf(monkeypatch, garble, [])
    assert garble.is_pdf_garbled("whatever.pdf", llm=FakeLlm()) is True


def test_a_minority_of_garbled_pages_does_not_route_to_vision(garble, tmp_path, monkeypatch):
    """Signal A needs half the *checked* pages, not one of them: two junk pages out of
    six is a table-heavy document, not a ciphered one."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    fake_pypdf(monkeypatch, garble, ["cover"] + [JUNK, JUNK] + [CLEAN_WORDS] * 4)
    llm = FakeLlm(chat=[USABLE] * 6)

    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_half_the_pages_garbled_routes_to_vision_without_asking_the_model(
    garble, tmp_path, monkeypatch
):
    """Signal A is conclusive on its own — reaching signal C would mean paying for LLM
    calls to confirm a verdict already made."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    fake_pypdf(monkeypatch, garble, ["cover"] + [JUNK] * 3 + [CLEAN_WORDS] * 3)
    llm = FakeLlm(chat=[USABLE] * 6)

    assert garble.is_pdf_garbled(path, llm=llm) is True
    assert llm.calls == [], "signal A must short-circuit B and C"


def test_a_page_whose_extraction_raises_is_left_out_of_the_ratio(garble, tmp_path, monkeypatch):
    """One damaged page object must not decide the document, nor abort the scan."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    fake_pypdf(
        monkeypatch,
        garble,
        ["cover", ValueError("bad xref"), JUNK] + [CLEAN_WORDS] * 4,
    )
    llm = FakeLlm(chat=[USABLE] * 6)

    # 1 junk / 5 successfully checked = 0.2, so the verdict is still clean.
    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_sample_indices_past_the_fitz_page_count_are_skipped_by_signal_b(
    garble, tmp_path, monkeypatch
):
    """pypdf and fitz can disagree on the page count of a damaged file; signal B must
    survive that rather than raising out of the verdict."""
    path = write_pdf(tmp_path / "three.pdf", pages=3)
    fake_pypdf(monkeypatch, garble, [CLEAN_WORDS] * 8)
    llm = FakeLlm(chat=[USABLE] * 8)

    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_a_blank_page_contributes_no_cipher_ratio(garble, tmp_path, monkeypatch):
    """Signal B averages over pages with text; an image-only page has no ratio to
    average and must not dilute the mean towards clean."""
    path = write_pdf(tmp_path / "mixed.pdf", pages=8, blank_pages=(2, 3, 4))
    fake_pypdf(monkeypatch, garble, [CLEAN_WORDS] * 8)
    llm = FakeLlm(chat=[USABLE] * 8)

    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_a_partially_ciphered_document_is_caught_by_signal_b_alone(garble, tmp_path):
    """The document signal B exists for: real words everywhere (so signal A passes) with
    ~10 % of tokens font-ciphered in lowercase, which check 3 cannot see."""
    path = write_pdf(
        tmp_path / "cipher.pdf",
        pages=8,
        text=" ".join([CLEAN_WORDS, LOWER_CIPHER] * 3),
    )
    llm = FakeLlm(chat=[USABLE] * 8)

    assert garble.is_pdf_garbled(path, llm=llm) is True
    assert llm.calls == [], "signal B must short-circuit the model backstop"


def test_signal_b_failing_to_open_the_file_does_not_stop_the_verdict(
    garble, tmp_path, monkeypatch
):
    """Signal A said clean; a fitz failure must leave that answer standing."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    fake_pypdf(monkeypatch, garble, [CLEAN_WORDS] * 7)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("cannot open broken document")

    monkeypatch.setattr(garble, "fitz", types.SimpleNamespace(open=refuse))

    assert garble.is_pdf_garbled(path, llm=FakeLlm(chat=[USABLE] * 7)) is False


def test_the_model_backstop_can_route_a_document_the_heuristics_pass(garble, tmp_path):
    """Signal C is the only signal that can still flip a document at this point, which is
    the whole reason it runs after A and B."""
    path = write_pdf(tmp_path / "clean.pdf", pages=8)
    llm = FakeLlm(chat=[CONFIDENT_GARBLED] * 8)
    messages: list[str] = []

    assert garble.is_pdf_garbled(path, llm=llm, progress=messages.append) is True
    assert any("model" in message for message in messages)


def test_signal_c_raising_outright_still_yields_a_clean_verdict(garble, tmp_path, monkeypatch):
    """Belt and braces around the backstop: even a failure that escapes
    `_llm_garble_check` must not break an upload."""
    path = write_pdf(tmp_path / "clean.pdf", pages=8)

    def explode(*_args, **_kwargs):
        raise RuntimeError("iliad is on fire")

    monkeypatch.setattr(garble, "_llm_garble_check", explode)

    assert garble.is_pdf_garbled(path, llm=FakeLlm()) is False


def test_the_sample_size_is_honoured(garble, tmp_path, monkeypatch):
    """`sample_count` bounds the number of pages read, and therefore the number of LLM
    calls signal C can make."""
    path = write_pdf(tmp_path / "long.pdf", pages=30)
    fake_pypdf(monkeypatch, garble, [CLEAN_WORDS] * 30)
    llm = FakeLlm(chat=[USABLE] * 30)

    assert garble.is_pdf_garbled(path, sample_count=3, llm=llm) is False
    assert len(llm.calls_of("chat")) == 3
