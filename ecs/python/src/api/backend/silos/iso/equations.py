"""Finding equations that carry no number, and placing them. Ported from
backend.py:2665-3306.

Numbered formulas — the ones ending in "(4.2)" — are picked up by the caption logic in
`prescan_media`. This module handles the rest, which is harder: an unnumbered equation
has no caption to search for, so it has to be recognised by how it is typeset and then
anchored to a piece of preceding text so the emitter knows where to put it.

The pipeline, and why each step exists:

  1. `_find_formula_candidates` — two detectors, unioned. The per-line one catches
     ordinary equations; `_detect_equations_banded` exists because some ISO PDFs lay out
     every variable and operator as its own positioned text run, so one visual equation
     arrives as a dozen separate "lines", each with too few math glyphs to trip the
     per-line test.
  2. `_filter_formula_candidates` — drops anything overlapping a detected table or
     figure, since a number inside a table cell is not an equation.
  3. `_triage_parallel` → the vision model, which gets a **tight crop and a wider
     context crop in one request** and is told to use the wider one to decide whether
     the tight one really is an equation, and the tight one to read the LaTeX. That is
     why this cannot become one call per image.
  4. `_find_preceding_anchor` — the nearest substantive text above it, which the emitter
     matches to decide the insertion point.
  5. `_section_idx_for_page` — which TOC section it belongs to, disambiguated by y
     position when several sections start on the same page.

Seam changes only: the direct `requests` call becomes `llm.chat_vision_multi`, the AWS
and filesystem plumbing arrives as `ws`, and the `print` trail becomes `logger.info` with
identical text. Worker count stays at 4 — distinct from the 8 used elsewhere — and
`ex.map` is kept so the accepted list keeps candidate order.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import fitz

from .geometry import _extract_page_regions, _intersect_area, _rect_area
from .mediastore import _session_media_dir, _write_media
from .patterns import _PAGE_NUM_RE, _WATERMARK_RE
from .sections import _section_end_page_0, _section_num
from .vision import _ILIAD_VISION_MODEL

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# FORMULA DETECTION & TRIAGE (unnumbered equations, anchor-based placement)
# ─────────────────────────────────────────────────────────────────────────────
# Pipeline:
#   1. _find_formula_candidates          → (page, bbox, crop_png, context_png)
#   2. _filter_formula_candidates        → drop table/figure overlaps
#   3. _triage_formula_via_llm (parallel)→ {is_equation, latex, eqn_number, ...}
#   4. _find_preceding_anchor            → text anchor for DOCX insertion
#   5. _section_idx_for_page             → TOC section index via page range
# The final list feeds into _extract_body, which inserts the cropped PNG
# (LaTeX stored as alt text on the image) at the paragraph matching the anchor.

# ── Detection constants (font/symbol heuristics) ─────────────────────────────
# Known math-font substrings (case-insensitive).
_MATH_FONT_RE = re.compile(
    r"(cmmi|cmsy|cmex|msam|msbm|"
    r"cambria\s*math|stix|xits|asana\s*math|"
    r"latin\s*modern\s*math|minion\s*math|lucida\s*math|"
    r"euler|neo\s*euler|mathjax|symbol|mathematical)",
    re.I,
)

# Math / Greek symbols that rarely appear in prose.
_MATH_GLYPHS = set(
    "∑∏∫∬∭∮∯∇∂√∞≤≥≠≈≡≅∝∈∉⊂⊃⊆⊇∪∩∅±∓×÷·∘⊕⊗⊥∥∠→←↔⇒⇐⇔"
    "αβγδεζηθικλμνξοπρστυφχψωϵϕϑϖϱ"
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
    "ℝℂℕℤℚℙℍℕ"
)

# Equation number on its own, e.g. "(1)", "(2.3)", "(4-7)", "(a)"
_EQN_NUM_RE = re.compile(r"^\(\s*(?:\d+(?:[\.\-]\d+)?|[a-z])\s*\)\s*$", re.I)


def _line_math_signal(spans):
    """Return (math_ratio, symbol_count, italic_ratio, total_chars) for a line."""
    math_f, symbols, italics, total = 0, 0, 0, 0
    for sp in spans:
        font  = sp.get("font", "")
        is_mf = bool(_MATH_FONT_RE.search(font))
        is_it = bool(sp.get("flags", 0) & 2)
        for ch in sp["text"]:
            if ch.isspace():
                continue
            total += 1
            if is_mf:
                math_f += 1
            if ch in _MATH_GLYPHS:
                symbols += 1
            if is_it:
                italics += 1
    if total == 0:
        return 0.0, 0, 0.0, 0
    return math_f / total, symbols, italics / total, total


_YBAND_TOL             = 4.0   # spans within this y-distance belong to the same band
_YBAND_MIN_MATH_GLYPHS = 2     # signal B threshold
_YBAND_MIN_FONTS       = 2     # signal C threshold (distinct fonts in band)
_YBAND_MAX_ALPHA       = 40    # reject bands with more alpha chars (prose)
_YBAND_MAX_WIDTH_FRAC  = 0.8   # reject bands spanning >80% of page width


def _detect_equations_banded(page):
    """Catch equations rendered as positioned glyph clouds (ISO/IEC variable-width PDFs).

    Some ISO PDFs lay each variable and operator out as its own positioned
    text run — so `page.get_text("dict")` puts them in separate "lines" even
    though visually they form one equation.  The per-line detector misses
    these because each fragment has ≤1 math glyph.

    This pass collects every non-empty span, groups spans by visual y-band
    (center-to-center within _YBAND_TOL), and applies the math signal to the
    aggregated band.  Bands that look like prose (alpha > 40 chars, or width
    > 80% of page) are rejected to avoid false positives.
    """
    spans = []
    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                txt = sp["text"]
                if not txt.strip():
                    continue
                bbox = sp["bbox"]
                yc   = 0.5 * (bbox[1] + bbox[3])
                sym   = sum(1 for ch in txt if ch in _MATH_GLYPHS)
                alpha = sum(1 for ch in txt if ch.isalpha())
                total = sum(1 for ch in txt if not ch.isspace())
                is_mf = bool(_MATH_FONT_RE.search(sp.get("font", "")))
                spans.append({
                    "bbox": bbox, "yc": yc, "font": sp.get("font", ""),
                    "sym": sym, "alpha": alpha, "total": total, "is_mf": is_mf,
                })

    if not spans:
        return []

    spans.sort(key=lambda s: (s["yc"], s["bbox"][0]))

    # Group spans into y-bands.
    bands = [[spans[0]]]
    for s in spans[1:]:
        if s["yc"] - bands[-1][-1]["yc"] <= _YBAND_TOL:
            bands[-1].append(s)
        else:
            bands.append([s])

    page_w = page.rect.width
    rects  = []
    for band in bands:
        sym_cnt   = sum(s["sym"]   for s in band)
        alpha_cnt = sum(s["alpha"] for s in band)
        total     = sum(s["total"] for s in band)
        if sym_cnt == 0 or total == 0:
            continue

        fonts    = {s["font"] for s in band}
        mf_ratio = sum(s["total"] for s in band if s["is_mf"]) / total

        xs = [s["bbox"][0] for s in band] + [s["bbox"][2] for s in band]
        ys = [s["bbox"][1] for s in band] + [s["bbox"][3] for s in band]
        width  = max(xs) - min(xs)
        height = max(ys) - min(ys)

        # Prose guards.
        if alpha_cnt > _YBAND_MAX_ALPHA:
            continue
        if width > _YBAND_MAX_WIDTH_FRAC * page_w:
            continue
        if width < 12 or height < 6:
            continue

        hit = (
            mf_ratio > 0.30
            or sym_cnt >= _YBAND_MIN_MATH_GLYPHS
            or (sym_cnt >= 1 and len(fonts) >= _YBAND_MIN_FONTS)
        )
        if hit:
            rects.append(fitz.Rect(min(xs), min(ys), max(xs), max(ys)))

    return rects


def _detect_equations(page):
    """Return list of merged fitz.Rect bboxes for equation regions on *page*.

    Combines two detectors:
      1. Per-line signal test — three heuristics on each line's spans: math-
         font ratio (>0.30), math symbol count (>=2), italic ratio (>0.70
         with at least one symbol).  Lines whose text is an equation number
         alone — e.g. "(4.2)" — are also flagged so they can merge with an
         adjacent equation body.
      2. Y-band aggregation (_detect_equations_banded) — handles equations
         typeset as positioned glyph clouds, where each variable/operator is
         a separate fitz "line".

    The two rect sets are unioned, then merged by y-proximity.
    """
    eqn_line_rects = []

    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        for ln in blk.get("lines", []):
            spans = ln.get("spans", [])
            if not spans:
                continue
            text = "".join(sp["text"] for sp in spans).strip()
            if not text:
                continue

            is_eqnum = bool(_EQN_NUM_RE.match(text))
            math_ratio, sym_cnt, ital_ratio, _total = _line_math_signal(spans)
            is_math = (
                math_ratio > 0.30
                or sym_cnt >= 2
                or (sym_cnt >= 1 and ital_ratio > 0.70)
            )
            if is_math or is_eqnum:
                eqn_line_rects.append(fitz.Rect(ln["bbox"]))

    # Union with band-based rects.
    eqn_line_rects.extend(_detect_equations_banded(page))

    if not eqn_line_rects:
        return []

    # Merge: same-line (y-overlap), or close vertical neighbours (multi-line eqn).
    eqn_line_rects.sort(key=lambda r: (r.y0, r.x0))
    merged = [eqn_line_rects[0]]
    for r in eqn_line_rects[1:]:
        last = merged[-1]
        y_overlap = min(r.y1, last.y1) - max(r.y0, last.y0)
        min_h     = min(r.y1 - r.y0, last.y1 - last.y0)
        vgap      = r.y0 - last.y1
        if (min_h > 0 and y_overlap > 0.5 * min_h) or vgap <= 8:
            merged[-1] = last | r
        else:
            merged.append(r)

    # Drop tiny isolated specks (rare false positives).
    merged = [r for r in merged if (r.x1 - r.x0) > 12 and (r.y1 - r.y0) > 6]
    return merged


def _crop_equation(page, rect, dpi=200, pad=4):
    """Return PNG bytes for a tight crop of *rect* on *page* (for LLM LaTeX)."""
    padded = fitz.Rect(
        max(0, rect.x0 - pad),
        max(0, rect.y0 - pad),
        min(page.rect.width,  rect.x1 + pad),
        min(page.rect.height, rect.y1 + pad),
    )
    zoom = dpi / 72.0
    pix  = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=padded, alpha=False)
    return pix.tobytes("png")


def _crop_with_context(page, rect, margin=100, dpi=110):
    """Return PNG bytes for a *margin*-expanded crop (for LLM triage context)."""
    ctx = fitz.Rect(
        max(0, rect.x0 - margin),
        max(0, rect.y0 - margin),
        min(page.rect.width,  rect.x1 + margin),
        min(page.rect.height, rect.y1 + margin),
    )
    zoom = dpi / 72.0
    pix  = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=ctx, alpha=False)
    return pix.tobytes("png")


def _find_formula_candidates(fitz_doc, page_range=None):
    """Scan pages in *fitz_doc* and return candidate formula dicts.

    *page_range*: optional (start_0, end_0) inclusive 0-indexed range.
    """
    if page_range is None:
        page_iter = range(len(fitz_doc))
    else:
        s, e = page_range
        page_iter = range(max(0, s), min(len(fitz_doc), e + 1))

    out = []
    for pn in page_iter:
        page = fitz_doc[pn]
        for rect in _detect_equations(page):
            out.append({
                "page_0":      pn,
                "bbox":        [rect.x0, rect.y0, rect.x1, rect.y1],
                "crop_png":    _crop_equation(page, rect),
                "context_png": _crop_with_context(page, rect),
            })
    return out


_FORMULA_TRIAGE_PROMPT = (
    "You are given two images of the SAME region from a PDF of an ISO/IEC standard. "
    "The first is a TIGHT crop of a possible equation. The second is a WIDER crop "
    "showing surrounding context (so you can tell if the region is actually inside "
    "a table, figure/diagram, or plain prose).\n\n"
    "Return ONLY a valid JSON object — no markdown fences, no explanation.\n\n"
    "Schema:\n"
    "{\n"
    '  "is_equation": true|false,\n'
    '  "confidence":  "high"|"medium"|"low",\n'
    '  "latex":       "<LaTeX source, omit $ delimiters>",\n'
    '  "display_style":"display"|"inline",\n'
    '  "has_eqn_number": true|false,\n'
    '  "eqn_number":   "(4.2)" or null,\n'
    '  "reject_reason": null or one of '
    '"table_cell"|"figure_text"|"prose"|"caption"|"toc_entry"|"other"\n'
    "}\n\n"
    "Rules:\n"
    "1. Use the WIDER context crop ONLY to decide is_equation (reject if the tight "
    "crop is a table cell, label on a figure/diagram, a caption, a TOC entry with "
    "dot leaders, or plain italic prose with no operators).\n"
    "2. Extract LaTeX from the TIGHT crop only. Do NOT include an equation number "
    "(e.g. '(4.2)') inside the LaTeX — put it in eqn_number.\n"
    "3. Simple expressions like 'V = IR', 'P = F/A' ARE equations — accept them.\n"
    "4. Multi-line display equations: join with '\\\\\\\\'.\n"
    "5. If is_equation is false, set latex to \"\" and reject_reason to the best match."
)


def _call_llm_vision_multi(b64_images, prompt, llm):
    """Send several PNGs in ONE message via the shared client.

    Used for formula triage (tight crop + wider context in a single call). The prompt
    tells the model to judge with the wider crop and read LaTeX from the tighter one, so
    the two images must arrive together.
    """
    return llm.chat_vision_multi(
        images=list(b64_images),
        prompt=prompt,
        model=_ILIAD_VISION_MODEL,
        max_tokens=REDACTED
        timeouts=(90,),
    )


def _parse_triage_response(raw):
    """Parse the triage JSON, tolerating markdown fences and junk prefixes."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.rstrip())
    # Grab the first {...} block if the model added leading narration.
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _triage_formula_via_llm(candidate, llm):
    """Send crop + context PNGs to the vision LLM and return the enrichment dict.

    On any failure returns a dict with is_equation=False so the caller can drop it.
    """
    try:
        b64_tight = base64.b64encode(candidate["crop_png"]).decode("utf-8")
        b64_ctx   = base64.b64encode(candidate["context_png"]).decode("utf-8")
        raw       = _call_llm_vision_multi([b64_tight, b64_ctx], _FORMULA_TRIAGE_PROMPT, llm)
        parsed    = _parse_triage_response(raw)
        if not parsed:
            return {"is_equation": False, "reject_reason": "parse_failed"}
        return parsed
    except Exception as exc:
        logger.info("[formula-triage] page %s failed: %s", candidate.get('page_0'), exc)
        return {"is_equation": False, "reject_reason": "llm_error"}


def _filter_formula_candidates(candidates, page_regions_by_page):
    """Drop candidates overlapping detected table/figure regions on the same page.

    *page_regions_by_page*: {page_0: [region_dict, ...]} — output of
    _extract_page_regions, already computed during the figure/table pass.
    """
    kept = []
    for c in candidates:
        regs  = page_regions_by_page.get(c["page_0"], [])
        bbox  = fitz.Rect(c["bbox"])
        if _rect_area(bbox) <= 0:
            continue
        # Narrower overlap threshold than _overlaps_any's default — formulas
        # often sit adjacent to table captions and we don't want to drop those
        # that barely clip.  20% is the IoU-like cut used elsewhere (vision
        # path, prescan_media figure match).
        overlap_ratio = max(
            (_intersect_area(bbox, fitz.Rect(r["bbox"])) / _rect_area(bbox))
            for r in regs
        ) if regs else 0.0
        if overlap_ratio > 0.20:
            continue
        # Drop tiny specks that the detector's floor (>12x6) already mostly
        # catches — second guardrail against inline italic words.
        if (bbox.x1 - bbox.x0) < 20 or (bbox.y1 - bbox.y0) < 8:
            continue
        kept.append(c)
    return kept


def _triage_parallel(candidates, llm, max_workers=4):
    """Triage candidates in parallel, attaching LLM result onto each dict.

    Returns only the candidates where is_equation=True.

    `ex.map` rather than `as_completed`: the order of the returned list becomes the
    insertion order in the document, so it must not depend on which call finished first.
    """
    if not candidates:
        return []

    logger.info(
        "[formula-triage] triaging %d candidates (max_workers=%d)",
        len(candidates),
        max_workers,
    )

    results = [None] * len(candidates)

    def _worker(i):
        return _triage_formula_via_llm(candidates[i], llm)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, res in zip(range(len(candidates)),
                          ex.map(_worker, range(len(candidates)))):
            results[i] = res

    kept = []
    rejected = Counter()
    for c, r in zip(candidates, results):
        if r and r.get("is_equation"):
            c["llm"] = r
            kept.append(c)
        else:
            rejected[(r or {}).get("reject_reason") or "unknown"] += 1

    logger.info("[formula-triage] accepted=%d  rejected=%s", len(kept), dict(rejected))
    return kept


# Min anchor length (after whitespace-collapse).  Shorter preceding blocks —
# typically connector words like "and", "where", "so" — are skipped and we
# keep walking up the page for a more discriminating anchor.
_ANCHOR_MIN_LEN = 10


def _find_preceding_anchor(page, bbox, char_limit=60):
    """Return (anchor_text, anchor_y0) for the closest SUBSTANTIVE text block above *bbox*.

    Skips blocks whose collapsed text is shorter than _ANCHOR_MIN_LEN (e.g.
    "and", "where", "so that") — these are too common as substrings to be
    reliable anchors and would cause false matches in _extract_body.  When
    the immediate preceding block is short, walks further up the page to
    find a longer preceding block.

    Uses fitz `get_text("blocks", sort=True)` so results are consistent with
    what _extract_body later consumes.  Returns ("", None) when no suitable
    preceding block is found (e.g. formula at top of page).
    """
    rect = fitz.Rect(bbox)
    candidates = []   # list of (y1, collapsed_text, y0) sorted descending by y1
    for blk in page.get_text("blocks", sort=True):
        x0, y0, x1, y1, text, _bno, btype = blk
        if btype != 0:
            continue
        text = (text or "").strip()
        if not text:
            continue
        if _WATERMARK_RE.search(text[:120]):
            continue
        if _PAGE_NUM_RE.match(text):
            continue
        # Must be ABOVE the formula (allow small overlap tolerance).
        if y1 > rect.y0 + 2:
            continue
        collapsed = re.sub(r'\s+', ' ', text).strip()
        if not collapsed:
            continue
        candidates.append((y1, collapsed, y0))

    if not candidates:
        return "", None

    # Walk from closest-above down; pick first candidate with len >= min.
    candidates.sort(key=lambda t: -t[0])
    for y1, collapsed, y0 in candidates:
        if len(collapsed) >= _ANCHOR_MIN_LEN:
            anchor = collapsed[-char_limit:] if len(collapsed) > char_limit else collapsed
            return anchor, y0

    # Everything above is short-and-weak: fall back to the closest one anyway
    # (better than nothing; end-of-page flush will handle if match still fails).
    y1, collapsed, y0 = candidates[0]
    return collapsed, y0


def _section_idx_for_page(toc, page_0, toc_start_idx=0, toc_end_idx=None,
                          page=None, bbox=None):
    """Return the TOC index whose page range covers *page_0* (0-indexed).

    Walks the selected slice in order and keeps the last entry whose start
    page is ≤ the formula's page.  This picks the deepest subsection on
    tie-pages (e.g. parent 4 and child 4.1 both starting on p.12 → child).

    When multiple TOC entries start on the SAME page as the formula AND
    *page* + *bbox* are supplied, the tie is broken by y-coordinate: we
    locate each heading on the page (via section-number text search) and
    pick the heading whose y ≤ formula.y0 is closest.  This avoids the
    "last section on page wins" bug for dense ISO pages that start many
    sections in one page.

    Returns None when the formula sits before the first section in range.
    """
    if toc_end_idx is None:
        toc_end_idx = len(toc) - 1
    page_1 = page_0 + 1

    # ── Pass 1: page-based lookup (unchanged semantics when no bbox) ────────
    best = None
    for i in range(toc_start_idx, toc_end_idx + 1):
        if toc[i]['page'] <= page_1:
            best = i
        else:
            break
    if best is None:
        return None

    # ── Pass 2: y-disambiguation when multiple sections share formula's page
    if page is None or bbox is None:
        return best

    # Find every TOC entry starting on the same page as the formula.
    same_page_ids = [i for i in range(toc_start_idx, toc_end_idx + 1)
                     if toc[i]['page'] == page_1]
    if len(same_page_ids) <= 1:
        return best

    formula_y0 = bbox[1] if isinstance(bbox, (list, tuple)) else bbox.y0

    # Locate each heading's y-position on the page.  Use the section number
    # prefix (e.g. "6.2", "Annex A") as the search key — headings almost
    # always start with it, and it's more unique than the title text.
    heading_ys = {}
    for idx in same_page_ids:
        num = _section_num(toc[idx]['title'])
        if not num:
            continue
        # Anchored search for the number as a line-start token.  We look at
        # text blocks rather than raw character search so "6.2" inside body
        # text doesn't match.
        for blk in page.get_text("blocks", sort=True):
            text = (blk[4] or "").strip()
            if not text:
                continue
            # Match "<num>\b" or "<num>\s+<word>" at block start.
            if text == num or text.startswith(num + ' ') \
                    or text.startswith(num + '\n') or text.startswith(num + '\t'):
                heading_ys[idx] = blk[1]  # y0
                break

    if not heading_ys:
        return best

    # Pick the heading with largest y ≤ formula_y0 (closest preceding heading).
    candidates = [(y, idx) for idx, y in heading_ys.items() if y <= formula_y0]
    if candidates:
        return max(candidates, key=lambda t: t[0])[1]

    # All headings on this page are BELOW the formula → formula belongs to
    # the preceding section (the one that started on an earlier page).
    # Walk back from the first same-page entry.
    first_same = min(same_page_ids)
    if first_same - 1 >= toc_start_idx:
        return first_same - 1
    return best


def find_unnumbered_formulas(pdf_path, toc=None, toc_start_idx=0, toc_end_idx=None,
                             ws=None, llm=None, page_range=None):
    """Full pipeline: detect → filter → LLM triage → anchor → section.

    Returns list of enriched dicts ready for DOCX insertion:
        {
            "page_0":      int,
            "bbox":        [x0, y0, x1, y1],
            "image":       bytes|str,   # PNG bytes OR absolute file path
            "latex":       str,
            "eqn_number":  str|None,
            "display_style": str,
            "anchor_text": str,
            "anchor_y0":   float|None,
            "section_idx": int|None,
        }

    *page_range*: optional (start_0, end_0) — restricts detection to a page
    slice.  When omitted and *toc* + *toc_start_idx* + *toc_end_idx* are
    provided, the range is derived from the TOC.
    """
    doc = fitz.open(pdf_path)
    try:
        # Derive page_range from TOC if caller didn't pass one explicitly.
        if page_range is None and toc:
            end_i = toc_end_idx if toc_end_idx is not None else len(toc) - 1
            page_range = (
                max(0, toc[toc_start_idx]['page'] - 1),
                _section_end_page_0(toc, end_i, len(doc) - 1),
            )

        candidates = _find_formula_candidates(doc, page_range=page_range)
        logger.info(
            "[formula] raw candidates: %d  range=%s", len(candidates), page_range
        )

        if not candidates:
            return []

        # Pre-filter: drop candidates that overlap detected tables/figures.
        # Cache _extract_page_regions per page so we don't recompute.
        pages_needed = {c["page_0"] for c in candidates}
        regions_by_page = {}
        for pn in pages_needed:
            try:
                regions_by_page[pn] = _extract_page_regions(doc[pn])
            except Exception as exc:
                logger.info("[formula] page %s region scan failed: %s", pn, exc)
                regions_by_page[pn] = []

        filtered = _filter_formula_candidates(candidates, regions_by_page)
        logger.info("[formula] after pre-filter: %d", len(filtered))
        if not filtered:
            return []

        # LLM triage + LaTeX in parallel.
        accepted = _triage_parallel(filtered, llm)
        if not accepted:
            return []

        # Enrich each accepted candidate with anchor + section.
        enriched = []
        media_dir = _session_media_dir(ws)
        for i, c in enumerate(accepted):
            page   = doc[c["page_0"]]
            anchor, anchor_y0 = _find_preceding_anchor(page, c["bbox"])
            sec_idx = _section_idx_for_page(
                toc, c["page_0"], toc_start_idx,
                toc_end_idx if toc_end_idx is not None else (len(toc) - 1 if toc else 0),
                page=page, bbox=c["bbox"],
            ) if toc else None

            llm_result = c.get("llm", {})
            image_value = c["crop_png"]
            if media_dir:
                fname = f'Formula_anchor_{i + 1:03d}.png'
                # Verbatim: no durable copy is requested here, unlike the numbered
                # formulas in prescan_media. That is safe only because the pre-scan and
                # the document build run in ONE stage on ONE machine, so this path is
                # still readable when the emitter opens it. Splitting them across stages
                # would leave every unnumbered formula pointing at a deleted file.
                image_value = _write_media(media_dir, fname, c["crop_png"])

            enriched.append({
                "page_0":        c["page_0"],
                "bbox":          c["bbox"],
                "image":         image_value,
                "latex":         llm_result.get("latex", "") or "",
                "eqn_number":    llm_result.get("eqn_number"),
                "display_style": llm_result.get("display_style", "display"),
                "anchor_text":   anchor,
                "anchor_y0":     anchor_y0,
                "section_idx":   sec_idx,
            })

        # Drop anchor-less entries without a section — they have nowhere to go.
        enriched = [e for e in enriched
                    if e["anchor_text"] or e["section_idx"] is not None]
        logger.info("[formula] final enriched: %d", len(enriched))
        return enriched
    finally:
        doc.close()
