"""Reading a page with the model instead of the text layer.

Ported from backend.py:572-760. Used when `garble.is_pdf_garbled` says the text layer
cannot be trusted: every page is rendered to a PNG and the model returns the page's text
plus the bounding boxes of its figures and tables.

Both prompts live here, as they do in the source. `VISION_TOC_PROMPT` is used by `toc.py`
rather than by this module, but it is a vision prompt and it shares the model constant,
so `toc.py` imports it from here rather than keeping a second copy.

Seam changes only: the direct `requests` call becomes the injected client, and the loop
reports progress. Everything about the prompt, the 150 dpi render, the 4096-token budget,
the fence stripping, the eight workers and both empty-page fallbacks is unchanged.

Page order is load-bearing — the golden-file comparison depends on it — so the results
list is preallocated and `ex.map` is zipped over `range(total_pages)`, exactly as the
source does. Do not switch to `as_completed`.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import fitz

from .workspace import ProgressFn, no_progress

logger = logging.getLogger(__name__)

_ILIAD_VISION_MODEL = "gpt-5.2"

VISION_EXTRACT_PROMPT = (
    "Extract ALL content from this PDF page exactly as it appears. "
    "Return ONLY a valid JSON object — no markdown fences, no explanation.\n\n"
    "Required JSON format:\n"
    "{\n"
    '  "text": "<full verbatim text of the page, preserving newlines and structure>",\n'
    '  "images": [{"caption": "<Figure X — description>", "bbox_norm": [x0, y0, x1, y1]}],\n'
    '  "tables": [{"caption": "<Table X.Y — description>", "bbox_norm": [x0, y0, x1, y1], '
    '"data": [["header1", "header2"], ["val1", "val2"]]}]\n'
    "}\n\n"
    "Rules:\n"
    "1. text must be verbatim — no summarization, paraphrasing, or generated content.\n"
    "2. Preserve section numbering (1, 1.1, 1.1.1), bullet points, and lettered lists a) b) c).\n"
    "3. Preserve table-of-contents dot leaders exactly (e.g. '3.1 Scope .............. 12').\n"
    "4. bbox_norm values are [x0, y0, x1, y1] normalized 0.0-1.0 relative to page width/height (origin top-left).\n"
    "5. Only include entries in 'images' for actual diagrams/figures with an explicit Figure or Fig caption.\n"
    "6. Only include entries in 'tables' for actual tabular data with an explicit Table caption.\n"
    "7. For each table, 'data' must contain ALL rows and ALL columns as strings. Use empty string for empty/merged cells.\n"
    "8. Exclude any lines starting with 'Copyrighted material licensed to'.\n"
    "9. Exclude any lines containing the © symbol.\n"
    "10. Exclude the line 'No further reproduction or distribution is permitted.'\n"
    "11. EXCLUDE the inner contents of any table from 'text'. The table is already captured "
    "structurally in 'tables[*].data', so it must NOT be repeated inside the 'text' field. "
    "Keep only the table's caption line (e.g. 'Table B.1 — One-sided tolerance limit factors') "
    "in 'text'; drop column headers, row labels, and every cell value. This applies to ALL "
    "tables on the page, including reference/data tables that span multiple pages.\n"
    "11a. The exclusion in rule 11 covers EVERYTHING drawn inside the table's outer border, "
    "not only the data cells. In ISO/IEC standards a table's footnote rows sit BELOW the last "
    "data row but still INSIDE that border — lines such as 'a  This test applies only to output "
    "lines…', 'b  All cables are attached during the test.', 'NOTE 1  …', or an unlettered row "
    "like 'All AC values shall be applied to currents having a frequency not less than 0,1 Hz'. "
    "Those rows belong to the table and MUST NOT appear in 'text'. Use the drawn border as the "
    "test: if a line is inside the rectangle that encloses the table, drop it; if it is outside "
    "that rectangle, keep it. A lettered paragraph BELOW the border, such as a clause sub-point "
    "'b) NIS-E with a patient connection of a type F applied part…', is body text and MUST be "
    "kept in 'text'.\n"
    "12. EXCLUDE any text drawn INSIDE a figure or diagram (flowchart boxes, labels, callouts, "
    "legends/keys, axis titles, dimension markings, and any wording that is part of the "
    "illustration) from the 'text' field. Each figure is captured separately as an image, so its "
    "internal text must NOT be repeated in 'text'. Keep ONLY the figure's caption line "
    "(e.g. 'Figure 1 — ISO 11608 series road map') in 'text'. This exclusion applies solely to "
    "wording within the figure itself — ordinary body paragraphs, headings, and clause-level "
    "notes or footnotes that belong to the SECTION rather than to a figure or table MUST still "
    "be kept in 'text'. This sentence does not re-admit anything excluded by rules 11 and 11a: "
    "a footnote row inside a table's border is part of that table and stays out of 'text' even "
    "though it is a footnote."
)

VISION_TOC_PROMPT = (
    "You are analyzing the first pages of an ISO/IEC standard document. "
    "Your task is to identify the complete Table of Contents (section structure) of this document.\n\n"
    "Examine ALL visible pages and extract every section heading, annex, and major subdivision.\n\n"
    "Return ONLY a valid JSON array — no markdown fences, no explanation.\n\n"
    "Required JSON format:\n"
    "[\n"
    '  {"section_number": "1", "title": "Scope", "page_number": 1},\n'
    '  {"section_number": "1.1", "title": "General", "page_number": 1},\n'
    '  {"section_number": "Annex A", "title": "Test methods (informative)", "page_number": 25}\n'
    "]\n\n"
    "Rules:\n"
    "1. Include ALL numbered sections (1, 1.1, 1.1.1, etc.) and all Annexes (Annex A, Annex B, etc.).\n"
    "2. section_number must be the bare number ('4.1.2') or 'Annex X' — no trailing punctuation.\n"
    "3. title is the heading text WITHOUT the section number prefix.\n"
    "4. page_number is the printed page number shown in the document, as an integer.\n"
    "5. If a printed Table of Contents page is visible, use it as the primary source.\n"
    "6. If no TOC page is visible, infer the structure from section headings visible in the body text.\n"
    "7. Include standard front-matter sections: Foreword, Introduction (if present) with page_number.\n"
    "8. Do NOT include individual figures, tables, or bibliography entries.\n"
    "9. Ensure entries are in document order.\n"
    "10. If you cannot determine a page number, use 0."
)


def _render_page_to_base64(fitz_doc, page_idx, dpi=150):
    """Render a page to a base64-encoded PNG.

    Returns (b64_string, page_width_pts, page_height_pts).
    Uses fitz.Matrix for the scale transform (portable across all PyMuPDF versions).
    """
    page     = fitz_doc.load_page(page_idx)
    mat      = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix      = page.get_pixmap(matrix=mat, alpha=False)
    b64      = base64.b64encode(pix.tobytes("png")).decode("utf-8")
    return b64, page.rect.width, page.rect.height


def _call_llm_vision(b64_png, prompt, llm):
    """One page image plus a prompt, returning the model's raw reply.

    `max_tokens=4096` ensures full-page content is not truncated. The single 120s
    attempt is the source's; a one-element `timeouts` tuple adds no retry ISO lacked.
    """
    return llm.chat_vision(
        image_b64=b64_png,
        prompt=prompt,
        model=_ILIAD_VISION_MODEL,
        max_tokens=REDACTED
        timeouts=(120,),
    )


def _extract_page_via_llm(pdf_path, page_idx, llm, dpi=150):
    """Render one page, call the vision model, parse the JSON result.

    Opens its own fitz.Document for thread-safety (each worker gets its own handle).
    Returns dict: {page_idx, text, images, tables, page_w_pts, page_h_pts, b64}.
    """
    doc = fitz.open(pdf_path)
    try:
        b64, pw, ph = _render_page_to_base64(doc, page_idx, dpi=dpi)
    finally:
        doc.close()

    raw = ""
    try:
        raw = _call_llm_vision(b64, VISION_EXTRACT_PROMPT, llm).strip()
        # Strip possible markdown code-fence wrappers
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.rstrip())
        result = json.loads(raw)
        logger.info(
            "[vision-page] page %d JSON parsed OK: text_len=%d, images=%d, tables=%d",
            page_idx,
            len(result.get('text', '')),
            len(result.get('images', [])),
            len(result.get('tables', [])),
        )
    except Exception as exc:
        # A page the model could not return usable JSON for becomes an empty page rather
        # than a failed document: losing one page of two hundred beats losing all of it.
        logger.info(
            "[vision-page] page %d JSON parse FAILED: %s  raw=%r",
            page_idx,
            exc,
            raw[:200],
        )
        result = {"text": "", "images": [], "tables": []}

    return {
        "page_idx":   page_idx,
        "text":       result.get("text", ""),
        "images":     result.get("images", []),
        "tables":     result.get("tables", []),
        "page_w_pts": pw,
        "page_h_pts": ph,
        "b64":        b64,
    }


def extract_all_pages(
    pdf_path,
    total_pages,
    *,
    llm,
    progress: ProgressFn = no_progress,
    max_workers=8,
):
    """Extract all pages in parallel using a ThreadPoolExecutor.

    Each worker opens its own fitz.Document (thread-safe).
    Returns an ordered list of page dicts (index == page_idx).

    Progress is reported from the main thread as `ex.map` yields, never from a worker:
    the callback writes to the run's database session, and sharing that across threads
    is not safe. `ex.map` yields in submission order, so this both preserves page order
    and gives a heartbeat — without which a 200-page document would sit silent for
    longer than the reaper's 900-second patience and be re-queued mid-flight.
    """
    logger.info(
        "[vision-all] starting parallel LLM extraction: %d pages, max_workers=%d",
        total_pages,
        max_workers,
    )

    results = [None] * total_pages

    def _worker(idx):
        try:
            return _extract_page_via_llm(pdf_path, idx, llm)
        except Exception as exc:
            logger.info("[vision-all] page %d failed: %s", idx, exc)
            return {
                "page_idx":   idx,
                "text":       "",
                "images":     [],
                "tables":     [],
                # A4 at 72dpi. A wrong page size would skew every normalised bbox on
                # this page, but a missing one would break the downstream maths outright.
                "page_w_pts": 595.0,
                "page_h_pts": 842.0,
                "b64":        "",
            }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for idx, page_data in zip(range(total_pages), ex.map(_worker, range(total_pages))):
            results[idx] = page_data
            progress(f"Read page {idx + 1} of {total_pages}")

    # ── Retry pages that came back EMPTY but actually have content ─────────
    # A transient LLM error / unparseable response leaves a page with text="".
    # On a garbled PDF that page then renders as CIPHERED fitz text downstream
    # (the "encrypted fields" symptom), because it is excluded from llm_texts
    # and the body extractor falls back to the raw font-ciphered fitz layer.
    # Distinguish a genuinely blank page (little/no raw content) from a failed
    # extraction (the page HAS raw fitz text / images / vector drawings) and
    # retry only the latter, so transient failures do not leak ciphered text.
    # Bounded to 2 rounds; retries run on the main thread (progress-safe).
    try:
        _rdoc = fitz.open(pdf_path)
        def _page_has_content(idx):
            try:
                _pg = _rdoc[idx]
                return (
                    len((_pg.get_text("text") or "").strip()) > 50
                    or bool(_pg.get_images())
                    or len(_pg.get_drawings()) > 3
                )
            except Exception:
                return True
        for _attempt in range(2):
            _empty = [
                i for i in range(total_pages)
                if not (results[i] or {}).get("text", "").strip()
                and _page_has_content(i)
            ]
            if not _empty:
                break
            logger.info(
                "[vision-all] retry %d: re-extracting %d empty-but-nonblank pages %s",
                _attempt + 1, len(_empty), _empty,
            )
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for idx, page_data in zip(_empty, ex.map(_worker, _empty)):
                    if (page_data or {}).get("text", "").strip():
                        results[idx] = page_data
                    progress(f"Re-read page {idx + 1} of {total_pages}")
        _rdoc.close()
    except Exception as exc:
        logger.info("[vision-all] empty-page retry skipped: %s", exc)

    non_empty = sum(1 for pd in results if pd and pd.get("text", "").strip())
    logger.info(
        "[vision-all] done: %d pages extracted, %d have non-empty text",
        len(results),
        non_empty,
    )
    return results
