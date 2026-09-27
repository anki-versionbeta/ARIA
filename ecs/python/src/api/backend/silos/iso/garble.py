"""Is this PDF's text layer usable, or font-ciphered garbage?

Ported from backend.py:261-565. The verdict is the most consequential decision in the
silo: `False` reads the text layer for nothing, `True` sends every page through vision
extraction and, later, one Textract page-analysis per page. Getting it wrong is either a
report full of ciphered nonsense or a large bill.

Three independent signals, OR'd — any one routes to vision:

  A  pypdf per-page heuristic. pypdf faithfully exposes broken font encodings as garbled
     strings, so this catches wholly garbled or encrypted documents.
  B  fitz substitution-cipher ratio. Some documents are only *partially* ciphered —
     headings and tables extract cleanly while the body is enciphered — and the clean
     text dilutes A's per-page ratios enough to pass. B measures the cipher-symbol
     fraction on the text fitz actually returns, and uses the document-level mean rather
     than a page majority, because a page vote misses documents where only a minority of
     pages are affected.
  C  an LLM readability backstop, for ciphers the fixed heuristics do not know about.

The thresholds are calibrated against real documents and are quoted in the comments
below; they are not adjustable knobs. Signal C never raises and falls back to "clean",
so Iliad being slow or down cannot block an upload.

Seam changes only: the direct `requests` call becomes the injected client (with
`temperature=0` preserved, which matters because this verdict must be deterministic for
a given document), and the `print` trail becomes `logger.info` with identical message
text, because a worker's stdout is not captured and that trail is what a validation
review reads.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import fitz
import pypdf

from .workspace import ProgressFn, no_progress

logger = logging.getLogger(__name__)

GARBLE_THRESHOLD = 0.4
GARBLE_PAT = re.compile(r'[A-Z][>:<^@=\\]{1,2}[A-Z]')
# Substitution-cipher garble (Signal B). Some PDFs are only PARTIALLY garbled:
# headings / running headers / tables extract cleanly while body paragraphs are
# font-ciphered into tokens like "H?=>JI" ("rights") or "<KD9J?ED" ("function")
# — letters mapped to a mix of letters and the symbols < > = ? ^ ~ | \.  These
# symbols essentially never touch a letter in real ISO/English text (URLs and
# math/table cells are space-separated or filtered), whereas ciphered tokens
# are full of them.  _CIPHER_ADJ matches such a symbol ADJACENT to a letter
# (either side).  ':' and ';' are deliberately EXCLUDED — they are normal
# punctuation ("Note:", "following;") and would cause false positives on clean
# docs.  Measured doc-level mean: garbled docs run 0.14–0.37, clean docs ≤ 0.002
# (>25x margin), so a 0.05 mean threshold separates them robustly.  _URLISH_RE
# drops URL/email/watermark tokens so legitimate links don't score as cipher.
_CIPHER_ADJ = re.compile(r'[A-Za-z][<>=?^~|\\]|[<>=?^~|\\][A-Za-z]')
_URLISH_RE  = re.compile(r'https?://|www\.|@|\.com|\.org|\.iso\.|techstreet', re.I)
_ISO_COMMON_WORDS = frozenset({
    'the', 'of', 'and', 'to', 'in', 'a', 'is', 'that', 'for', 'are',
    'shall', 'be', 'this', 'with', 'or', 'an', 'as', 'not', 'from', 'by',
    'which', 'when', 'if', 'on', 'at', 'it', 'have', 'was', 'were', 'been',
    'has', 'all', 'its', 'each', 'may', 'used', 'other', 'than', 'they',
    'any', 'more', 'within', 'should', 'must', 'also', 'only', 'such',
    'between', 'after', 'can', 'no', 'per', 'provided', 'part', 'section',
    'clause', 'standard', 'iso', 'requirement', 'system', 'device', 'test',
    'data', 'value', 'temperature', 'pressure', 'time', 'information',
    'figure', 'table', 'annex', 'note', 'example',
})


def _is_garbled_text(text, threshold=GARBLE_THRESHOLD):
    """Return True if text looks like font-encoding garbage (3-check heuristic).

    Check 1 — non-printable character ratio (classic full-encryption symptom).
    Check 2 — word-level plausibility:
        2a. Fewer than 30 % of tokens are purely alphabetic — garbled font tokens
            mix letters with punctuation chars like > : < ^ @ = \
        2b. Fewer than 3 % of alpha tokens are recognisable ISO/English words —
            font-encoding substitution produces unrecognisable letter sequences.
    Check 3 — font-encoding garble pattern (uppercase + embedded punctuation).
    """
    if not text or len(text.strip()) < 20:
        return True

    # Check 1: non-printable character ratio
    printable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
    if (1.0 - printable / len(text)) > threshold:
        logger.info("  [garble-check1] non-printable ratio → garbled")
        return True

    # Check 2: word-level plausibility
    tokens = REDACTED
        re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", w).lower()
        for w in text.split()
    ]
    tokens = REDACTED
    if not tokens:
        return True

    # 2a: ratio of purely alphabetic tokens (length > 1)
    alpha_tokens = REDACTED
    if len(alpha_tokens) / len(tokens) < 0.30:
        logger.info(
            "  [garble-check2a] alpha-token ratio=%.3f < 0.30 → garbled",
            len(alpha_tokens) / len(tokens),
        )
        return True

    # 2b: vocabulary check — at least 3 % of alpha tokens must be recognisable words
    if len(alpha_tokens) > 10:
        known = sum(1 for t in alpha_tokens if t in _ISO_COMMON_WORDS)
        if (known / len(alpha_tokens)) < 0.03:
            logger.info(
                "  [garble-check2b] known-word ratio=%.3f < 0.03 → garbled",
                known / len(alpha_tokens),
            )
            return True

    # Check 3: font-encoding garble pattern ratio > 8 %
    raw_tokens = REDACTED
    if raw_tokens:
        garble_hits = sum(1 for w in raw_tokens if GARBLE_PAT.search(w))
        if (garble_hits / len(raw_tokens)) > 0.08:
            logger.info(
                "  [garble-check3] garble pattern ratio=%.3f > 0.08 → garbled",
                garble_hits / len(raw_tokens),
            )
            return True

    return False


def _fitz_cipher_ratio(text):
    """Fraction of tokens that look like substitution-cipher garble.

    Counts tokens containing a cipher symbol (< > = ? ^ ~ | \\) ADJACENT to a
    letter — e.g. 'H?=>JI' = 'rights', '<KD9J?ED' = 'function'.  ':'/';' are
    excluded (normal punctuation).  URL / email / watermark tokens are excluded
    so genuine links don't score.  Returns 0.0 when too few tokens to judge.
    """
    raw = [w for w in (text or '').split() if not _URLISH_RE.search(w)]
    if len(raw) <= 10:
        return 0.0
    return sum(1 for w in raw if _CIPHER_ADJ.search(w)) / len(raw)


# ── Signal C: LLM text-quality classifier ───────────────────────────────────
# Generic backstop for garble the heuristics (A/B) miss: instead of pattern-
# tweaking per document, ask an LLM whether the EXTRACTED PAGE TEXT is real
# words or font-encoding garbage.  It only needs to judge readability, not
# decode anything — validated to flag ciphered pages while leaving clean
# tables / figures / front-matter / any natural language alone.
_ILIAD_TEXT_MODEL = "gpt-5.2"
_GARBLE_LLM_PROMPT = (
    "You are a text-quality classifier for a PDF text-extraction pipeline.\n"
    "Decide whether the extracted page text below is USABLE or GARBLED.\n"
    "- USABLE: readable, well-formed text in ANY natural language; this includes "
    "messy tables, numbers, headers/footers, equations, dot leaders, or layout "
    "noise — as long as the words are real words.\n"
    "- GARBLED: a substantial portion of the words are NOT real words — e.g. "
    "font-encoding corruption / substitution cipher / mojibake where letters are "
    "replaced by symbols or wrong characters (e.g. 'H?=>JI' for 'rights').\n"
    "Respond with STRICT JSON only, no markdown: "
    '{"verdict":"USABLE"|"GARBLED","confidence":0.0-1.0}\n\n'
    "PAGE TEXT:\n<<<\n{TEXT}\n>>>"
)


def _llm_classify_page_garbled(text, llm):
    """Ask the LLM if one page's extracted text is GARBLED.

    Returns (is_garbled: bool, confidence: float) or None on any failure.
    Pure text call (no image) — cheap and fast.  Never raises.

    `temperature=0` is not decoration: this verdict routes the whole document, so it
    must be the same verdict every time for the same page.
    """
    if not text or len(text.strip()) < 20:
        return None
    try:
        content = llm.chat(
            user=_GARBLE_LLM_PROMPT.replace("{TEXT}", text[:3000]),
            model=_ILIAD_TEXT_MODEL,
            max_tokens=REDACTED
            temperature=0,
            # One 45s attempt, as in the source. A one-element tuple adds no retry that
            # ISO did not have.
            timeouts=(45,),
        ).strip()
        content = re.sub(r'^```[a-z]*\n?|\n?```$', '', content).strip()
        obj = json.loads(content)
        verdict = str(obj.get("verdict", "")).upper()
        conf = float(obj.get("confidence", 0) or 0)
        return (verdict == "GARBLED", conf)
    except Exception as exc:
        logger.info("[garble]   LLM page classify failed: %s", exc)
        return None


def _llm_garble_check(pdf_path, indices, llm):
    """Signal C: classify the FITZ text of the sampled pages via LLM, in parallel.

    Returns True if the document should be treated as garbled.  Uses fitz text
    (what the normal path actually extracts).  Fail-safe: any page that errors
    is ignored; if every page errors / no pages judged, returns False so the
    pipeline falls back to the heuristic verdict (normal path).
    """
    # Collect fitz text per sampled page
    page_texts = []
    try:
        with fitz.open(pdf_path) as fdoc:
            for idx in indices:
                if idx >= len(fdoc):
                    continue
                t = fdoc[idx].get_text("text") or ""
                if len(t.strip()) >= 20:
                    page_texts.append((idx, t))
    except Exception as exc:
        logger.info("[garble] signal-C: fitz read failed: %s", exc)
        return False
    if not page_texts:
        return False

    results = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {
                ex.submit(_llm_classify_page_garbled, t, llm): idx
                for idx, t in page_texts
            }
            for fut in futs:
                idx = futs[fut]
                try:
                    results[idx] = fut.result(timeout=60)
                except Exception:
                    results[idx] = None
    except Exception as exc:
        logger.info("[garble] signal-C: LLM pool failed: %s", exc)
        return False

    judged = [v for v in results.values() if v is not None]
    if not judged:
        logger.info(
            "[garble] signal-C: no pages judged (LLM unavailable) → fall back to clean"
        )
        return False

    # High-confidence garbled pages.  Threshold tuned with the heuristic margins:
    # clean pages classify USABLE with conf ~1.0; ciphered pages GARBLED ~0.95.
    garbled = sum(1 for (is_g, conf) in judged if is_g and conf >= 0.6)
    frac = garbled / len(judged)
    decision = garbled >= 2 or frac >= 0.34
    logger.info(
        "[garble] signal-C LLM: garbled_pages=%d/%d (frac=%.2f) → %s",
        garbled,
        len(judged),
        frac,
        'garbled' if decision else 'clean',
    )
    return decision


def is_pdf_garbled(
    pdf_path,
    sample_count=6,
    *,
    llm,
    progress: ProgressFn = no_progress,
):
    """Return True if the PDF appears to contain garbled / encrypted text.

    Two independent signals (OR'd — either one routes to the vision path):

    (A) pypdf heuristic — pypdf faithfully exposes broken font-encoding as
        garbled strings.  Catches fully-garbled / encrypted PDFs.

    (B) fitz substitution-cipher check — some PDFs are only PARTIALLY garbled:
        headings/tables extract cleanly but the body is font-ciphered (e.g. ISO
        11608-5:2022, 'rights'→'H?=>JI'; ISO 7864 printed).  The clean text
        dilutes the pypdf per-page ratios enough to pass (A), so (A) misses it.
        Signal (B) measures, on the text *fitz* actually returns (the extractor
        the normal path uses), the fraction of tokens with a cipher symbol
        (< > = ? ^ ~ | \\) touching a letter.  Doc-level MEAN across sampled
        pages is used (not a page-majority) because partially-garbled docs only
        cipher a minority of pages — a page vote misses them.  Calibrated: clean
        docs ≤ 0.002, garbled docs 0.14–0.37, so mean > 0.05 (or any single page
        > 0.20) routes to vision with a wide safety margin.

    Skips page 0 (cover/title) which often has minimal text.
    """
    logger.info("[garble] checking garble on: %s", pdf_path)
    reader = pypdf.PdfReader(pdf_path)
    total = len(reader.pages)
    logger.info("[garble] total pages: %d", total)
    if total == 0:
        return True

    # Spread sample indices across the document, skipping cover page (idx 0)
    step    = max(1, (total - 1) // sample_count)
    indices = list(range(1, total, step))[:sample_count]
    if not indices:
        indices = [0]

    logger.info("[garble] sampling pages: %s", indices)
    progress(f"Checking the text layer on {len(indices)} page(s)")

    # ── Signal A: pypdf per-page heuristic ──────────────────────────────────
    garbled_count = 0
    checked_count = 0
    for idx in indices:
        try:
            text = reader.pages[idx].extract_text() or ""
            if len(text.strip()) < 10:
                continue   # skip near-blank / image-only pages
            checked_count += 1
            is_g = _is_garbled_text(text)
            logger.info(
                "[garble]   page %d: chars=%d, garbled=%s  preview=%r",
                idx,
                len(text),
                is_g,
                text[:80],
            )
            if is_g:
                garbled_count += 1
        except Exception as exc:
            logger.info("[garble]   page %d extraction error: %s", idx, exc)
            continue

    if checked_count == 0:
        # Could not extract any text — treat as garbled
        logger.info("[garble] no pages had extractable text → treating as garbled")
        return True

    if (garbled_count / checked_count) >= 0.5:
        logger.info(
            "[garble] signal-A pypdf: garbled_pages=%d/%d → garbled",
            garbled_count,
            checked_count,
        )
        return True

    # ── Signal B: fitz substitution-cipher check (doc-level mean) ───────────
    try:
        ratios = []
        with fitz.open(pdf_path) as fdoc:
            for idx in indices:
                if idx >= len(fdoc):
                    continue
                ftext = fdoc[idx].get_text("text") or ""
                if len(ftext.strip()) < 10:
                    continue
                ratios.append(_fitz_cipher_ratio(ftext))
        if ratios:
            mean_ratio = sum(ratios) / len(ratios)
            max_ratio  = max(ratios)
            if mean_ratio > 0.05 or max_ratio > 0.20:
                logger.info(
                    "[garble] signal-B fitz cipher: mean=%.3f max=%.3f → garbled",
                    mean_ratio,
                    max_ratio,
                )
                return True
            logger.info(
                "[garble] signal-B fitz cipher: mean=%.3f max=%.3f → clean",
                mean_ratio,
                max_ratio,
            )
    except Exception as exc:
        logger.info("[garble] signal-B fitz cipher check failed: %s", exc)

    # ── Signal C: LLM text-quality backstop (only reached when A & B say clean)
    # Generic catch for novel garble/ciphers the fixed heuristics miss.  Never
    # raises and falls back to "clean" (normal path) if the LLM is unavailable,
    # so it can't break uploads when Iliad is down/slow.
    try:
        progress("Checking text quality with the model")
        if _llm_garble_check(pdf_path, indices, llm):
            logger.info("[garble] signal-C LLM → garbled")
            return True
    except Exception as exc:
        logger.info("[garble] signal-C LLM check failed (fallback clean): %s", exc)

    logger.info(
        "[garble] all signals clean (pypdf %d/%d) → not garbled",
        garbled_count,
        checked_count,
    )
    return False
