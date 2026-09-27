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
    _FIGURE_CAPTION_RE,
    _FORMULA_TRAIL_RE,
    _MATH_CHARS_RE,
    _TABLE_CAPTION_RE,
    _WATERMARK_RE,
)
from .textract import _call_textract_page, _find_caption_above, _find_caption_below

logger = logging.getLogger(__name__)


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
        _BOTTOM_PAD     = 90   # increased from 30 — ensures Textract under-detected y1n still reaches last rows
        _last_tbl    = {}  # tbl_ref -> {'x0n', 'x1n', 'page_idx'} for continuation stitching
        _tbl_x_union = {}  # tbl_ref -> {'x0n': min, 'x1n': max} — widest extent seen

        # `sorted` rather than insertion order: the negative figure keys and the
        # unlabeled-table keys are derived from how many have been seen so far, so the
        # output would otherwise depend on the order llm_pages happened to arrive in.
        for page_idx in sorted(_textract_cache.keys()):
            tx   = _textract_cache[page_idx]
            page = doc[page_idx]
            pw   = page.rect.width
            ph   = page.rect.height

            for tb_norm in tx['tables']:
                x0n, y0n, x1n, y1n = tb_norm

                tbl_ref = _find_caption_above(llm_texts_local, page_idx, y0n, _TABLE_CAPTION_RE)
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
                    )
                    if _fig_nearby is not None:
                        logger.info(
                            "[prescan] page %s: skipping TABLE stitch — figure %s caption nearby",
                            page_idx,
                            _fig_nearby,
                        )
                        continue

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
                _RIGHT_MARGIN_N = 0.88   # normalised right boundary (~right text margin)
                _LEFT_MARGIN_N  = 0.05   # normalised left boundary  (~left text margin)
                if x0n < 0.30 and x1n < 0.85:
                    x0n = min(x0n, _LEFT_MARGIN_N)
                    x1n = max(x1n, _RIGHT_MARGIN_N)
                    logger.info(
                        "[prescan] table %s page=%s: expanded x to full-page width (%.2f-%.2f)",
                        tbl_ref,
                        page_idx,
                        x0n,
                        x1n,
                    )

                # Union-bbox: ensure every continuation page uses the widest
                # x-extent seen so far for this table (Fix A — original part).
                if tbl_ref not in _tbl_x_union:
                    _tbl_x_union[tbl_ref] = {'x0n': x0n, 'x1n': x1n}
                else:
                    x0n = min(x0n, _tbl_x_union[tbl_ref]['x0n'])
                    x1n = max(x1n, _tbl_x_union[tbl_ref]['x1n'])
                    _tbl_x_union[tbl_ref]['x0n'] = x0n
                    _tbl_x_union[tbl_ref]['x1n'] = x1n

                # Use a larger upward extension for continuation pages so that the
                # "Table X (continued)" header row and column labels are not clipped.
                is_continuation = tbl_ref in tables_pages
                top_pad = 55 if is_continuation else _SUB_HEADER_PTS
                clip = fitz.Rect(
                    max(0,   x0n * pw),
                    max(0,   y0n * ph - top_pad),
                    min(pw,  x1n * pw),
                    min(ph - _FOOTER_PTS, y1n * ph + _BOTTOM_PAD),
                )
                if clip.width <= 5 or clip.height <= 5:
                    continue

                pix = page.get_pixmap(clip=clip, dpi=200)
                tables_pages.setdefault(tbl_ref, []).append(pix.tobytes("png"))
                if tbl_ref not in tables_first_page:
                    tables_first_page[tbl_ref] = page_idx
                if tbl_ref not in tbl_captions:
                    tbl_captions[tbl_ref] = f'Table {tbl_ref}'
                table_rects_by_page.setdefault(page_idx, []).append(
                    (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1))
                )
                _last_tbl[tbl_ref] = {'x0n': x0n, 'x1n': x1n, 'page_idx': page_idx}
                logger.info(
                    "[vision-path] table %s page=%s: %.0fx%.0f",
                    tbl_ref,
                    page_idx,
                    clip.width,
                    clip.height,
                )

            for fig_norm in tx['figures']:
                x0n, y0n, x1n, y1n = fig_norm
                # Expand Textract clip using the matching LLM image bbox when Textract
                # under-detects the figure extent (e.g. returns a tiny region).
                # Guard: only union with LLM images that carry an explicit figure caption
                # ("Figure N — ...").  Captionless LLM detections may cover multiple
                # adjacent figures on the same page; unguarded overlap expansion would
                # pull the next figure's content into this figure's capture region.
                if llm_pages:
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
                                logger.info(
                                    "[prescan] figure page=%s: LLM bbox union (%.2f,%.2f-%.2f,%.2f)",
                                    page_idx,
                                    x0n,
                                    y0n,
                                    x1n,
                                    y1n,
                                )
                                break
                _FIG_PAD = 15
                clip = fitz.Rect(
                    max(0,  x0n * pw - _FIG_PAD), max(0,  y0n * ph - _FIG_PAD),
                    min(pw, x1n * pw + _FIG_PAD), min(ph, y1n * ph + _FIG_PAD),
                )
                if clip.width <= 5 or clip.height <= 5:
                    continue

                # Priority 1: LLM image bbox overlap — most accurate on pages with
                # multiple figures (e.g. page 37 has both Figure 7 and Figure 8).
                # Pick the LLM image with the highest overlap ratio to the Textract bbox.
                fig_cap = None
                if llm_pages:
                    _pd2 = next((p for p in llm_pages if p and p.get("page_idx") == page_idx), None)
                    if _pd2:
                        _best_ovl = 0.0
                        _no_bbox_cap = None
                        for _ie in _pd2.get("images", []):
                            _ec = (_ie.get("caption") or "").strip()
                            if not _FIGURE_CAPTION_RE.match(_ec):
                                continue
                            _eb = _ie.get("bbox_norm") or []
                            if len(_eb) != 4:
                                if _no_bbox_cap is None:
                                    _ecm0 = _FIGURE_CAPTION_RE.match(_ec)
                                    _no_bbox_cap = _ecm0.group(1) if _ecm0 else _ec
                                continue
                            _llm_w = _eb[2] - _eb[0]
                            _llm_h = _eb[3] - _eb[1]
                            _xovl = min(x1n, _eb[2]) - max(x0n, _eb[0])
                            _yovl = min(y1n, _eb[3]) - max(y0n, _eb[1])
                            if _llm_w > 0 and _llm_h > 0 and _xovl > 0 and _yovl > 0:
                                _ovl_ratio = (_xovl * _yovl) / (_llm_w * _llm_h)
                                if _ovl_ratio > _best_ovl:
                                    _best_ovl = _ovl_ratio
                                    _ecm = _FIGURE_CAPTION_RE.match(_ec)
                                    fig_cap = _ecm.group(1) if _ecm else _ec
                        if fig_cap is None:
                            fig_cap = _no_bbox_cap  # fallback: any captioned image with no bbox

                # Priority 2: text-based search — used when LLM page data is absent
                if fig_cap is None:
                    fig_cap = _find_caption_above(llm_texts_local, page_idx, y0n, _FIGURE_CAPTION_RE)
                if fig_cap is None:
                    fig_cap = _find_caption_below(llm_texts_local, page_idx, y1n, _FIGURE_CAPTION_RE)
                mf = re.match(r'^(\d+)', fig_cap) if fig_cap else None
                fig_num = int(mf.group(1)) if mf else -(len(figures) + 1)
                if fig_num not in figures:
                    pix = page.get_pixmap(clip=clip, dpi=200)
                    figures[fig_num] = pix.tobytes("png")
                    fig_captions[fig_num] = fig_cap or f'Figure {abs(fig_num)}'
                    figures_first_page[fig_num] = page_idx
                    fig_rects_by_page.setdefault(page_idx, []).append(
                        (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1))
                    )
                    logger.info(
                        "[vision-path] figure %s page=%s: %.0fx%.0f",
                        fig_num,
                        page_idx,
                        clip.width,
                        clip.height,
                    )

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

    # ── LLM bbox fallback: capture tables/figures that Textract missed ──────
    # The LLM JSON includes bbox_norm for items it detected. Use those bboxes
    # to crop anything with a recognised caption that Textract didn't find.
    if llm_pages:
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
                _clip = fitz.Rect(
                    max(0,    _x0n * _pw - 5),
                    max(0,    _y0n * _ph - _SUB_HEADER_PTS),
                    min(_pw,  _x1n * _pw + 5),
                    min(_ph - _FOOTER_PTS, _y1n * _ph + _BOTTOM_PAD),
                )
                if _clip.width <= 5 or _clip.height <= 5:
                    continue
                try:
                    _pix = _pg.get_pixmap(clip=_clip, dpi=200)
                    tables_pages[_ref] = [_pix.tobytes("png")]
                    if _ref not in tables_first_page:
                        tables_first_page[_ref] = _pidx
                    if _ref not in tbl_captions:
                        tbl_captions[_ref] = _cap
                    logger.info(
                        "[vision-path] LLM-fallback table %s page=%s: %.0fx%.0f",
                        _ref,
                        _pidx,
                        _clip.width,
                        _clip.height,
                    )
                except Exception as _exc:
                    logger.info(
                        "[vision-path] LLM-fallback table %s page=%s failed: %s",
                        _ref,
                        _pidx,
                        _exc,
                    )

            for img_entry in pd.get("images", []):
                _cap = (img_entry.get("caption") or "").strip()
                _mf  = _FIGURE_CAPTION_RE.match(_cap)
                if not _mf:
                    continue
                _fnum = int(_mf.group(1))
                if _fnum in figures:
                    continue  # already captured by Textract
                _bbox = img_entry.get("bbox_norm")
                if not _bbox or len(_bbox) != 4:
                    continue
                _x0n, _y0n, _x1n, _y1n = _bbox
                # Clip the bottom of this figure's bbox against the top of any
                # later figure on the same page (higher number, lower y0).
                # Without this, a large LLM bbox for Figure N can extend into
                # Figure N+1's region, causing N+1's circuit to appear inside N.
                for _other in pd.get("images", []):
                    _oc = (_other.get("caption") or "").strip()
                    _om = _FIGURE_CAPTION_RE.match(_oc)
                    if not _om:
                        continue
                    _ofnum = int(_om.group(1))
                    if _ofnum <= _fnum:
                        continue  # same figure or earlier — skip
                    _ob = _other.get("bbox_norm")
                    if not _ob or len(_ob) != 4:
                        continue
                    if _ob[1] < _y1n:  # other figure starts above our intended bottom
                        _y1n = _ob[1]
                        logger.info(
                            "[prescan] LLM-fallback fig %s p%s: clipped y1n to %.3f "
                            "(fig %s starts there)",
                            _fnum,
                            _pidx,
                            _y1n,
                            _ofnum,
                        )
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
                    logger.info(
                        "[vision-path] LLM-fallback figure %s page=%s: %.0fx%.0f",
                        _fnum,
                        _pidx,
                        _clip.width,
                        _clip.height,
                    )
                except Exception as _exc:
                    logger.info(
                        "[vision-path] LLM-fallback figure %s page=%s failed: %s",
                        _fnum,
                        _pidx,
                        _exc,
                    )

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
