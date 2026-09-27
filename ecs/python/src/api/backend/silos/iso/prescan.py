"""Media pre-scan: figures, tables and formulas out of the PDF as PNGs.

Ported from backend.py:3328-3952. The only changes are the platform seams: the managed
folder becomes `ws`, the AWS client becomes the injected `textract` callable, the direct
`requests` call becomes the injected `llm`, and the `print` trail becomes `logger.info`
with identical message text.

RETURN ARITY — four live paths:

    toc is None,     ws is None  -> (figures, tables, formulas)      values are bytes
    toc is None,     ws given    -> (figures, tables, formulas)      values are paths
    toc is not None, ws is None  -> the 8-tuple below                values are bytes
    toc is not None, ws given    -> the 8-tuple below                values are paths

`toc=[]` counts as "not None": it yields the 8-tuple while skipping the unnumbered-formula
detector, because that is guarded on `if toc and not llm_pages`. The 5-tuple named in the
source's own docstring is stale — the real shape is 8.

THE 8-TUPLE, in order:

    figures, tables, formulas, unnumbered_formulas,
    table_rects_by_page, fig_rects_by_page, unlabeled_tables_by_page, media_first_page

STRUCTURES — this is the contract `rows.py` and `generate.py` are written against:

figures : dict[int, bytes | str]
    key   the figure number, from the caption; **or a negative number**
          (`-(len(figures) + 1)`) for a figure with no usable caption, so it is never
          silently dropped. Consumers must tolerate negative keys.
    value PNG bytes, or an absolute local path when `ws` is given. The filename is
          derived from the caption via `_sanitize_filename`.
    First writer wins: a figure number already present is skipped.

tables : dict[str, list[bytes] | list[str]]
    key   the table reference as a STRING — "1", "3.2", "E.1" — or
          f"unlabeled_{page_idx}_{n}" when there is no caption.
    value ONE ENTRY PER CAPTURED PDF PAGE, in ascending page order. Deliberately NOT
          concatenated into one tall image: vstacking and then slicing cut across table
          rows. Paths gain a "_p{n}" suffix when a table spans more than one page.

formulas : dict[int, bytes | str]
    key   the number in the trailing "(N)" of a block that also contains a maths
          character.
    value bytes, or a path named f"Formula_{n}.png".
    ALWAYS EMPTY on the vision path — the numbered-formula scan lives in the `else`
    branch, so a garbled document yields none.

unnumbered_formulas : list[dict]
    From `find_unnumbered_formulas`; see its docstring for the nine keys. Empty on the
    vision path and whenever `toc` is falsy. Its "image" value is a LOCAL-ONLY path,
    never given a durable copy — safe only because the pre-scan and the document build
    run in one stage on one machine.

table_rects_by_page : dict[int, list[tuple[float, float, float, float]]]
    page_0 -> clip rects in PDF points, as plain floats. Consumed by `_extract_body` to
    suppress text blocks sitting inside a table, so cell text is not emitted a second
    time as body rows beside the inserted image. Recorded even when the crop itself
    fails, because suppression still has to happen.

fig_rects_by_page : dict[int, list[tuple[float, ...]]]
    The same suppression for figures — it stops key labels, axis titles and values
    embedded in a diagram leaking into the description column. Uses the FULL figure
    bbox, not the cropped one.

unlabeled_tables_by_page : dict[int, list[str]]
    page_0 -> the unlabeled table refs to flush at end of page. Normal path only.

media_first_page : dict[str, int]
    f"table:{ref}" and f"figure:{num}" -> the 0-indexed page of FIRST capture. One flat
    dict so the emitter needs a single lookup.
"""

from __future__ import annotations

import io
import logging
import re
import traceback

import fitz
from PIL import Image as PILImage

from .equations import find_unnumbered_formulas
from .geometry import _extract_page_regions
from .mediastore import _sanitize_filename, _session_media_dir, _write_media
from .patterns import (
    _ANNEX_FIG_CAP_RE,
    _FIGURE_CAPTION_RE,
    _FORMULA_TRAIL_RE,
    _MATH_CHARS_RE,
    _TABLE_CAPTION_RE,
    _WATERMARK_RE,
    _fig_ref_display,
    _parse_fig_ref,
)
from .textract import _call_textract_page, _find_caption_above, _find_caption_below

logger = logging.getLogger(__name__)


# Vision path supplements Textract figure/table detection with the LLM's own
# bbox_norm boxes.  True matches the deployed standalone server (backend.py).
_VISION_USE_LLM_BOXES = True


def _correct_landscape_png(png_bytes, page):
    """
    If the majority of text lines on `page` flow upward (dir≈(0,-1)), the page
    contains a landscape table that was printed 90° CCW on a portrait PDF page.
    Rotate the captured PNG 90° CW so it displays correctly in the DOCX.
    Returns (corrected_bytes, was_rotated).
    """
    rot = sum(1 for b in page.get_text("dict").get("blocks", [])
              for l in b.get("lines", [])
              if abs(l.get("dir", (1, 0))[0]) < 0.1 and l.get("dir", (1, 0))[1] < 0)
    nrm = sum(1 for b in page.get_text("dict").get("blocks", [])
              for l in b.get("lines", [])
              if abs(l.get("dir", (1, 0))[0]) >= 0.1)
    if rot <= nrm:
        return png_bytes, False
    img = PILImage.open(io.BytesIO(png_bytes))
    img = img.rotate(-90, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), True


def prescan_media(pdf_path, llm_pages=None, ws=None,
                  toc=None, toc_start_idx=0, toc_end_idx=None,
                  textract=None, llm=None):
    """Scan every page and collect PNG images for figures, tables, and formulas.

    When *ws* is provided, each extracted image is also written to the run's media
    directory with a caption-derived filename and stored durably, and the returned
    dicts map key → absolute file path (not raw bytes).  When *ws* is None the
    returned dicts map key → bytes (in-memory only; used by tests).

    When llm_pages is provided (vision path for garbled PDFs), uses bbox_norm
    coordinates from LLM data to render figure/table regions.  Formulas are
    left empty on the vision path (they appear as images and are captured there).

    When *toc* is provided on the NORMAL path, also runs the unnumbered-formula
    detector (frml_ext + LLM triage) over the selected TOC slice and returns
    additional elements.  See the module docstring for the exact return shape;
    the 5-tuple mentioned in the original docstring here was already stale.

    *table_rects_by_page* maps page_0 → list of (x0, y0, x1, y1) tuples in PDF
    points; consumed by _extract_body to suppress text blocks that fall inside
    a detected table region (so the table's individual cell text is not
    re-emitted as content rows alongside the inserted table image).

    For backward compatibility, callers passing no *toc* still receive the
    3-tuple (figures, tables, formulas).
    """
    doc          = fitz.open(pdf_path)
    figures      = {}    # int  → bytes
    tables_pages      = {}    # str  → [bytes, ...]
    tables_first_page = {}    # str  → int (0-indexed page of first capture)
    figures_first_page = {}   # int  → int (0-indexed page of first capture)
    formulas          = {}    # int  → bytes
    fig_captions      = {}    # int  → caption string (for filename)
    tbl_captions      = {}    # str  → caption string
    # page_0 → [(x0, y0, x1, y1), ...] — used by _extract_body to suppress
    # text blocks that fall inside a detected table region (otherwise the
    # table's individual cell text would be re-emitted as content rows
    # alongside the inserted table image).
    table_rects_by_page = {}
    # page_0 → [(x0, y0, x1, y1), ...] — same suppression for figures:
    # prevents text inside circuit diagrams / illustrations (key labels,
    # axis titles, embedded table values) from leaking as body rows.
    fig_rects_by_page = {}
    # page_0 → [tbl_ref, ...] — unlabeled tables flushed at end-of-page
    unlabeled_tables_by_page = {}
    _fig_clip_area = {}    # int fig_num -> clip area px2 of stored capture (re-capture logic)
    _tbl_cap_pages = {}    # tbl_ref -> set[page_idx]: pages captured per table (dedup guard)

    if llm_pages:
        # ── Vision path (Textract-only) ──────────────────────────────────────
        # Run Textract on every LLM-processed page to get pixel-accurate table
        # and figure bboxes. LLM text is used only for captions.
        llm_texts_local = {
            pd["page_idx"]: pd.get("text", "")
            for pd in llm_pages if pd
        }

        _textract_cache = {}
        for pd in llm_pages:
            if not pd:
                continue
            pidx = pd["page_idx"]
            if pidx not in _textract_cache:
                pix_tx = doc[pidx].get_pixmap(dpi=150)
                # ONE charged page analysis per page. A 200-page garbled document is
                # 200 analyses; the memo below only prevents charging a repeat of the
                # same page.
                _textract_cache[pidx] = _call_textract_page(
                    pix_tx.tobytes("png"), textract
                )
                logger.info("[vision-path] page %s: textract done", pidx)

        _FOOTER_PTS     = 10   # reduced from 45 — avoids clipping last table rows near page bottom
        _SUB_HEADER_PTS = 20   # small upward extension — enough for one sub-header line
        _BOTTOM_PAD     = 15   # small pad for border lines; LLM bbox extension handles under-detected rows
        _last_tbl    = {}  # tbl_ref -> {'x0n', 'x1n', 'page_idx'} for continuation stitching
        _tbl_x_union = {}  # tbl_ref -> {'x0n': min, 'x1n': max} — widest extent seen

        # Phase 1: collect all raw Textract bboxes per page before any capture.
        # Gives Phase 2 full neighbour context: next-item y0 for bottom capping,
        # and figure bounds for inline-table containment checks.
        _page_layout = {}  # page_idx -> {'tables': [(x0n,y0n,x1n,y1n),...], 'figures': [...]}
        for _pl_idx, _pl_tx in _textract_cache.items():
            _page_layout[_pl_idx] = {
                'tables':  [tuple(b) for b in _pl_tx.get('tables', [])],
                'figures': [tuple(b) for b in _pl_tx.get('figures', [])],
                'layout':  list(_pl_tx.get('layout', [])),
            }
        # Supplement _page_layout['figures'] with LLM image bboxes that carry an
        # explicit figure caption.  Textract often fails to detect figure regions
        # (it only reliably detects tables), so on pages where the LLM identified
        # a figure the Textract list may be empty.  Without these LLM bboxes,
        # Change B's containment check (below) never fires for such pages, causing
        # inline key/legend tables that sit inside the figure area to be captured
        # as separate unlabeled tables and rendered as apparent duplicates in the DOCX.
        if llm_pages and _VISION_USE_LLM_BOXES:
            for _lp in llm_pages:
                if not _lp:
                    continue
                _lp_idx = _lp.get('page_idx')
                if _lp_idx is None:
                    continue
                for _img in _lp.get('images', []):
                    _cap = (_img.get('caption') or '').strip()
                    if not (_FIGURE_CAPTION_RE.match(_cap) or _ANNEX_FIG_CAP_RE.match(_cap)):
                        continue
                    _bb = _img.get('bbox_norm')
                    if _bb and len(_bb) == 4:
                        _page_layout.setdefault(_lp_idx, {'tables': [], 'figures': [], 'layout': []})
                        _pl_entry = tuple(float(v) for v in _bb)
                        if _pl_entry not in _page_layout[_lp_idx]['figures']:
                            _page_layout[_lp_idx]['figures'].append(_pl_entry)

        for page_idx in sorted(_textract_cache.keys()):
            tx   = _textract_cache[page_idx]
            page = doc[page_idx]
            pw   = page.rect.width
            ph   = page.rect.height

            for tb_norm in tx['tables']:
                x0n, y0n, x1n, y1n = tb_norm
                # Pure Textract cell-envelope bottom, captured BEFORE the LLM
                # extension (below) may overwrite y1n.  The bbox floor uses this
                # (not the LLM-extended value) so an over-eager LLM bbox cannot
                # push the clip past the real table bottom.
                _tx_cell_y1n = y1n

                tbl_ref = _find_caption_above(llm_texts_local, page_idx, y0n, _TABLE_CAPTION_RE)
                if tbl_ref is None:
                    # Fallback: the LLM page text can miss a table caption on
                    # heavily garbled pages (this is why Table 4 — Atmosphere
                    # conditions was dropped: _find_caption_above found nothing,
                    # so the detection was skipped and Table 4 fell back to a
                    # truncated/left-clipped _extract_body render).  Textract
                    # reads the rendered image, so its layout blocks carry the
                    # caption text AND position reliably.  Match a "Table N" /
                    # "Table N.M" caption sitting just above the detected table
                    # bbox.  The anchored regex + tight proximity guard prevent
                    # body references ("see Table 4") or unrelated captions from
                    # matching.
                    for _lb in _page_layout.get(page_idx, {}).get('layout', []):
                        _lb_m = _TABLE_CAPTION_RE.match((_lb.get('text') or '').strip())
                        if not _lb_m:
                            continue
                        _lb_y1 = _lb['bbox'][3]
                        if -0.02 <= (y0n - _lb_y1) <= 0.06:
                            tbl_ref = _lb_m.group(1)
                            logger.info(f"[prescan] table {tbl_ref} page={page_idx}: "
                                  f"caption matched from Textract layout "
                                  f"(LLM text missed it)")
                            break
                if tbl_ref is None:
                    # Guard: if this TABLE detection sits on a page that has a
                    # figure caption nearby, it belongs to an embedded sub-table
                    # inside that figure (e.g. CF/BF measurement-value boxes in
                    # circuit-diagram figures).  Stitching it as a table
                    # continuation would pull figure pages into the preceding
                    # table's multi-page capture, causing figure images to appear
                    # inside the wrong table row and duplicating them later at
                    # their correct section.  Skip entirely in that case.
                    _fig_nearby = (
                        _find_caption_above(llm_texts_local, page_idx, y0n, _FIGURE_CAPTION_RE)
                        or _find_caption_below(llm_texts_local, page_idx, y1n, _FIGURE_CAPTION_RE)
                        or _find_caption_above(llm_texts_local, page_idx, y0n, _ANNEX_FIG_CAP_RE)
                        or _find_caption_below(llm_texts_local, page_idx, y1n, _ANNEX_FIG_CAP_RE)
                    )
                    if _fig_nearby is not None:
                        # Check if this table overlaps substantially with a known figure
                        # bbox on this page.  If so, the table is an inline key/legend
                        # whose content is already visible inside the figure PNG, and
                        # capturing it again as an unlabeled table would produce a
                        # duplicate-looking image in the DOCX.
                        # Two criteria for skipping (either is sufficient):
                        #   A. Table fully inside the figure bbox (strict containment)
                        #   B. Table x-range is within the figure x-range AND the table
                        #      top (y0n) starts inside the figure y-range, with at least
                        #      30 % y-overlap — catches key/legend tables whose bottom
                        #      edge extends slightly below the figure bbox.
                        # Uses both Textract figure bboxes (from _page_layout Phase 1)
                        # and LLM figure bboxes (added to _page_layout in Phase 1 extension).
                        _tbl_inside_fig = False
                        _tbl_h = max(y1n - y0n, 0.001)
                        _tbl_w = max(x1n - x0n, 0.001)
                        for _fl in _page_layout.get(page_idx, {}).get('figures', []):
                            _fl_x0, _fl_y0, _fl_x1, _fl_y1 = _fl
                            # x-overlap fraction: Textract often detects key/legend
                            # tables at full-page width, breaking strict containment.
                            _x_ovl = min(x1n, _fl_x1) - max(x0n, _fl_x0)
                            _y_overlap = min(y1n, _fl_y1) - max(y0n, _fl_y0)
                            if _x_ovl / _tbl_w >= 0.30 and _y_overlap / _tbl_h >= 0.25:
                                _tbl_inside_fig = True
                                logger.info(f"[prescan] page {page_idx}: inline table skipped "
                                      f"(inside figure bbox y={_fl_y0:.3f}-{_fl_y1:.3f})")
                                break
                        if _tbl_inside_fig:
                            continue
                        # Capture as unlabeled so inline tables between figures
                        # (e.g. CF/BF value table between Figure 7 and Figure 8)
                        # are still emitted in _extract_body rather than dropped.
                        _utbl_idx = len(unlabeled_tables_by_page.get(page_idx, []))
                        tbl_ref = f'unlabeled_{page_idx}_{_utbl_idx}'
                        unlabeled_tables_by_page.setdefault(page_idx, []).append(tbl_ref)
                        logger.info(f"[prescan] page {page_idx}: inline table near figure {_fig_nearby} "
                              f"captured as {tbl_ref}")
                    else:
                        # No caption — stitch as continuation if a matching table was seen
                        # within the last 2 pages (gap <= 2 handles one intervening text-only
                        # page between continuation pages while preventing over-merging).
                        # If neither a caption nor a nearby continuation is found, skip entirely
                        # to avoid TOC and other false-positive captures.
                        ref_w = x1n - x0n
                        best_ref = None
                        best_page = -1
                        if ref_w > 0:
                            for prev_ref, prev in _last_tbl.items():
                                gap = page_idx - prev['page_idx']
                                if 1 <= gap <= 2:
                                    overlap = min(x1n, prev['x1n']) - max(x0n, prev['x0n'])
                                    if overlap / ref_w >= 0.5 and prev['page_idx'] > best_page:
                                        best_page = prev['page_idx']
                                        best_ref = prev_ref
                        tbl_ref = best_ref
                        # Guard: do NOT stitch a caption-less Textract "table"
                        # as a continuation when a NEW section/annex heading
                        # sits above it on this page.  Bulleted lists (e.g.
                        # "C.1 General" items a–h, "11.3 Freedom from defects"
                        # items a–d) are detected as TABLEs by Textract; without
                        # this guard they were stitched onto the previous real
                        # table (B.2 / Table 8) and rendered as stray images in
                        # the wrong section.  A genuine table continuation page
                        # carries only a "Table N (continued)" header — not a
                        # section-number heading — so it is unaffected.
                        if tbl_ref is not None:
                            _new_sec_re = re.compile(
                                r'^(?:Annex\s+[A-Z]\b|\d+(?:\.\d+)*\s+\S'
                                r'|[A-Z]\.\d+(?:\.\d+)*\s+\S)')
                            for _hb in _page_layout.get(page_idx, {}).get('layout', []):
                                if _hb['type'] not in ('LAYOUT_SECTION_HEADER',
                                                       'LAYOUT_HEADER', 'LAYOUT_TITLE'):
                                    continue
                                _hb_txt = (_hb.get('text') or '').strip()
                                if _TABLE_CAPTION_RE.match(_hb_txt):
                                    continue  # "Table N (continued)" — not a new section
                                if (_hb['bbox'][1] < y0n - 0.005
                                        and _new_sec_re.match(_hb_txt)):
                                    logger.info(f"[prescan] page {page_idx}: NOT stitching "
                                          f"caption-less table as continuation of "
                                          f"{tbl_ref} — new section heading above "
                                          f"({_hb_txt[:40]!r})")
                                    tbl_ref = None
                                    break
                        if tbl_ref is None:
                            continue  # no caption and no nearby continuation → skip

                # Fix A: Snap right boundary to full page text area when the table
                # starts near the left margin.  Textract under-detects the right
                # edge of tall multi-page tables — it stops at the dense-content
                # zone and misses sparse right columns or lightly-bordered cells.
                # ISO standards use single-column A4 layout so every table that
                # starts at the left margin should extend to the right margin
                # (~88 % of page width).  We only trigger this when:
                #   • x0n < 0.15  — table clearly starts at/near left margin
                #   • x1n < 0.85  — textract right edge is short of right margin
                # The union-bbox logic below then propagates this expanded extent
                # to ALL continuation pages automatically.
                _RIGHT_MARGIN_N = 0.97   # normalised right boundary — extended to capture footnotes at page edge
                _LEFT_MARGIN_N  = 0.05   # normalised left boundary  (~left text margin)
                if x0n < 0.30 and x1n < 0.85:
                    x0n = min(x0n, _LEFT_MARGIN_N)
                    x1n = max(x1n, _RIGHT_MARGIN_N)
                    logger.info(f"[prescan] table {tbl_ref} page={page_idx}: "
                          f"expanded x to full-page width ({x0n:.2f}–{x1n:.2f})")

                # Union-bbox: ensure every continuation page uses the widest
                # x-extent seen so far for this table (Fix A — original part).
                if tbl_ref not in _tbl_x_union:
                    _tbl_x_union[tbl_ref] = {'x0n': x0n, 'x1n': x1n}
                else:
                    x0n = min(x0n, _tbl_x_union[tbl_ref]['x0n'])
                    x1n = max(x1n, _tbl_x_union[tbl_ref]['x1n'])
                    _tbl_x_union[tbl_ref]['x0n'] = x0n
                    _tbl_x_union[tbl_ref]['x1n'] = x1n

                # Fix: if Textract y1n looks short vs the LLM bbox for the same
                # table on the same page, extend y1n to the LLM value so bottom
                # rows are not cropped (e.g. Table 11).
                if llm_pages:
                    _llm_pd = next((p for p in llm_pages if p and p.get("page_idx") == page_idx), None)
                    if _llm_pd:
                        for _ltbl in (_llm_pd.get("tables") or []):
                            _ltbl_cap = (_ltbl.get("caption") or "").strip()
                            _ltbl_m = _TABLE_CAPTION_RE.match(_ltbl_cap)
                            if not _ltbl_m or _ltbl_m.group(1) != tbl_ref:
                                continue
                            _ltbl_bbox = _ltbl.get("bbox_norm") or []
                            if len(_ltbl_bbox) != 4:
                                continue
                            _llm_y0 = _ltbl_bbox[1]
                            _llm_y1 = _ltbl_bbox[3]
                            # Do NOT extend across a page break.  When this table
                            # CONTINUES on the next page ("Table N (continued)"),
                            # the LLM bbox describes the WHOLE logical (multi-page)
                            # table, so extending this page's crop to _llm_y1 stretches
                            # it down to swallow the shared footnotes → a bloated,
                            # near-empty fragment (ISO 11608-4 Table 12 page 1).  Skip
                            # the extension so this page keeps its true on-page extent;
                            # the continuation page captures the rest.  Single-page
                            # tables (e.g. Table 11) have no (continued) page and are
                            # unaffected.
                            _next_pd = next((p for p in llm_pages
                                             if p and p.get("page_idx") == page_idx + 1), None)
                            _next_txt = (_next_pd or {}).get("text", "") or ""
                            _tbl_continues = bool(re.search(
                                r'Table\s+' + re.escape(str(tbl_ref)) + r'\b[^\n]*\(continued\)',
                                _next_txt, re.IGNORECASE))
                            if _llm_y1 > y1n + 0.01 and not _tbl_continues:
                                logger.info(f"[prescan] table {tbl_ref} page={page_idx}: "
                                      f"extending y1n {y1n:.3f}→{_llm_y1:.3f} from LLM bbox")
                                y1n = _llm_y1
                            elif _llm_y1 > y1n + 0.01 and _tbl_continues:
                                logger.info(f"[prescan] table {tbl_ref} page={page_idx}: "
                                      f"LLM y1 extension {y1n:.3f}→{_llm_y1:.3f} SKIPPED "
                                      f"(table continues on next page)")

                # A table ref already in tables_pages is a continuation ONLY when
                # the gap to the first-seen page is ≤8 pages.  A larger gap means
                # a physically distinct table with the same caption number (e.g.
                # Table 12 appears in both section 8.9.2 and 8.10.2).  In that case
                # use a page-suffixed key so the two images are stored separately
                # and _extract_body can serve the right one per section.
                _tbl_first_pg = tables_first_page.get(tbl_ref)
                if tbl_ref in tables_pages and _tbl_first_pg is not None:
                    if page_idx - _tbl_first_pg <= 8:
                        is_continuation = True
                    else:
                        tbl_ref = f'{tbl_ref}_{page_idx}'
                        logger.info(f"[prescan] table ref conflict p{page_idx}: using key {tbl_ref!r}")
                        is_continuation = False
                else:
                    is_continuation = False
                top_pad = 55 if is_continuation else _SUB_HEADER_PTS
                _TBL_H_PAD = 12  # horizontal padding so outer border lines are not clipped
                # Cap clip top at the y1 of the closest previous Textract item on this page
                # so upward padding (_SUB_HEADER_PTS) never bleeds into a preceding table.
                _prev_y1_pts = 0  # default: page top
                for _pb in (_page_layout.get(page_idx, {}).get('tables', []) +
                            _page_layout.get(page_idx, {}).get('figures', [])):
                    _pb_y1_pts = _pb[3] * ph
                    if _pb_y1_pts < y0n * ph - 2:  # item ends above current table top
                        _prev_y1_pts = max(_prev_y1_pts, _pb_y1_pts)
                # Cap clip bottom at the y0 of the next Textract-detected item on this
                # page (table or figure) so that extended y1n values (Fix 1 / LLM)
                # never bleed into the next item's territory.
                _next_y0_pts = ph - _FOOTER_PTS  # default: page bottom
                for _nb in (_page_layout.get(page_idx, {}).get('tables', []) +
                            _page_layout.get(page_idx, {}).get('figures', [])):
                    _nb_y0_pts = _nb[1] * ph
                    if _nb_y0_pts > y1n * ph + 2:  # item starts below current table bottom
                        _next_y0_pts = min(_next_y0_pts, _nb_y0_pts)
                # Detect landscape page: majority of text lines have upward direction
                # (dir≈(0,-1)), meaning the page is a 90°CCW landscape table.
                _pg_rot_lines = sum(
                    1 for b in page.get_text("dict").get("blocks", [])
                    for l in b.get("lines", [])
                    if abs(l.get("dir", (1, 0))[0]) < 0.1 and l.get("dir", (1, 0))[1] < 0)
                _pg_nrm_lines = sum(
                    1 for b in page.get_text("dict").get("blocks", [])
                    for l in b.get("lines", [])
                    if abs(l.get("dir", (1, 0))[0]) >= 0.1)
                _is_landscape_page = _pg_rot_lines > _pg_nrm_lines

                # Change 1 (extended): scan this page for the FIRST caption
                # of any kind — table OR figure — that sits below the current
                # table's top.  Use it to cap both the drawing-scan range and
                # _clip_y1 so one table's PNG never bleeds into the next item.
                _next_tbl_cap_y = ph  # default: no next caption found on page
                for _ntb in sorted(page.get_text("blocks"), key=lambda b: b[1]):
                    if len(_ntb) < 7 or _ntb[6] != 0:
                        continue
                    _ntb_txt = (_ntb[4] or "").strip()
                    _ntb_tbl_m = _TABLE_CAPTION_RE.match(_ntb_txt)
                    _ntb_fig_m = (_FIGURE_CAPTION_RE.match(_ntb_txt)
                                  or _ANNEX_FIG_CAP_RE.match(_ntb_txt))
                    # Skip: this is the current table's own caption
                    if _ntb_tbl_m and _ntb_tbl_m.group(1) == tbl_ref:
                        continue
                    if not (_ntb_tbl_m or _ntb_fig_m):
                        continue
                    if _ntb[1] > y0n * ph + 5:  # at least 5pt below current table top
                        _next_tbl_cap_y = _ntb[1]
                        _ntb_what = (f"Table {_ntb_tbl_m.group(1)}" if _ntb_tbl_m
                                     else f"Figure {_ntb_fig_m.group(1)}")
                        logger.info(f"[prescan] table {tbl_ref} p{page_idx}: next "
                              f"caption {_ntb_what} at "
                              f"y={_ntb[1]:.1f}pt → capping clip_y1 at "
                              f"{_next_tbl_cap_y - 3:.1f}pt")
                        break

                _draw_tbl_bottom = None
                if _is_landscape_page:
                    # For landscape pages the PDF y-axis maps to the visual column axis.
                    # Drawing-scan horizontal lines are column separators (not row borders)
                    # and the text-below scan assumes portrait — skip both.
                    _clip_y1 = min(ph - _FOOTER_PTS, y1n * ph + _BOTTOM_PAD,
                                   _next_tbl_cap_y - 3)
                else:
                    # Walk text blocks below the table bottom:
                    # • footnote-pattern lines (single letter + 2+ spaces, or "NOTE ")
                    #   are absorbed into the clip — they belong to the table image and
                    #   would otherwise leak as spurious body rows in _extract_body.
                    # • the first non-footnote block becomes _first_text_below_pts.
                    _TFOOT_RE = re.compile(r'^[a-zA-Z]\s{2,}|^NOTE\s', re.IGNORECASE)
                    _first_text_below_pts = ph - _FOOTER_PTS
                    _footnote_y1_pts = y1n * ph  # extended when footnotes are absorbed
                    for _ftb in sorted(page.get_text("blocks"), key=lambda b: b[1]):
                        if len(_ftb) < 7 or _ftb[6] != 0:
                            continue
                        if _ftb[1] < y1n * ph + 2:
                            continue  # at or above the table bottom — skip
                        _ftb_txt = (_ftb[4] or "").strip()
                        if _ftb[1] <= y1n * ph + 80 and _TFOOT_RE.match(_ftb_txt):
                            _footnote_y1_pts = max(_footnote_y1_pts, _ftb[3])
                        else:
                            _first_text_below_pts = _ftb[1]
                            break
                    # Scan page drawings for the last major horizontal line within
                    # the table bbox.  When the LLM extended y1n past the actual
                    # grid bottom (e.g. Table 1: LLM y1=0.689 but grid ends at
                    # 0.615), the footnote-scan loop skips text that falls between
                    # the grid and y1n.  The drawing-based cap catches that case.
                    # For continuation pages Textract y1n often undershoots
                    # (only header rows detected).  Extend the drawing scan
                    # to the first real text below the table so the actual
                    # last grid line is found.
                    _draw_scan_top = (
                        min(_first_text_below_pts - 5, _next_tbl_cap_y - 3)
                        if is_continuation
                        else min(y1n * ph, _next_tbl_cap_y - 3)
                    )
                    for _draw_path in page.get_drawings():
                        _draw_r = _draw_path['rect']
                        if (_draw_r.height < 2 and _draw_r.width > 80 and
                                y0n * ph - 5 <= _draw_r.y0 <=
                                _draw_scan_top + 2):
                            if _draw_tbl_bottom is None or _draw_r.y0 > _draw_tbl_bottom:
                                _draw_tbl_bottom = _draw_r.y0
                    _draw_clip_extras = (
                        [_draw_tbl_bottom + 4] if _draw_tbl_bottom is not None else []
                    )
                    _clip_y1 = min(ph - _FOOTER_PTS,
                                   _footnote_y1_pts + _BOTTOM_PAD,
                                   _next_y0_pts - _SUB_HEADER_PTS - 5,
                                   _first_text_below_pts - 4,
                                   _next_tbl_cap_y - 3,
                                   *_draw_clip_extras)
                    # BBOX FLOOR (guarded) — fix bottom-row truncation without
                    # over-capturing body text below the table.
                    #
                    # Root cause of truncation (Table 2 continuation, Table H.1
                    # etc.): on borderless / header-only continuation rows the
                    # fitz draw-line scan latches onto an INTERIOR row separator
                    # well above the true bottom, so _clip_y1 cuts the last rows.
                    #
                    # We only intervene when there is NO reliable bottom border
                    # grid line — i.e. the deepest detected horizontal line
                    # (_draw_tbl_bottom) is absent or sits clearly ABOVE the
                    # Textract cell-envelope bottom.  In that case we floor the
                    # clip at the deeper of the Textract cell bottom / LLM-
                    # extended bottom so the real last rows are captured.
                    #
                    # When a real bottom border IS present (draw line at/below
                    # the cell bottom) the existing draw-based clip is already
                    # correct and is left untouched — this prevents over-
                    # extending small bordered tables (e.g. Table 6) into the
                    # following paragraph, and ignores an over-eager LLM bbox.
                    #
                    # The floor is always bounded by the next detected item, the
                    # next caption and the footer so it can never bleed into
                    # following content.
                    _tx_cell_bot = _tx_cell_y1n * ph
                    if (_draw_tbl_bottom is None
                            or _draw_tbl_bottom < _tx_cell_bot - 8):
                        _true_bottom = max(_tx_cell_bot, y1n * ph)
                        _tx_floor = min(_true_bottom + 12,
                                        _next_y0_pts - 5,
                                        ph - _FOOTER_PTS,
                                        _next_tbl_cap_y - 3)
                        if _tx_floor > _clip_y1:
                            logger.info(f"[prescan-clip] tbl={tbl_ref} pg={page_idx}: "
                                  f"bbox-floor lifts clip_y1 {_clip_y1:.1f}→"
                                  f"{_tx_floor:.1f} (no bottom border; "
                                  f"cell y1={_tx_cell_bot:.1f}pt "
                                  f"draw_bot={_draw_tbl_bottom})")
                            _clip_y1 = _tx_floor

                    # ── Textract TABLE_FOOTER extension (vision path) ──────────
                    # A table's footnote rows ("a …", "b …", "NOTE …") sit BELOW
                    # the last cell row but INSIDE the table's outer border.  On
                    # garbled/"printed" PDFs the fitz footnote scan above only
                    # sees ciphered text, so those rows are cut off (22442-1
                    # Table D.3 lost its a/b footnotes).  Textract labels them
                    # TABLE_FOOTER and reads their position reliably from the
                    # rendered image.  Extend the clip to the deepest TABLE_FOOTER
                    # that starts just below the Textract cell envelope, bounded
                    # by the next section heading, the next table/figure/caption
                    # and the page footer — so it can never bleed into body text.
                    # Fires ONLY when such a TABLE_FOOTER exists (no footnotes →
                    # no change → identical output for every other table).
                    _tf_stop_n = 1.0          # next section header (page fraction)
                    _tf_bottom_n = None       # deepest TABLE_FOOTER bottom
                    for _tf_lb in _page_layout.get(page_idx, {}).get('layout', []):
                        _tf_type = _tf_lb.get('type') or ''
                        _tf_y0 = _tf_lb['bbox'][1]
                        _tf_y1 = _tf_lb['bbox'][3]
                        if _tf_type == 'TABLE_FOOTER':
                            # Must start close below this table's cell bottom
                            # (same table, not a footer further down the page).
                            # Widened lower tolerance (was -0.005): a TABLE_FOOTER
                            # often OVERLAPS the cell envelope (its top sits a few pt
                            # ABOVE the envelope bottom, e.g. ISO 11608-4 Table 5's
                            # "If voltage falls…" row at y0=0.719 vs cell y1=0.726).
                            # -0.005 rejected it and the footnote row was sliced off;
                            # -0.03 adopts an overlapping footer. Still bounded below
                            # by next heading/table/footer, so it can't bleed.
                            if _tx_cell_y1n - 0.03 <= _tf_y0 <= _tx_cell_y1n + 0.10:
                                _tf_bottom_n = max(_tf_bottom_n or 0.0, _tf_y1)
                        elif _tf_type in ('LAYOUT_SECTION_HEADER',
                                          'LAYOUT_TITLE'):
                            if _tf_y0 >= _tx_cell_y1n - 0.005:
                                _tf_stop_n = min(_tf_stop_n, _tf_y0)
                    if _tf_bottom_n is not None and _tf_bottom_n > _tx_cell_y1n + 0.01:
                        _tf_bottom_n = min(_tf_bottom_n, _tf_stop_n - 0.004)
                        _tf_clip = min(_tf_bottom_n * ph + _BOTTOM_PAD,
                                       _next_y0_pts - 5,
                                       ph - _FOOTER_PTS,
                                       _next_tbl_cap_y - 3)
                        if _tf_clip > _clip_y1:
                            logger.info(f"[prescan-clip] tbl={tbl_ref} pg={page_idx}: "
                                  f"TABLE_FOOTER extends clip_y1 "
                                  f"{_clip_y1:.1f}→{_tf_clip:.1f} "
                                  f"(cell y1={_tx_cell_y1n * ph:.1f}pt "
                                  f"foot y1={_tf_bottom_n * ph:.1f}pt)")
                            _clip_y1 = _tf_clip
                logger.info(f"[prescan-clip] tbl={tbl_ref} pg={page_idx}: "
                      f"landscape={_is_landscape_page} y1n={y1n:.3f} "
                      f"clip_y1={_clip_y1:.1f} draw_bot={_draw_tbl_bottom}")
                _clip_y0 = max(0, _prev_y1_pts + 1, y0n * ph - top_pad)
                # Change 2: when the matched caption sits significantly above
                # Textract's y0 (Textract missed the table's top rows), extend
                # the clip upward to start just above the caption.
                # Only on first-page captures — continuation pages have no
                # caption on the current page so the scan would find nothing.
                if not is_continuation:
                    for _cb2 in sorted(page.get_text("blocks"), key=lambda b: b[1]):
                        if len(_cb2) < 7 or _cb2[6] != 0:
                            continue
                        _cb2_m = _TABLE_CAPTION_RE.match((_cb2[4] or "").strip())
                        if _cb2_m and _cb2_m.group(1) == tbl_ref:
                            _cap_y0_pts = _cb2[1]
                            if _cap_y0_pts < y0n * ph - 2:  # Fix B: was 10pt — now 2pt catches small tables
                                _clip_y0_up = max(0, _prev_y1_pts + 1,
                                                  _cap_y0_pts - 5)
                                logger.info(f"[prescan] table {tbl_ref} p{page_idx}: "
                                      f"extending clip_y0 upward "
                                      f"{_clip_y0:.1f}→{_clip_y0_up:.1f}pt "
                                      f"(caption at {_cap_y0_pts:.1f}pt, "
                                      f"Textract y0={y0n*ph:.1f}pt)")
                                _clip_y0 = _clip_y0_up
                            break
                clip = fitz.Rect(
                    max(0,   x0n * pw - _TBL_H_PAD),
                    _clip_y0,
                    min(pw,  x1n * pw + _TBL_H_PAD),
                    _clip_y1,
                )
                # Extend clip leftward so the leftmost column is never clipped.
                # On garbled/Techstreet PDFs the text-block x-positions are
                # scrambled, so use DRAWING PATHS (table border lines) as the
                # authoritative source.  Find the leftmost vertical stroke inside
                # the clip's Y band — that's the table's left border line.
                # Fall back to text-block x0 only if no drawing path is found.
                _tbl_min_x0 = clip.x0
                for _tdrw in page.get_drawings():
                    _tdr = _tdrw.get('rect')
                    if not _tdr:
                        continue
                    # Vertical table-border lines: narrow (≤4pt) and tall (≥15pt)
                    if _tdr.width > 4 or _tdr.height < 15:
                        continue
                    # Must overlap the clip's Y band
                    if _tdr.y1 < clip.y0 + 5 or _tdr.y0 > clip.y1 - 5:
                        continue
                    if _tdr.x0 < _tbl_min_x0:
                        _tbl_min_x0 = float(_tdr.x0)
                if _tbl_min_x0 >= clip.x0 - 2:
                    # No drawing-path improvement — try text blocks as fallback
                    for _txblk in page.get_text("blocks"):
                        if len(_txblk) < 7 or _txblk[6] != 0:
                            continue
                        if _txblk[3] < clip.y0 + 5 or _txblk[1] > clip.y1 - 5:
                            continue
                        if _txblk[0] < _tbl_min_x0:
                            _tbl_min_x0 = float(_txblk[0])
                if _tbl_min_x0 < clip.x0 - 2:
                    _tbl_new_x0 = max(0.0, _tbl_min_x0 - 3)
                    logger.info(f"[prescan] table {tbl_ref} p{page_idx}: "
                          f"extending clip.x0 {clip.x0:.1f}→{_tbl_new_x0:.1f}pt "
                          f"(leftmost border/text at {_tbl_min_x0:.1f}pt)")
                    clip = fitz.Rect(_tbl_new_x0, clip.y0, clip.x1, clip.y1)
                if clip.width <= 5 or clip.height <= 5:
                    continue

                # Dedup: skip if this (tbl_ref, page_idx) was already captured.
                # Textract sometimes returns two overlapping bboxes on the same page
                # that both get matched to the same caption, producing duplicate images.
                if page_idx in _tbl_cap_pages.get(tbl_ref, set()):
                    logger.info(f"[prescan] table {tbl_ref} page={page_idx}: "
                          f"skipping duplicate bbox (already captured this page)")
                    continue

                pix = page.get_pixmap(clip=clip, dpi=200)
                _tbl_png, _tbl_was_rot = _correct_landscape_png(pix.tobytes("png"), page)
                if _tbl_was_rot:
                    logger.info(f"[prescan] table {tbl_ref} page={page_idx}: rotated 90 CW (landscape)")
                tables_pages.setdefault(tbl_ref, []).append(_tbl_png)
                _tbl_cap_pages.setdefault(tbl_ref, set()).add(page_idx)
                if tbl_ref not in tables_first_page:
                    tables_first_page[tbl_ref] = page_idx
                if tbl_ref not in tbl_captions:
                    tbl_captions[tbl_ref] = f'Table {tbl_ref}'
                table_rects_by_page.setdefault(page_idx, []).append(
                    (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1))
                )
                _last_tbl[tbl_ref] = {'x0n': x0n, 'x1n': x1n, 'page_idx': page_idx}
                logger.info(f"[vision-path] table {tbl_ref} page={page_idx}: {clip.width:.0f}x{clip.height:.0f}")

            for fig_norm in tx['figures']:
                x0n, y0n, x1n, y1n = fig_norm
                # Textract's LAYOUT_FIGURE bbox = the diagram itself, captured
                # before the LLM union (below) may inflate it.  Used to clamp
                # the final clip so an over-large LLM image bbox cannot pull the
                # figure crop above the diagram into the preceding paragraph
                # (Figure 3 bled "…age warning" above and items n/o below).
                _fig_diag_y0n = y0n
                _fig_diag_y1n = y1n
                # RAW Textract figure size (before any LLM union / x-extension) —
                # used to reject caption-less glyph-sized false positives below.
                _tx_fig_w0 = x1n - x0n
                _tx_fig_h0 = y1n - y0n
                _tx_fig_area0 = _tx_fig_w0 * _tx_fig_h0
                # Expand Textract clip using the matching LLM image bbox when Textract
                # under-detects the figure extent (e.g. returns a tiny region).
                # Guard: only union with LLM images that carry an explicit figure caption
                # ("Figure N — ...").  Captionless LLM detections may cover multiple
                # adjacent figures on the same page; unguarded overlap expansion would
                # pull the next figure's content into this figure's capture region.
                if llm_pages and _VISION_USE_LLM_BOXES:
                    _pd2 = next((p for p in llm_pages if p and p.get("page_idx") == page_idx), None)
                    if _pd2:
                        for _ie2 in _pd2.get("images", []):
                            _ie2_cap = (_ie2.get("caption") or "").strip()
                            if not _FIGURE_CAPTION_RE.match(_ie2_cap):
                                continue  # skip: no explicit figure caption — too risky to expand
                            _eb2 = _ie2.get("bbox_norm") or []
                            if len(_eb2) != 4:
                                continue
                            _lw2 = _eb2[2] - _eb2[0]
                            _lh2 = _eb2[3] - _eb2[1]
                            _xovl2 = min(x1n, _eb2[2]) - max(x0n, _eb2[0])
                            _yovl2 = min(y1n, _eb2[3]) - max(y0n, _eb2[1])
                            if (_lw2 > 0 and _lh2 > 0
                                    and _xovl2 / _lw2 >= 0.30
                                    and _yovl2 / _lh2 >= 0.20):
                                x0n = min(x0n, _eb2[0])
                                y0n = min(y0n, _eb2[1])
                                x1n = max(x1n, _eb2[2])
                                y1n = max(y1n, _eb2[3])
                                logger.info(f"[prescan] figure page={page_idx}: LLM bbox union "
                                      f"({x0n:.2f},{y0n:.2f}–{x1n:.2f},{y1n:.2f})")
                                break
                _FIG_PAD = 15
                clip = fitz.Rect(
                    max(0,  x0n * pw - _FIG_PAD), max(0,  y0n * ph - _FIG_PAD),
                    min(pw, x1n * pw + _FIG_PAD), min(ph, y1n * ph + _FIG_PAD),
                )
                if clip.width <= 5 or clip.height <= 5:
                    continue

                # Priority 0: Textract-layout caption BELOW the diagram.
                # ISO always places a figure's "Figure N —" caption directly
                # BELOW the figure.  Using the reliable Textract layout, pick
                # the CLOSEST such caption below this diagram that has NO other
                # figure diagram between it and this diagram.  This prevents a
                # stacked figure (e.g. 11608-4 p32: Figure 7's caption sits
                # ABOVE Figure 8's diagram) from stealing the wrong caption —
                # Figure 8's diagram now correctly binds to "Figure 8" below it,
                # not "Figure 7" above.  For single-figure pages (all 11608-1
                # figures) this yields the same caption as before, so it is a
                # safe refinement.
                fig_cap = None
                _p0_lay = _page_layout.get(page_idx, {}).get('layout', [])
                _p0_figs = _page_layout.get(page_idx, {}).get('figures', [])
                _p0_below = []
                for _p0c in _p0_lay:
                    _p0t = (_p0c.get('text') or '').strip()
                    _p0m = (_FIGURE_CAPTION_RE.match(_p0t)
                            or _ANNEX_FIG_CAP_RE.match(_p0t))
                    if _p0m and _p0c['bbox'][1] > y1n - 0.01:
                        _p0_below.append((_p0c['bbox'][1], _p0m.group(1)))
                _p0_below.sort()
                for _p0y, _p0ref in _p0_below:
                    _p0_blocked = any(
                        y1n + 0.01 < _of[1] < _p0y - 0.01
                        and not (_of[1] >= y0n - 0.001 and _of[3] <= y1n + 0.001)
                        for _of in _p0_figs)
                    if not _p0_blocked:
                        fig_cap = _p0ref
                        break

                # Priority 1: LLM image bbox overlap — most accurate on pages with
                # multiple figures (e.g. page 37 has both Figure 7 and Figure 8).
                # Pick the LLM image with the highest overlap ratio to the Textract bbox.
                if fig_cap is None and llm_pages and _VISION_USE_LLM_BOXES:
                    _pd2 = next((p for p in llm_pages if p and p.get("page_idx") == page_idx), None)
                    if _pd2:
                        _best_ovl = 0.0
                        _no_bbox_cap = None
                        for _ie in _pd2.get("images", []):
                            _ec = (_ie.get("caption") or "").strip()
                            _ecm_chk = (_FIGURE_CAPTION_RE.match(_ec)
                                        or _ANNEX_FIG_CAP_RE.match(_ec))
                            if not _ecm_chk:
                                continue
                            _eb = _ie.get("bbox_norm") or []
                            if len(_eb) != 4:
                                if _no_bbox_cap is None:
                                    _no_bbox_cap = _ecm_chk.group(1)
                                continue
                            _llm_w = _eb[2] - _eb[0]
                            _llm_h = _eb[3] - _eb[1]
                            _xovl = min(x1n, _eb[2]) - max(x0n, _eb[0])
                            _yovl = min(y1n, _eb[3]) - max(y0n, _eb[1])
                            if _llm_w > 0 and _llm_h > 0 and _xovl > 0 and _yovl > 0:
                                _ovl_ratio = (_xovl * _yovl) / (_llm_w * _llm_h)
                                if _ovl_ratio > _best_ovl:
                                    _best_ovl = _ovl_ratio
                                    fig_cap = _ecm_chk.group(1)
                        if fig_cap is None:
                            fig_cap = _no_bbox_cap  # fallback: any captioned image with no bbox

                # Priority 2: text-based search — used when LLM page data is absent
                if fig_cap is None:
                    fig_cap = _find_caption_above(llm_texts_local, page_idx, y0n, _FIGURE_CAPTION_RE)
                if fig_cap is None:
                    fig_cap = _find_caption_below(llm_texts_local, page_idx, y1n, _FIGURE_CAPTION_RE)
                # Priority 3: Annex figure captions (Figure A.1, H.2, F.1, etc.)
                if fig_cap is None:
                    fig_cap = _find_caption_above(llm_texts_local, page_idx, y0n, _ANNEX_FIG_CAP_RE)
                if fig_cap is None:
                    fig_cap = _find_caption_below(llm_texts_local, page_idx, y1n, _ANNEX_FIG_CAP_RE)
                # ── Reject glyph-sized caption-less Textract false positives ──
                # Textract sometimes tags a tiny symbol/glyph (e.g. a character in
                # the "Symbols and abbreviated terms" list, or a small mark on a
                # front-matter page) as a LAYOUT_FIGURE.  With NO "Figure N"
                # caption these become throwaway unlabeled figures that the
                # x-extension below stretches into a full-width TEXT strip, which
                # the flush loop then drops into the wrong section (11608-1: the
                # ρ/p symbol strip landing in the Foreword and clause 4).  A real
                # figure is always a sizeable diagram, so drop a detection that is
                # BOTH caption-less AND glyph-sized (raw Textract bbox < ~1% of the
                # page, or a sliver).  The symbol/notation TEXT is unaffected — it
                # is captured separately as body text; only the redundant, mis-
                # placed image strip is removed.  Captioned figures (any size) and
                # real diagrams are never touched.
                if fig_cap is None and (_tx_fig_area0 < 0.01
                                        or _tx_fig_w0 < 0.05
                                        or _tx_fig_h0 < 0.03):
                    logger.info(f"[prescan] page {page_idx}: dropping caption-less "
                          f"glyph-sized Textract figure (w={_tx_fig_w0:.3f} "
                          f"h={_tx_fig_h0:.3f} area={_tx_fig_area0:.4f}) — "
                          f"not a real figure")
                    continue
                fig_num = _parse_fig_ref(fig_cap) if fig_cap else None
                if fig_num is None:
                    fig_num = -(len(figures) + 1)

                # ── Defer a CROSS-PAGE ORPHAN diagram to the cross-page pass ──
                # A diagram near the BOTTOM of this page (y1 > 0.62) with NO
                # figure caption below it on this page, while the NEXT page
                # opens with a "Figure N" caption near its top — this diagram is
                # the START of a cross-page figure (diagram here, caption+Key on
                # the next page).  Capturing it here would mis-bind it to the
                # PREVIOUS figure's caption above it (e.g. the p31 diagram would
                # be mislabelled "Figure 6").  Skip it; the cross-page pass
                # after this loop stitches and labels it correctly.  This
                # pattern never occurs in 11608-1 (its figure captions are
                # mid-page, same page as the diagram), so 11608-1 is unaffected.
                if y1n > 0.62:
                    _cpo_below = any(
                        (_FIGURE_CAPTION_RE.match((_c.get('text') or '').strip())
                         or _ANNEX_FIG_CAP_RE.match((_c.get('text') or '').strip()))
                        and _c['bbox'][1] > y1n - 0.01
                        for _c in _page_layout.get(page_idx, {}).get('layout', []))
                    _cpo_next_top = any(
                        (_FIGURE_CAPTION_RE.match((_c.get('text') or '').strip())
                         or _ANNEX_FIG_CAP_RE.match((_c.get('text') or '').strip()))
                        and _c['bbox'][1] < 0.32
                        for _c in _page_layout.get(page_idx + 1, {}).get('layout', []))
                    if (not _cpo_below) and _cpo_next_top:
                        logger.info(f"[prescan] page {page_idx}: deferring cross-page "
                              f"orphan diagram (y{y0n:.2f}-{y1n:.2f}) to "
                              f"cross-page pass")
                        continue

                # ── BBOX-DRIVEN figure extent: pull Key + NOTE lines into
                # the figure PNG.  Textract's LAYOUT_FIGURE bbox wraps only
                # the diagram; a figure's "Key" legend and explanatory NOTE
                # lines sit BELOW the diagram and ABOVE the "Figure N — ..."
                # caption.  fitz text is garbled on printed ISO PDFs (NOTE
                # lines read as blank), but Textract reads the rendered image
                # so its layout-block text AND position are reliable.  Locate
                # this figure's caption among Textract layout blocks and
                # extend the clip down to just above it — everything between
                # (Key label, key items, NOTE 1/2) is captured automatically.
                # Bounded by any other detected figure/table so it can never
                # cross into a neighbouring element.
                # Run for any CAPTIONED figure — numeric ("2") OR annex ("C.1",
                # "D.1").  Annex figures get a negative fig_num from
                # _parse_fig_ref, so the old `fig_num > 0` gate skipped them and
                # their Key/legend (which sits below the diagram) was never
                # pulled into the crop.  Gate on the caption ref string instead,
                # and build the caption regex from that ref ("Figure C.1"), not
                # from the numeric key (which would be "-67001").
                if fig_cap:
                    _cap_re_tx = re.compile(
                        r'Figure\s+' + re.escape(str(fig_cap)) + r'\b', re.IGNORECASE)
                    _tx_layout = _page_layout.get(page_idx, {}).get('layout', [])
                    _cap_y0n_tx = None
                    for _lb in _tx_layout:
                        if _cap_re_tx.search(_lb.get('text', '') or ''):
                            _lb_y0n = _lb['bbox'][1]
                            # caption below the DIAGRAM top (use the Textract
                            # diagram bbox, not the LLM-inflated y0n)
                            if _lb_y0n > _fig_diag_y0n + 0.02:
                                _cap_y0n_tx = _lb_y0n
                                break
                    if _cap_y0n_tx is not None:
                        # Barrier: stop only at a DIFFERENT figure's caption or a
                        # real "Table N" caption between the diagram and this
                        # figure's caption.  A CAPTION-LESS table in that band is
                        # the figure's OWN Key/legend — Textract detects
                        # multi-column Key definition lists as TABLE blocks
                        # (11608-4 Figures 2/6/7/8), so barriering on the raw
                        # table bbox (the previous logic) cut the entire Key.
                        # Only a captioned neighbour is a genuine boundary.
                        _fig_barrier = _cap_y0n_tx
                        for _lb2 in _tx_layout:
                            _lb2_y0 = _lb2['bbox'][1]
                            if not (_fig_diag_y1n + 0.005 < _lb2_y0
                                    < _cap_y0n_tx - 0.005):
                                continue
                            _lb2_txt = (_lb2.get('text') or '').strip()
                            _b_fig = (_FIGURE_CAPTION_RE.match(_lb2_txt)
                                      or _ANNEX_FIG_CAP_RE.match(_lb2_txt))
                            _b_tbl = _TABLE_CAPTION_RE.match(_lb2_txt)
                            if _b_fig and _b_fig.group(1) != str(fig_cap):
                                _fig_barrier = min(_fig_barrier, _lb2_y0)
                            elif _b_tbl:
                                _fig_barrier = min(_fig_barrier, _lb2_y0)
                        # The figure ends just ABOVE its caption (the caption is
                        # emitted separately as the figure's text row, so it must
                        # not be baked in).  _FIG_PAD is added back when the clip
                        # Rect is built, so subtract it plus a small gap.
                        #
                        # This is a HARD bound applied in BOTH directions:
                        #   • EXTEND down to pull in Key/NOTE lines when the
                        #     diagram bbox stopped short (Figure 4).
                        #   • CONTRACT up when an over-large LLM image bbox
                        #     pushed the bottom past the caption into the
                        #     following body text (Figure 3: items n/o bled in).
                        _cap_margin = _FIG_PAD / ph + 0.004
                        _cap_bottom = min(_cap_y0n_tx, _fig_barrier) - _cap_margin
                        # Never cut into the diagram itself.
                        if _cap_bottom >= _fig_diag_y1n - 0.005:
                            if _cap_bottom < y1n - 0.001:
                                logger.info(f"[prescan] fig {fig_num} p{page_idx}: "
                                      f"contracting y1n {y1n:.3f}→{_cap_bottom:.3f}"
                                      f" to caption (was bleeding past caption "
                                      f"cap_y0={_cap_y0n_tx:.3f})")
                                y1n = _cap_bottom
                            elif (_cap_bottom > y1n + 0.001
                                    and (_cap_bottom - y1n) < 0.45):
                                logger.info(f"[prescan] fig {fig_num} p{page_idx}: "
                                      f"extending y1n {y1n:.3f}→{_cap_bottom:.3f} "
                                      f"to include Key/Notes above caption "
                                      f"(cap_y0={_cap_y0n_tx:.3f})")
                                y1n = _cap_bottom
                        # Clamp the TOP so an over-large LLM bbox cannot bleed
                        # the crop up into the preceding paragraph.  Clamp to
                        # essentially the diagram top — the _FIG_PAD added when
                        # the clip Rect is built (~15pt ≈ 0.019) already leaves
                        # room for a single units/"Dimensions" label line just
                        # above the diagram, without reaching the body text
                        # further up (Figure 3: body text ended at y≈0.309,
                        # "Dimensions" at 0.321, diagram at 0.334).
                        _diag_top_clamp = max(0.0, _fig_diag_y0n - 0.002)
                        if y0n < _diag_top_clamp - 0.001:
                            logger.info(f"[prescan] fig {fig_num} p{page_idx}: "
                                  f"clamping y0n {y0n:.3f}→{_diag_top_clamp:.3f} "
                                  f"to diagram top (was bleeding above)")
                            y0n = _diag_top_clamp
                        # ── Widen the crop HORIZONTALLY to include the Key /
                        # legend / NOTE text (vision path).  Textract's
                        # LAYOUT_FIGURE box bounds only the DIAGRAM, but a
                        # figure's "Key" legend sits at the page's LEFT margin —
                        # further left than a centred diagram — so a crop taken at
                        # the diagram's x-extent slices off the Key's item numbers
                        # and label starts (B.1, C.3, C.6, C.7).  The y-extent
                        # above already reaches down through the Key; here we union
                        # the x-extent with every Textract layout text block that
                        # falls INSIDE this figure's own vertical band (diagram top
                        # → just above the caption).  Driven by the ACTUAL detected
                        # Key position, so it adapts to any figure in any uploaded
                        # file.  Bounded to the page text column [0.05, 0.95] so it
                        # can never bleed sideways (e.g. into the Techstreet right-
                        # margin watermark), confined to this figure's band so a
                        # stacked neighbour's Key is never pulled in, and it only
                        # ever WIDENS (never shrinks the diagram).  No-op when the
                        # diagram already spans the Key width.
                        _kx0n, _kx1n = x0n, x1n
                        _k_band_top = _fig_diag_y0n - 0.005
                        _k_band_bot = y1n + 0.002
                        for _kb in _tx_layout:
                            _kb_cy = (_kb['bbox'][1] + _kb['bbox'][3]) / 2.0
                            if not (_k_band_top <= _kb_cy <= _k_band_bot):
                                continue
                            _kb_txt = (_kb.get('text') or '').strip()
                            if not _kb_txt:
                                continue
                            # Skip caption blocks (figure/table) — they are
                            # boundaries, not figure body — so their width can't
                            # drag the crop sideways.
                            if (_FIGURE_CAPTION_RE.match(_kb_txt)
                                    or _ANNEX_FIG_CAP_RE.match(_kb_txt)
                                    or _TABLE_CAPTION_RE.match(_kb_txt)):
                                continue
                            _kx0n = min(_kx0n, _kb['bbox'][0])
                            _kx1n = max(_kx1n, _kb['bbox'][2])
                        # Clamp the text-driven widening to the page text column,
                        # but never shrink the diagram's own x-extent.
                        _kx0n = min(x0n, max(_kx0n, 0.05))
                        _kx1n = max(x1n, min(_kx1n, 0.95))
                        if _kx0n < x0n - 0.003 or _kx1n > x1n + 0.003:
                            logger.info(f"[prescan] fig {fig_num} p{page_idx}: "
                                  f"widening x {x0n:.3f}-{x1n:.3f}→"
                                  f"{_kx0n:.3f}-{_kx1n:.3f} to include Key/legend")
                            x0n, x1n = _kx0n, _kx1n
                        clip = fitz.Rect(
                            max(0,  x0n * pw - _FIG_PAD),
                            max(0,  y0n * ph - _FIG_PAD),
                            min(pw, x1n * pw + _FIG_PAD),
                            min(ph, y1n * ph + _FIG_PAD),
                        )

                # ── Fix A: extend clip upward when the circuit diagram sits
                # ABOVE its own caption (caption appears near the clip top).
                # Pattern: Textract/LLM detect the data-table below the
                # caption but miss the raster drawing above it.
                # Triggers only when ALL three conditions hold:
                #   1. Positive figure number (not an unlabeled figure)
                #   2. Caption "Figure N" found at or within 3% of clip top
                #   3. A sufficiently-large raster image exists above the caption
                if fig_num > 0:
                    _fa_cap_re = re.compile(
                        r'Figure\s+' + str(fig_num) + r'\b', re.IGNORECASE)
                    _fa_cap_y0n = None
                    for _fa_blk in page.get_text("blocks"):
                        if len(_fa_blk) < 7 or _fa_blk[6] != 0:
                            continue
                        if _fa_cap_re.search((_fa_blk[4] or "").strip()):
                            _fa_cap_y0n = _fa_blk[1] / ph
                            break
                    if _fa_cap_y0n is not None and _fa_cap_y0n <= y0n + 0.03:
                        # Caption at or above clip top — check for drawing
                        # paths (vector) or raster images above the caption.
                        # get_image_info() misses vector circuit diagrams,
                        # so check get_drawings() for any path in the band
                        # [0.06, caption_y - 0.02] (below header, above caption).
                        _fa_has_content_above = any(
                            0.06 <= d.get('rect', (0, 0, 0, 0))[1] / ph
                                < _fa_cap_y0n - 0.02
                            for d in page.get_drawings()
                        )
                        if _fa_has_content_above:
                            # Upper bound: just past bottom of any already-
                            # captured figure on this page, or header line.
                            _fa_y0n_new = 0.04
                            for _fa_pr in fig_rects_by_page.get(page_idx, []):
                                _fa_pr_y1n = _fa_pr[3] / ph
                                if _fa_pr_y1n < y0n - 0.01:
                                    _fa_y0n_new = max(_fa_y0n_new,
                                                      _fa_pr_y1n + 0.01)
                            # Also advance past table rects above this figure
                            for _fa_tr in table_rects_by_page.get(page_idx, []):
                                _fa_tr_y1n = _fa_tr[3] / ph
                                if _fa_tr_y1n < _fa_cap_y0n - 0.02:
                                    _fa_y0n_new = max(_fa_y0n_new,
                                                      _fa_tr_y1n)
                            # Advance _fa_y0n_new past ALL non-figure text blocks
                            # (table cells, body text, section headings) above the
                            # caption.  Stop at "Key" / numbered key items — those
                            # mark where figure content starts.
                            for _fa_txb in sorted(page.get_text("blocks"),
                                                  key=lambda b: b[1]):
                                if len(_fa_txb) < 7 or _fa_txb[6] != 0:
                                    continue
                                _fa_tb_y0n = _fa_txb[1] / ph
                                _fa_tb_y1n = _fa_txb[3] / ph
                                if _fa_tb_y0n <= _fa_y0n_new + 0.005:
                                    continue
                                if _fa_tb_y0n >= _fa_cap_y0n - 0.02:
                                    break
                                _fa_tb_txt = (_fa_txb[4] or "").strip()
                                if re.match(r'^Key\s*$', _fa_tb_txt, re.IGNORECASE):
                                    break
                                if re.match(r'^\d{1,2}\s{2,}\S', _fa_tb_txt):
                                    break
                                _fa_y0n_new = max(_fa_y0n_new, _fa_tb_y1n)
                            # Lower bound: only cap at a DIFFERENT figure's
                            # caption between this caption and y1n.  A "Key"
                            # block below this caption is this figure's own
                            # Key section — do NOT cap there.
                            _fa_y1n_new = y1n
                            for _fa_kb in sorted(page.get_text("blocks"),
                                                 key=lambda b: b[1]):
                                if len(_fa_kb) < 7 or _fa_kb[6] != 0:
                                    continue
                                _fa_kb_y = _fa_kb[1] / ph
                                _fa_kb_txt = (_fa_kb[4] or "").strip()
                                _fa_other_fig = re.match(
                                    r'^Figure\s+(\d+)\b', _fa_kb_txt,
                                    re.IGNORECASE)
                                if (_fa_other_fig
                                        and int(_fa_other_fig.group(1)) != fig_num
                                        and _fa_kb_y > _fa_cap_y0n + 0.05
                                        and _fa_kb_y < y1n):
                                    _fa_y1n_new = _fa_kb_y - 0.01
                                    break
                            logger.info(f"[prescan] fig {fig_num} p{page_idx}: "
                                  f"diagram above caption "
                                  f"(cap_y={_fa_cap_y0n:.3f}), "
                                  f"extending y0n {y0n:.3f}→{_fa_y0n_new:.3f}"
                                  f" y1n {y1n:.3f}→{_fa_y1n_new:.3f}")
                            y0n, y1n = _fa_y0n_new, _fa_y1n_new
                            clip = fitz.Rect(
                                max(0,  x0n * pw - _FIG_PAD),
                                max(0,  y0n * ph - _FIG_PAD),
                                min(pw, x1n * pw + _FIG_PAD),
                                min(ph, y1n * ph + _FIG_PAD),
                            )

                # Extend clip leftward to capture Key-section labels that sit
                # to the left of the Textract-detected bbox edge.  Textract
                # reports x0 at the edge of the diagram; Key labels (A, 1,
                # 2, ...) are at the page's left text margin, often further
                # left.  Scan text blocks in the figure's Y band and widen
                # clip.x0 if any block starts to the left of the current edge.
                _key_min_x0 = clip.x0
                for _kb in page.get_text("blocks"):
                    if len(_kb) < 7 or _kb[6] != 0:
                        continue
                    _kbx0, _kby0, _kbx1, _kby1 = _kb[0], _kb[1], _kb[2], _kb[3]
                    if _kby1 < clip.y0 - 5 or _kby0 > clip.y1 + 5:
                        continue
                    if _kbx0 < _key_min_x0:
                        _key_min_x0 = _kbx0
                if _key_min_x0 < clip.x0 - 2:
                    _key_new_x0 = max(0, _key_min_x0 - 5)
                    logger.info(f"[prescan] fig {fig_num} p{page_idx}: extending "
                          f"clip.x0 {clip.x0:.1f}→{_key_new_x0:.1f}pt "
                          f"(leftmost text at {_key_min_x0:.1f}pt)")
                    clip = fitz.Rect(_key_new_x0, clip.y0, clip.x1, clip.y1)

                # Change 2: extend clip rightward — symmetric to the x0 fix.
                # Textract can stop short of the right page margin, clipping
                # figures whose content or key labels extend to the right edge.
                _key_max_x1 = clip.x1
                for _kb in page.get_text("blocks"):
                    if len(_kb) < 7 or _kb[6] != 0:
                        continue
                    _kbx0, _kby0, _kbx1, _kby1 = _kb[0], _kb[1], _kb[2], _kb[3]
                    if _kby1 < clip.y0 - 5 or _kby0 > clip.y1 + 5:
                        continue
                    if _kbx1 > _key_max_x1:
                        _key_max_x1 = _kbx1
                if _key_max_x1 > clip.x1 + 2:
                    _key_new_x1 = min(pw, _key_max_x1 + 5)
                    logger.info(f"[prescan] fig {fig_num} p{page_idx}: extending "
                          f"clip.x1 {clip.x1:.1f}→{_key_new_x1:.1f}pt "
                          f"(rightmost text at {_key_max_x1:.1f}pt)")
                    clip = fitz.Rect(clip.x0, clip.y0, _key_new_x1, clip.y1)

                _new_area = clip.width * clip.height
                _should_capture = (fig_num not in figures or
                                   _new_area > _fig_clip_area.get(fig_num, 0) * 2.0)
                if _should_capture:
                    if fig_num in figures:
                        logger.info(f"[prescan] figure {fig_num} page={page_idx}: "
                              f"replacing smaller capture "
                              f"({_fig_clip_area.get(fig_num,0):.0f}px² → {_new_area:.0f}px²)")
                    pix = page.get_pixmap(clip=clip, dpi=200)
                    figures[fig_num] = pix.tobytes("png")
                    _fig_clip_area[fig_num] = _new_area
                    fig_captions[fig_num] = fig_cap or (
                        f'Figure {_fig_ref_display(fig_num)}'
                        if _fig_ref_display(fig_num) else 'Figure')
                    figures_first_page[fig_num] = page_idx
                    fig_rects_by_page.setdefault(page_idx, []).append(
                        (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1))
                    )
                    logger.info(f"[vision-path] figure {fig_num} page={page_idx}: {clip.width:.0f}x{clip.height:.0f}")

    else:
        # ── Normal path: region detection ported from img_ext/extImg.py ─────
        # Each page is classified into image/table regions via seed-growing
        # (rasters + inline images + clustered vector drawings), then the
        # tight bbox is cropped and stored.  Formulas are handled by a
        # separate text-block scan — extImg does not detect them.
        #
        # Cross-page figure support: when a drawing cluster on page N sits
        # BELOW a named figure's caption, it belongs to the figure whose
        # caption appears at the top of page N+1 (e.g. Figure 7 in ISO 11608-4).
        # _cross_page_pending holds the PNG fragment keyed by page_idx so the
        # next labeled figure can stitch it above its own image.
        _cross_page_pending = {}   # page_idx → png_bytes of below-caption fragment
        for page_idx in range(len(doc)):
            page   = doc[page_idx]
            page_w = page.rect.width

            for reg in _extract_page_regions(page):
                bbox = fitz.Rect(reg["bbox"])
                if bbox.width <= 5 or bbox.height <= 5:
                    continue
                cap = (reg.get("caption") or "").strip()

                if reg["type"] == "image":
                    mf = _FIGURE_CAPTION_RE.match(cap)
                    if mf:
                        fig_num = int(mf.group(1))
                    else:
                        # Fallback: use any nearby text containing "Figure", or
                        # assign a unique negative key so the image is not dropped
                        fig_ref_m = re.search(r'\bFigure\s+(\d+)\b', cap, re.IGNORECASE)
                        if fig_ref_m:
                            fig_num = int(fig_ref_m.group(1))
                        else:
                            # Use a unique negative key to avoid colliding with
                            # properly numbered figures
                            fig_num = -(len(figures) + 1)
                            cap = cap or f'Figure (unlabeled, page {page_idx + 1})'
                    if fig_num in figures:
                        continue

                    # Use the FULL figure bbox for text suppression so that content
                    # below the caption (duplicate tables, continuation text) is also
                    # suppressed. Figure caption rows still appear in the DOCX because
                    # _extract_body's Change-4 exception always passes _FIGURE_CAPTION_RE
                    # matches through regardless of suppress rects.
                    caption_y0 = reg.get("caption_y0")
                    suppress_rect = (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
                    fig_rects_by_page.setdefault(page_idx, []).append(suppress_rect)

                    # Crop the PNG ABOVE the caption line so the caption text is not
                    # baked into the image (it is already used as the row description).
                    if caption_y0 is not None and caption_y0 > bbox.y0 + 10:
                        crop_bbox = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, caption_y0)
                    else:
                        crop_bbox = bbox

                    # Cross-page fragment detection: when this labeled figure's bbox
                    # extends significantly below caption_y0, the below-caption
                    # content belongs to the NEXT page's figure (cross-page layout).
                    # Example: Figure 7's circuit sits below Figure 6's caption on
                    # page N, while Figure 7's own caption is at the top of page N+1.
                    # Capture the below-caption region now, skip the caption line
                    # itself (+30 px offset), and store for stitching by next figure.
                    if (fig_num > 0
                            and caption_y0 is not None
                            and caption_y0 > bbox.y0 + 10
                            and bbox.y1 > caption_y0 + 100):
                        try:
                            _frag_bbox = fitz.Rect(
                                bbox.x0, caption_y0 + 30, bbox.x1, bbox.y1)
                            _frag_pix = page.get_pixmap(clip=_frag_bbox, dpi=200)
                            _cross_page_pending[page_idx] = _frag_pix.tobytes("png")
                            logger.info(
                                "[prescan] cross-page fragment stored page=%s y=%.0f-%.0f "
                                "(below caption of fig=%s)",
                                page_idx,
                                caption_y0 + 30,
                                bbox.y1,
                                fig_num,
                            )
                        except Exception as _exc:
                            logger.info(
                                "[prescan] cross-page frag capture failed page=%s: %s",
                                page_idx,
                                _exc,
                            )

                    try:
                        pix = page.get_pixmap(clip=crop_bbox, dpi=200)
                        png_bytes = pix.tobytes("png")
                    except Exception as exc:
                        logger.info(
                            "[prescan] figure pixmap failed page=%s bbox=%s cap=%r: %s",
                            page_idx,
                            tuple(bbox),
                            cap[:60],
                            exc,
                        )
                        continue

                    # Stitch cross-page fragment from the previous page if present.
                    # The fragment is the circuit on page N; this figure's PNG is
                    # the key-labels + caption area on page N+1.  Prepending gives
                    # the complete cross-page figure as one combined image.
                    _prev_frag = _cross_page_pending.pop(page_idx - 1, None)

                    # When stitching: if a drawing cluster exists inside the current
                    # crop AND another figure's caption appears below that cluster on
                    # this same page, the cluster belongs to the next figure — trim
                    # the current capture to caption-area only so the next figure
                    # can claim the full cluster via the orphaned-caption fallback.
                    if _prev_frag is not None:
                        _draws_in_crop = [
                            fitz.Rect(d["rect"]) for d in page.get_drawings()
                            if (fitz.Rect(d["rect"]).y0 > crop_bbox.y0 + 20
                                and fitz.Rect(d["rect"]).y1 <= crop_bbox.y1
                                and fitz.Rect(d["rect"]).height > 10)
                        ]
                        if _draws_in_crop:
                            _cluster_bot = max(r.y1 for r in _draws_in_crop)
                            _has_later_cap = any(
                                _FIGURE_CAPTION_RE.match((blk[4] or "").strip()[:50])
                                and blk[1] > _cluster_bot + 20
                                for blk in page.get_text("blocks")
                            )
                            if _has_later_cap:
                                _first_draw_y = min(r.y0 for r in _draws_in_crop)
                                _cap_only = fitz.Rect(
                                    crop_bbox.x0, crop_bbox.y0,
                                    crop_bbox.x1, _first_draw_y - 5)
                                if _cap_only.height > 15:
                                    try:
                                        _cpix = page.get_pixmap(clip=_cap_only, dpi=200)
                                        png_bytes = _cpix.tobytes("png")
                                        logger.info(
                                            "[prescan] stitched fig=%s: trimmed to caption-only "
                                            "y=%.0f-%.0f (drawing cluster released for next figure)",
                                            fig_num,
                                            crop_bbox.y0,
                                            _first_draw_y - 5,
                                        )
                                    except Exception as _exc:
                                        logger.info("[prescan] stitch-trim failed: %s", _exc)

                    if _prev_frag is not None:
                        try:
                            _img_prev = PILImage.open(io.BytesIO(_prev_frag)).convert('RGB')
                            _img_curr = PILImage.open(io.BytesIO(png_bytes)).convert('RGB')
                            _w = max(_img_prev.width, _img_curr.width)
                            _stitched = PILImage.new(
                                'RGB', (_w, _img_prev.height + _img_curr.height),
                                (255, 255, 255))
                            _stitched.paste(_img_prev, (0, 0))
                            _stitched.paste(_img_curr, (0, _img_prev.height))
                            _buf = io.BytesIO()
                            _stitched.save(_buf, 'PNG')
                            png_bytes = _buf.getvalue()
                            logger.info(
                                "[prescan] cross-page stitch fig=%s: prepended fragment from page %s",
                                fig_num,
                                page_idx - 1,
                            )
                        except Exception as _exc:
                            logger.info(
                                "[prescan] cross-page stitch failed fig=%s: %s", fig_num, _exc
                            )

                    figures[fig_num] = png_bytes
                    if fig_num not in figures_first_page:
                        figures_first_page[fig_num] = page_idx
                    fig_captions[fig_num] = cap

                elif reg["type"] == "table":
                    # Always record the rect for text suppression — even when the
                    # caption doesn't match `Table X.Y —`. This prevents the cell
                    # contents (cross-reference matrices, untitled measurement
                    # tables, "Table 14" without a dash, etc.) from leaking into
                    # the description column as multi-line numeric blocks.
                    table_rects_by_page.setdefault(page_idx, []).append(
                        (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
                    )
                    # Assign a referenceable key — prefer the matched caption;
                    # fall back to a unique key so the table image is never dropped.
                    mt = _TABLE_CAPTION_RE.match(cap)
                    if mt:
                        tbl_ref = mt.group(1)
                    else:
                        # Use page+index as a unique key for unlabeled tables
                        tbl_ref = f'unlabeled_{page_idx}_{len(tables_pages)}'
                        if not cap:
                            cap = f'Table (unlabeled, page {page_idx + 1})'
                        unlabeled_tables_by_page.setdefault(page_idx, []).append(tbl_ref)
                    # get_pixmap can raise "Invalid bandwriter header dimensions"
                    # on degenerate regions (very thin or out-of-page bboxes).
                    # The rect was already recorded above for text suppression;
                    # skipping the image rather than crashing keeps the
                    # downstream content extraction working.
                    try:
                        pix = page.get_pixmap(clip=bbox, dpi=200)
                        png_bytes = pix.tobytes("png")
                    except Exception as exc:
                        logger.info(
                            "[prescan] table pixmap failed page=%s bbox=%s cap=%r: %s",
                            page_idx,
                            tuple(bbox),
                            cap[:60],
                            exc,
                        )
                        continue
                    tables_pages.setdefault(tbl_ref, []).append(png_bytes)
                    if tbl_ref not in tables_first_page:
                        tables_first_page[tbl_ref] = page_idx
                    if tbl_ref not in tbl_captions:
                        tbl_captions[tbl_ref] = cap

            # ── Numbered block formulas (not covered by extImg) ──────────
            for blk in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, _, btype = blk
                if btype != 0 or not text.strip():
                    continue
                if _WATERMARK_RE.search(text[:120]):
                    continue
                stripped = text.strip()
                fm = _FORMULA_TRAIL_RE.search(stripped)
                if fm and _MATH_CHARS_RE.search(stripped):
                    fn = int(fm.group(1))
                    if fn not in formulas:
                        clip = fitz.Rect(max(x0 - 20, 0), max(y0 - 5, 0),
                                         min(x1 + 20, page_w), y1 + 5)
                        if clip.height > 5:
                            pix = page.get_pixmap(clip=clip, dpi=200)
                            formulas[fn] = pix.tobytes("png")

    # ── Cross-page figure stitching ─────────────────────────────────────────
    # Capture a figure whose diagram sits at the BOTTOM of page P-1 with its
    # "Figure N —" caption (and Key) at the TOP of page P.  Such a figure is
    # otherwise lost: the page-(P-1) diagram was deferred above, and the page-P
    # caption binds to the next figure's diagram below it.  We stitch
    # [P-1 diagram → page bottom] + [P top → just above the caption] into one
    # image and label it Figure N.
    #
    # Trigger requires ALL of: a "Figure N" caption near the top of page P
    # (y0 < 0.32) with figure content (a "Key"/fragments) above it, AND page
    # P-1 carrying a bottom LAYOUT_FIGURE (y1 > 0.62) with no caption of its
    # own.  11608-1 has no such layout, so this pass cannot change it.
    if llm_pages:
        try:
            import io as _cp_io
            from PIL import Image as _CpImage
        except Exception:
            _CpImage = None
        for _cp_pidx in sorted(_textract_cache.keys()):
            if _CpImage is None or (_cp_pidx - 1) not in _textract_cache:
                continue
            _cp_lay  = _page_layout.get(_cp_pidx, {}).get('layout', [])
            _cp_figs = _page_layout.get(_cp_pidx, {}).get('figures', [])
            # (a) figure caption near the top of page P
            _cp_cap = None
            for _cl in sorted(_cp_lay, key=lambda b: b['bbox'][1]):
                _clt = (_cl.get('text') or '').strip()
                _clm = _FIGURE_CAPTION_RE.match(_clt) or _ANNEX_FIG_CAP_RE.match(_clt)
                if _clm and _cl['bbox'][1] < 0.32:
                    _cp_cap = (_cl['bbox'][1], _clm.group(1))
                    break
            if _cp_cap is None:
                continue
            _cap_y0n, _cap_ref = _cp_cap
            _cp_fnum = _parse_fig_ref(_cap_ref)
            if _cp_fnum is None:
                continue
            # (b) figure content ABOVE the caption on page P (Key or fragments)
            _content_above = any(
                _c['bbox'][1] < _cap_y0n - 0.005
                and (_c.get('text') or '').strip().lower().startswith('key')
                for _c in _cp_lay
            ) or any(0.06 < _f[1] < _cap_y0n - 0.005 for _f in _cp_figs)
            if not _content_above:
                continue
            # (c) previous page: a bottom diagram (y1 > 0.62) that has NO figure
            # caption BELOW it on that page — i.e. its caption is not on its own
            # page (it's the top caption on page P).  A caption ABOVE it (e.g.
            # the previous figure's caption) does NOT disqualify it.
            _prev_lay  = _page_layout.get(_cp_pidx - 1, {}).get('layout', [])
            _prev_figs = _page_layout.get(_cp_pidx - 1, {}).get('figures', [])
            _prev_caps_y = [
                _pl['bbox'][1] for _pl in _prev_lay
                if (_FIGURE_CAPTION_RE.match((_pl.get('text') or '').strip())
                    or _ANNEX_FIG_CAP_RE.match((_pl.get('text') or '').strip()))]
            _orphans = [
                f for f in _prev_figs
                if f[3] > 0.62
                and not any(_cy > f[3] - 0.01 for _cy in _prev_caps_y)]
            if not _orphans:
                continue
            _od = min(_orphans, key=lambda f: f[1])  # topmost bottom-figure
            try:
                _pp   = doc[_cp_pidx - 1]
                _cp   = doc[_cp_pidx]
                _pp_w, _pp_h = _pp.rect.width, _pp.rect.height
                _cp_w, _cp_h = _cp.rect.width, _cp.rect.height
                _X0, _X1 = 0.05, 0.92   # trim margins + right-edge watermark
                # Start the previous-page crop BELOW the last figure caption on
                # that page.  Textract sometimes merges the previous figure and
                # this cross-page diagram into one big region; the cross-page
                # diagram is whatever sits below the previous figure's caption,
                # so cropping from there excludes the previous figure (e.g. so
                # Figure 7's crop does not redundantly include Figure 6 above).
                _cp_crop_top = max([_od[1] - 0.02]
                                   + [_cy + 0.02 for _cy in _prev_caps_y])
                _r1 = fitz.Rect(_X0*_pp_w, max(0, _cp_crop_top*_pp_h),
                                _X1*_pp_w, 0.945*_pp_h)
                _r2 = fitz.Rect(_X0*_cp_w, 0.06*_cp_h,
                                _X1*_cp_w, max(0.07*_cp_h, _cap_y0n*_cp_h - 4))
                _im1 = _CpImage.open(_cp_io.BytesIO(
                    _pp.get_pixmap(clip=_r1, dpi=200).tobytes('png')))
                _im2 = _CpImage.open(_cp_io.BytesIO(
                    _cp.get_pixmap(clip=_r2, dpi=200).tobytes('png')))
                _W = max(_im1.width, _im2.width)
                _cv = _CpImage.new('RGB', (_W, _im1.height + _im2.height), 'white')
                _cv.paste(_im1, (0, 0)); _cv.paste(_im2, (0, _im1.height))
                _ob = _cp_io.BytesIO(); _cv.save(_ob, format='PNG')
                figures[_cp_fnum] = _ob.getvalue()
                fig_captions[_cp_fnum] = f'Figure {_cap_ref}'
                figures_first_page[_cp_fnum] = _cp_pidx - 1
                logger.info(f"[prescan] cross-page figure {_cp_fnum}: stitched diagram "
                      f"p{_cp_pidx-1} (y{_od[1]:.2f}→bottom) + Key/top p{_cp_pidx} "
                      f"(top→{_cap_y0n:.2f})")
            except Exception as _cpe:
                logger.info(f"[prescan] cross-page figure {_cp_fnum} failed: {_cpe}")

    # ── LLM bbox fallback: capture tables/figures that Textract missed ──────
    # The LLM JSON includes bbox_norm for items it detected. Use those bboxes
    # to crop anything with a recognised caption that Textract didn't find.
    if llm_pages and _VISION_USE_LLM_BOXES:
        for pd in llm_pages:
            if not pd:
                continue
            _pidx = pd["page_idx"]
            _pg   = doc[_pidx]
            _pw, _ph = _pg.rect.width, _pg.rect.height

            for tbl_entry in pd.get("tables", []):
                _cap = (tbl_entry.get("caption") or "").strip()
                _mt  = _TABLE_CAPTION_RE.match(_cap)
                if not _mt:
                    continue
                _ref = _mt.group(1)
                if _ref in tables_pages:
                    continue  # already captured by Textract
                _bbox = tbl_entry.get("bbox_norm")
                if not _bbox or len(_bbox) != 4:
                    continue
                _x0n, _y0n, _x1n, _y1n = _bbox
                # Cap bottom at next item on same page.
                # Textract items + other LLM-detected items on the same page.
                # LLM bboxes are already tight — no _BOTTOM_PAD to avoid
                # capturing text that immediately follows the table.
                _lf_next_y0_pts = _ph - _FOOTER_PTS
                for _lf_nb in (_page_layout.get(_pidx, {}).get('tables', []) +
                               _page_layout.get(_pidx, {}).get('figures', [])):
                    _lf_nb_y0_pts = _lf_nb[1] * _ph
                    if _lf_nb_y0_pts > _y1n * _ph + 2:
                        _lf_next_y0_pts = min(_lf_next_y0_pts, _lf_nb_y0_pts)
                for _lf_llm_nb in pd.get("tables", []) + pd.get("figures", []):
                    _lf_llm_nb_bbox = _lf_llm_nb.get("bbox_norm") or []
                    if len(_lf_llm_nb_bbox) == 4 and _lf_llm_nb_bbox[1] > _y1n + 0.01:
                        _lf_next_y0_pts = min(_lf_next_y0_pts, _lf_llm_nb_bbox[1] * _ph)
                # Scan PDF drawings for the last major horizontal line — table
                # grid borders are wide (>80pts); inline marks and checkmarks
                # are narrow and get skipped.
                #
                # This LLM-fallback path fires only when Textract missed the
                # table entirely (e.g. Table H.1 at 150 DPI).  In that case the
                # LLM bbox can UNDERSHOOT the real table bottom (H.1 stopped at
                # "Basic safety", dropping the last "Human factors" row).  So we
                # scan BELOW the LLM y1n as well, bounded by the next detected
                # item, the first Textract layout text/heading below the table,
                # and the footer.  The deepest grid line is the true bottom
                # border, which recovers the dropped rows.  When the LLM bbox is
                # already correct the scan simply confirms it (grid lines only
                # exist within the table, so it never runs into body text).
                _lf_scan_cap = min(_lf_next_y0_pts, _ph - _FOOTER_PTS)
                for _lf_lb in _page_layout.get(_pidx, {}).get('layout', []):
                    if _lf_lb['type'] in ('LAYOUT_TEXT', 'LAYOUT_SECTION_HEADER',
                                          'LAYOUT_TITLE', 'LAYOUT_HEADER',
                                          'LAYOUT_FOOTER'):
                        _lf_lb_y0p = _lf_lb['bbox'][1] * _ph
                        if _lf_lb_y0p > _y1n * _ph + 2:
                            _lf_scan_cap = min(_lf_scan_cap, _lf_lb_y0p)
                _lf_draw_bottom = None
                for _lf_path_d in _pg.get_drawings():
                    _lf_rd = _lf_path_d['rect']
                    if (_lf_rd.height < 2 and _lf_rd.width > 80 and
                            _y0n * _ph - 5 <= _lf_rd.y0 <= _lf_scan_cap + 2):
                        if _lf_draw_bottom is None or _lf_rd.y0 > _lf_draw_bottom:
                            _lf_draw_bottom = _lf_rd.y0
                if _lf_draw_bottom is not None and _lf_draw_bottom > _y1n * _ph:
                    # LLM bbox undershot — extend down to the true bottom border
                    logger.info(f"[prescan] LLM-fallback table {_ref} p{_pidx}: "
                          f"LLM y1={_y1n * _ph:.1f}pt undershoots grid bottom "
                          f"{_lf_draw_bottom:.1f}pt — extending")
                    _lf_y1_tight = _lf_draw_bottom + 4
                elif _lf_draw_bottom is not None:
                    # LLM bbox OK — keep tight (avoid trailing footnote text)
                    _lf_y1_tight = min(_lf_draw_bottom + 4, _y1n * _ph)
                else:
                    _lf_y1_tight = _y1n * _ph
                _lf_clip_y1 = min(_ph - _FOOTER_PTS,
                                  _lf_y1_tight,
                                  _lf_scan_cap - 3)
                _clip = fitz.Rect(
                    max(0,    _x0n * _pw - 12),
                    max(0,    _y0n * _ph - _SUB_HEADER_PTS),
                    min(_pw,  _x1n * _pw + 12),
                    _lf_clip_y1,
                )
                if _clip.width <= 5 or _clip.height <= 5:
                    continue
                try:
                    _pix = _pg.get_pixmap(clip=_clip, dpi=200)
                    _lf_png, _lf_was_rot = _correct_landscape_png(_pix.tobytes("png"), _pg)
                    if _lf_was_rot:
                        logger.info(f"[vision-path] LLM-fallback table {_ref} page={_pidx}: rotated 90 CW (landscape)")
                    tables_pages[_ref] = [_lf_png]
                    if _ref not in tables_first_page:
                        tables_first_page[_ref] = _pidx
                    if _ref not in tbl_captions:
                        tbl_captions[_ref] = _cap
                    logger.info(f"[vision-path] LLM-fallback table {_ref} page={_pidx}: {_clip.width:.0f}x{_clip.height:.0f}")
                except Exception as _exc:
                    logger.info(f"[vision-path] LLM-fallback table {_ref} page={_pidx} failed: {_exc}")

            for img_entry in pd.get("images", []):
                _cap = (img_entry.get("caption") or "").strip()
                _mf  = _FIGURE_CAPTION_RE.match(_cap)
                # Annex figure caption ("Figure H.1 — …", "Figure C.3 — …").
                # Without this the numeric-only regex skipped annex figures, so
                # a figure Textract missed (e.g. 11608-1 Fig H.1 — page 68
                # returns 0 Textract figures) was never recovered even though
                # the LLM detected it with a valid bbox.  Match annex refs too
                # and key by the negative annex ref via _parse_fig_ref.
                _mf_ann = _ANNEX_FIG_CAP_RE.match(_cap) if not _mf else None
                if not _mf and not _mf_ann:
                    continue
                _fnum = int(_mf.group(1)) if _mf else _parse_fig_ref(_mf_ann.group(1))
                # Caption ref string ("8" or "H.1") for building fitz caption
                # patterns below — the numeric key (-72001) must never be used
                # in a "Figure N" regex.
                _fref_str = _mf.group(1) if _mf else _mf_ann.group(1)
                if _fnum is None or _fnum in figures:
                    continue  # already captured by Textract
                _bbox = img_entry.get("bbox_norm")
                if not _bbox or len(_bbox) != 4:
                    continue
                _x0n, _y0n, _x1n, _y1n = _bbox

                # Fix: if this LLM bbox overlaps heavily with an already-captured
                # figure rect on the same page, the LLM gave a wrong/duplicate bbox.
                # Use the figure caption's actual y-position in the PDF text to
                # find the correct y0 (e.g. Figure 8 on page 37 gets same bbox
                # as Figure 7 from LLM — caption "Figure 8" sits at y≈0.663 in
                # the PDF text, so we anchor y0 just above that).
                _existing_rects = fig_rects_by_page.get(_pidx, [])
                for _er in _existing_rects:
                    _er_x0n = _er[0] / _pw
                    _er_y0n = _er[1] / _ph
                    _er_x1n = _er[2] / _pw
                    _er_y1n = _er[3] / _ph
                    _xovl_fix = min(_x1n, _er_x1n) - max(_x0n, _er_x0n)
                    _yovl_fix = min(_y1n, _er_y1n) - max(_y0n, _er_y0n)
                    _area_fix = (_x1n - _x0n) * (_y1n - _y0n)
                    if _area_fix > 0 and _xovl_fix > 0 and _yovl_fix > 0:
                        _ovl_frac = (_xovl_fix * _yovl_fix) / _area_fix
                        if _ovl_frac >= 0.7:
                            # Heavy overlap — find caption text position in PDF
                            _found_y = None
                            _cap_pat = re.compile(
                                r'Figure\s+' + re.escape(_fref_str) + r'\b', re.IGNORECASE)
                            for _blk in _pg.get_text("blocks"):
                                _btxt = (_blk[4] or "").strip() if len(_blk) > 4 else ""
                                if _cap_pat.search(_btxt):
                                    _found_y = _blk[1] / _ph  # top of block, normalised
                                    break
                            if _found_y is not None and _found_y > _y0n + 0.05:
                                _new_y0 = max(0.0, _found_y - 0.08)
                                logger.info(f"[prescan] LLM-fallback fig {_fnum} p{_pidx}: "
                                      f"bbox collision ({_ovl_frac:.2f}), "
                                      f"anchoring y0n {_y0n:.3f}→{_new_y0:.3f} "
                                      f"from caption at {_found_y:.3f}")
                                _y0n = _new_y0
                            break  # only correct once

                # Clip the bottom of this figure's bbox against the top of any
                # later figure on the same page (higher number, lower y0).
                # Without this, a large LLM bbox for Figure N can extend into
                # Figure N+1's region, causing N+1's circuit to appear inside N.
                for _other in pd.get("images", []):
                    _oc = (_other.get("caption") or "").strip()
                    _om = _FIGURE_CAPTION_RE.match(_oc) or _ANNEX_FIG_CAP_RE.match(_oc)
                    if not _om:
                        continue
                    _ob = _other.get("bbox_norm")
                    if not _ob or len(_ob) != 4:
                        continue
                    # Geometry-ordered clip (works for numeric AND annex refs,
                    # where numeric<->annex number comparison is meaningless):
                    # any OTHER figure whose bbox starts below our top and above
                    # our intended bottom clips us there, so figure N never
                    # bleeds into the figure stacked below it (e.g. C.2→C.3).
                    if _ob[1] > _y0n + 0.02 and _ob[1] < _y1n:
                        _y1n = _ob[1]
                        logger.info(f"[prescan] LLM-fallback fig {_fref_str} p{_pidx}: "
                              f"clipped y1n to {_y1n:.3f} ({_om.group(1)} starts there)")

                # ── Fix B-up: extend y0n upward when key-labels or a
                # circuit drawing lie between the previous figure's clip
                # bottom and this figure's LLM bbox start.
                # Guard: fires only when a previous figure exists on this
                # page AND the gap contains text but no section heading.
                _fb_prev_y1n = 0.04
                for _fb_pr in fig_rects_by_page.get(_pidx, []):
                    _fb_pr_y1n = _fb_pr[3] / _ph
                    if _fb_pr_y1n < _y0n - 0.01:
                        _fb_prev_y1n = max(_fb_prev_y1n, _fb_pr_y1n)
                if _fb_prev_y1n > 0.04 and _fb_prev_y1n < _y0n - 0.015:
                    _fb_gap_blks = [
                        _gb for _gb in _pg.get_text("blocks")
                        if (len(_gb) >= 7 and _gb[6] == 0
                            and _gb[1] / _ph >= _fb_prev_y1n
                            and _gb[3] / _ph <= _y0n
                            and (_gb[4] or "").strip())
                    ]
                    _fb_gap_has_hdr = any(
                        re.match(r'^\d+(\.\d+)+[\s\n]', (_gb[4] or "").strip())
                        for _gb in _fb_gap_blks
                    )
                    if _fb_gap_blks and not _fb_gap_has_hdr:
                        logger.info(f"[prescan] LLM-fallback fig {_fnum} p{_pidx}: "
                              f"gap content, extending y0n "
                              f"{_y0n:.3f}→{_fb_prev_y1n:.3f}")
                        _y0n = _fb_prev_y1n

                # ── Fix B-cap: cap y1n at the bottom of this figure's
                # own caption line so the following section heading is
                # not baked into the figure's PNG.
                _fb_cap_re = re.compile(
                    r'Figure\s+' + re.escape(_fref_str) + r'\b', re.IGNORECASE)
                for _fb_cb in _pg.get_text("blocks"):
                    if len(_fb_cb) < 7 or _fb_cb[6] != 0:
                        continue
                    if not _fb_cap_re.search((_fb_cb[4] or "").strip()):
                        continue
                    _fb_cap_y0n = _fb_cb[1] / _ph  # top of matched block
                    _fb_cap_y1n = _fb_cb[3] / _ph  # bottom of matched block
                    # Only a caption sitting BELOW the diagram top may cap the
                    # crop.  An in-text "Figure X" REFERENCE above the diagram
                    # (e.g. the annex "H.1 General" paragraph that mentions
                    # "Figure H.1") must NOT shrink the crop — that produced a
                    # 15px sliver for Fig H.1.  Require the block to start below
                    # the diagram top before it can act as the bottom caption.
                    if _fb_cap_y0n <= _y0n + 0.02:
                        continue
                    if _fb_cap_y1n < _y1n - 0.01:
                        logger.info(f"[prescan] LLM-fallback fig {_fref_str} "
                              f"p{_pidx}: capping y1n "
                              f"{_y1n:.3f}→{_fb_cap_y1n:.3f} (caption)")
                        _y1n = _fb_cap_y1n
                    break

                _PAD2 = 6
                _clip = fitz.Rect(
                    max(0,   _x0n * _pw - _PAD2),
                    max(0,   _y0n * _ph - _PAD2),
                    min(_pw, _x1n * _pw + _PAD2),
                    min(_ph, _y1n * _ph + _PAD2),
                )
                if _clip.width <= 5 or _clip.height <= 5:
                    continue
                try:
                    _pix = _pg.get_pixmap(clip=_clip, dpi=200)
                    figures[_fnum] = _pix.tobytes("png")
                    fig_captions[_fnum] = _cap
                    figures_first_page[_fnum] = _pidx
                    fig_rects_by_page.setdefault(_pidx, []).append(
                        (float(_clip.x0), float(_clip.y0), float(_clip.x1), float(_clip.y1))
                    )
                    logger.info(f"[vision-path] LLM-fallback figure {_fnum} page={_pidx}: {_clip.width:.0f}x{_clip.height:.0f}")
                except Exception as _exc:
                    logger.info(f"[vision-path] LLM-fallback figure {_fnum} page={_pidx} failed: {_exc}")

    # Fix B: Keep per-page images as a list instead of vstacking into one tall
    # image.  Vstacking then pixel-slicing cut across table rows; showing each
    # PDF-page slice separately keeps every row intact.
    tables = tables_pages   # dict[ref → list[bytes]], one entry per captured page
    doc.close()

    # ── Unnumbered formulas (normal path only; vision path relies on LLM
    #    bbox_norm entries in 'images'/'tables', not supported here yet) ─────
    unnumbered = []
    if toc and not llm_pages:
        try:
            unnumbered = find_unnumbered_formulas(
                pdf_path, toc=toc,
                toc_start_idx=toc_start_idx, toc_end_idx=toc_end_idx,
                ws=ws, llm=llm,
            )
        except Exception as exc:
            logger.info("[prescan] unnumbered-formula detection failed: %s", exc)
            logger.info("[prescan] %s", traceback.format_exc())
            unnumbered = []

    # Combine first-capture pages for tables and figures into one dict
    media_first_page = {f'table:{r}': p for r, p in tables_first_page.items()}
    media_first_page.update({f'figure:{n}': p for n, p in figures_first_page.items()})

    # ── Persist to the run's media directory when a workspace is provided ────
    media_dir = _session_media_dir(ws)
    if media_dir is None:
        if toc is not None:
            return figures, tables, formulas, unnumbered, table_rects_by_page, fig_rects_by_page, unlabeled_tables_by_page, media_first_page
        return figures, tables, formulas

    fig_paths, tbl_paths, fml_paths = {}, {}, {}

    for fn, png in figures.items():
        cap   = fig_captions.get(fn, f'Figure {fn}')
        fname = _sanitize_filename(cap) + '.png'
        fig_paths[fn] = _write_media(media_dir, fname, png, ws=ws)

    for ref, pages in tables.items():
        cap        = tbl_captions.get(ref, f'Table {ref}')
        page_paths = []
        for i, png in enumerate(pages):
            suffix = f'_p{i + 1}' if len(pages) > 1 else ''
            fname  = _sanitize_filename(cap) + suffix + '.png'
            page_paths.append(_write_media(media_dir, fname, png, ws=ws))
        tbl_paths[ref] = page_paths   # list of file paths, one per page

    for fn, png in formulas.items():
        fname = f'Formula_{fn}.png'
        fml_paths[fn] = _write_media(media_dir, fname, png, ws=ws)

    logger.info(
        "[prescan] saved figs=%d tbls=%d fmls=%d unnum=%d -> %s",
        len(fig_paths),
        len(tbl_paths),
        len(fml_paths),
        len(unnumbered),
        media_dir,
    )
    if toc is not None:
        return fig_paths, tbl_paths, fml_paths, unnumbered, table_rects_by_page, fig_rects_by_page, unlabeled_tables_by_page, media_first_page
    return fig_paths, tbl_paths, fml_paths
