"""The text-vs-vision routing decision (`silos/iso/garble.py`).

This is the most consequential branch in the silo, so the tests pin both ends of the
calibrated margin the thresholds rely on.

A genuinely font-ciphered PDF cannot be built in-test — `fitz` only writes correct font
encodings — so the cipher signals are exercised against text constants derived from the
substitutions the source documents (`'rights'` -> `'H?=>JI'`,
`'function'` -> `'<KD9J?ED'`), and the whole-document verdict is covered at its two
extremes plus the real corpus in the golden-file test.
"""

from __future__ import annotations

import pytest

from da_platform.settings import REPO_ROOT
from da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

ISO_DIR = REPO_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "garble.py").is_file(), reason="the ISO silo is not present"
)

# Real ISO prose, of the kind signal A must leave alone.
CLEAN_TEXT = (
    "This International Standard specifies requirements for information supplied by "
    "the manufacturer of a medical device. The test shall be performed at a "
    "temperature of 23 degrees and the pressure recorded in Table 3. Each requirement "
    "of this document is applicable when the device is used as intended by the "
    "manufacturer, and any deviation shall be recorded in the report."
)

# The same sentence font-ciphered the way the source documents: letters mapped onto a
# mix of letters and the symbols < > = ? ^ ~ | \.
GARBLED_TEXT = (
    "H?=>JI <KD9J?ED &H;GK?H;C;DJI ^EH ?D^EHC7J?ED IKFFB?;: 8O J>; "
    "C7DK^79JKH;H E^ 7 C;:?97B :;L?9;# ->; J;IJ I>7BB 8; F;H^EHC;: 7J 7 "
    "J;CF;H7JKH; E^ (’ :;=H;;I 7D: J>; FH;IIKH; H;9EH:;: ?D -78B; ’#"
)


@pytest.fixture(scope="module")
def garble():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "garble.py", name="garble")


def write_pdf(path, *, pages: int = 4, text: str = CLEAN_TEXT):
    import fitz

    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_textbox(fitz.Rect(72, 90, 523, 700), text, fontsize=10)
    document.save(str(path))
    document.close()
    return str(path)


# ── the text heuristic (signal A) ─────────────────────────────────────────────


def test_real_prose_is_not_garbled(garble):
    assert garble._is_garbled_text(CLEAN_TEXT) is False


def test_ciphered_text_is_garbled(garble):
    assert garble._is_garbled_text(GARBLED_TEXT) is True


def test_almost_empty_text_counts_as_garbled(garble):
    """An image-only page has nothing to read, so vision is the right route."""
    assert garble._is_garbled_text("") is True
    assert garble._is_garbled_text("   ") is True
    assert garble._is_garbled_text("too short") is True


def test_numeric_and_symbol_heavy_text_is_garbled_by_the_alpha_ratio_check(garble):
    assert garble._is_garbled_text("1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 %% ++ ==") is True


def test_unrecognisable_words_are_garbled_by_the_vocabulary_check(garble):
    """Check 2b: plausible-looking letter sequences that are not words."""
    invented = " ".join(["qwrtpq", "zxcvbn", "plkjhg", "mnbvcx"] * 6)
    assert garble._is_garbled_text(invented) is True


# ── the cipher ratio (signal B) ───────────────────────────────────────────────


def test_the_cipher_ratio_separates_clean_from_ciphered_by_a_wide_margin(garble):
    """Calibrated in the source: clean documents score <= 0.002, ciphered ones
    0.14-0.37, and the threshold sits at 0.05."""
    clean = garble._fitz_cipher_ratio(CLEAN_TEXT * 3)
    ciphered = garble._fitz_cipher_ratio(GARBLED_TEXT * 3)

    assert clean <= 0.05, f"clean prose scored {clean:.3f}, which would route to vision"
    assert ciphered > 0.05, f"ciphered text scored {ciphered:.3f}, which would be missed"
    assert ciphered > clean


def test_the_cipher_ratio_ignores_urls_and_watermarks(garble):
    """A page footer full of links must not read as a cipher."""
    links = " ".join(["https://www.iso.org/standard/1234.html"] * 20)
    assert garble._fitz_cipher_ratio(links) == 0.0


def test_the_cipher_ratio_declines_to_judge_a_short_sample(garble):
    assert garble._fitz_cipher_ratio("a<b c>d e=f") == 0.0


# ── the LLM backstop (signal C) ───────────────────────────────────────────────


def test_the_classifier_pins_temperature_and_model(garble):
    """A non-zero temperature would make the routing decision vary between two runs of
    the same document, which is a validation problem rather than a nuisance."""
    llm = FakeLlm(chat=['{"verdict":"GARBLED","confidence":0.95}'])

    assert garble._llm_classify_page_garbled(CLEAN_TEXT, llm) == (True, 0.95)

    call = llm.calls_of("chat")[0]
    assert call["model"] == "gpt-5.2"
    assert call["temperature"] == 0
    assert call["max_tokens"] == 60
    # One attempt, as in the source.
    assert call["timeouts"] == (45,)


def test_the_classifier_strips_a_markdown_fence(garble):
    llm = FakeLlm(chat=['```json\n{"verdict":"USABLE","confidence":1.0}\n```'])
    assert garble._llm_classify_page_garbled(CLEAN_TEXT, llm) == (False, 1.0)


def test_the_classifier_truncates_the_page_to_three_thousand_characters(garble):
    llm = FakeLlm(chat=['{"verdict":"USABLE","confidence":1.0}'])
    garble._llm_classify_page_garbled("x" * 9000, llm)

    # Counting every "x" would also count the ones in the prompt's own wording, so
    # assert on the longest run instead.
    sent = llm.calls_of("chat")[0]["user"]
    assert "x" * 3000 in sent
    assert "x" * 3001 not in sent


@pytest.mark.parametrize(
    "reply",
    ["not json", "{}", '{"verdict":"MAYBE"}', StubLlmError("504")],
    ids=["unparseable", "empty-object", "unknown-verdict", "transport-failure"],
)
def test_the_classifier_never_raises(garble, reply):
    """Signal C must not be able to break an upload when Iliad is slow or down."""
    result = garble._llm_classify_page_garbled(CLEAN_TEXT, FakeLlm(chat=[reply]))
    assert result is None or result == (False, 0.0)


def test_signal_c_needs_two_confident_pages_or_a_third_of_them(garble, tmp_path):
    """`garbled >= 2 or frac >= 0.34` (backend.py:461). One flagged page out of six is
    deliberately not enough."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    confident = '{"verdict":"GARBLED","confidence":0.95}'
    usable = '{"verdict":"USABLE","confidence":1.0}'

    one_page = FakeLlm(chat=[confident] + [usable] * 5)
    assert garble._llm_garble_check(path, list(range(1, 7)), one_page) is False

    two_pages = FakeLlm(chat=[confident, confident] + [usable] * 4)
    assert garble._llm_garble_check(path, list(range(1, 7)), two_pages) is True


def test_signal_c_ignores_a_low_confidence_verdict(garble, tmp_path):
    """conf >= 0.6 is required; ciphered pages come back around 0.95."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    unsure = '{"verdict":"GARBLED","confidence":0.4}'
    llm = FakeLlm(chat=[unsure] * 6)
    assert garble._llm_garble_check(path, list(range(1, 7)), llm) is False


def test_signal_c_falls_back_to_clean_when_the_model_is_unavailable(garble, tmp_path):
    """No verdict must mean "use the heuristics", not "assume the worst" — otherwise an
    Iliad outage would send every document down the expensive path."""
    path = write_pdf(tmp_path / "clean.pdf", pages=7)
    llm = FakeLlm(chat=[StubLlmError("gateway down")] * 6)
    assert garble._llm_garble_check(path, list(range(1, 7)), llm) is False


# ── the whole-document verdict ────────────────────────────────────────────────


def test_a_clean_pdf_is_not_routed_to_vision(garble, tmp_path):
    path = write_pdf(tmp_path / "clean.pdf", pages=8)
    llm = FakeLlm(chat=['{"verdict":"USABLE","confidence":1.0}'] * 8)
    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_a_pdf_with_no_text_layer_is_routed_to_vision(garble, tmp_path):
    """The image-only scan case: nothing to read, so signal A returns early."""
    import fitz

    document = fitz.open()
    for _ in range(4):
        document.new_page(width=595, height=842)
    path = tmp_path / "blank.pdf"
    document.save(str(path))
    document.close()

    assert garble.is_pdf_garbled(str(path), llm=FakeLlm()) is True


def test_an_empty_pdf_is_routed_to_vision(garble, tmp_path):
    import fitz

    document = fitz.open()
    document.new_page()
    path = tmp_path / "one.pdf"
    document.save(str(path))
    document.close()

    # A single page means indices falls back to [0], which has no text.
    assert garble.is_pdf_garbled(str(path), llm=FakeLlm()) is True


def test_the_cover_page_is_skipped_when_sampling(garble, tmp_path):
    """Covers often carry almost no text, so including page 0 would bias signal A."""
    path = write_pdf(tmp_path / "clean.pdf", pages=20)
    llm = FakeLlm(chat=['{"verdict":"USABLE","confidence":1.0}'] * 20)
    assert garble.is_pdf_garbled(path, llm=llm) is False


def test_the_verdict_reports_progress(garble, tmp_path):
    """A 6-page sample plus up to six LLM calls is long enough to need a heartbeat."""
    path = write_pdf(tmp_path / "clean.pdf", pages=8)
    messages: list[str] = []
    llm = FakeLlm(chat=['{"verdict":"USABLE","confidence":1.0}'] * 8)

    garble.is_pdf_garbled(path, llm=llm, progress=messages.append)

    assert messages
