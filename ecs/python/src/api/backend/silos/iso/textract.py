"""Where on the page is the table, and where is the figure?

Ported from backend.py:763-904. Two jobs:

* **Caption search** over the vision model's page text. ISO puts figure captions *below*
  the figure and table captions *above* the table, which is why there are two functions
  rather than one with a flag.
* **Page analysis via AWS Textract**, whose cell-level geometry is more reliable than a
  language model's estimate of where a table's edges are — especially on a continuation
  page with no column headers and no visible borders. The result is then matched back to
  the model's own bbox by IoU, and the model's coordinates are used if nothing matches.

Seam changes only. `_call_textract_page` used to construct its own AWS client and name
the region in source; it now takes the analysis callable as an argument, because a silo
may not build a client of its own — the per-page concurrency ceiling and the
credential-expiry retry belong to the platform. The platform's `analyze_document`
already defaults to `FeatureTypes=['LAYOUT', 'TABLES']`, which is exactly what the
source asked for, so the request itself is unchanged.

Cost note: this is **one charged analysis per page**, and it runs on every page of a
document that took the vision path. A 200-page scan is 200 analyses.
"""

from __future__ import annotations

import logging
import re

import fitz

from .geometry import _intersect_area, _rect_area

logger = logging.getLogger(__name__)


def _find_caption_above(llm_texts, page_idx, table_y0_norm, caption_re):
    """Return caption ref (e.g. 'B.1', '3') from LLM text on page_idx, or None.
    Only searches text blocks that appear above the table's y-position so that
    a later caption (or body cross-reference) on the same page cannot be matched."""
    text = (llm_texts or {}).get(page_idx, '')
    blocks = [b.strip() for b in re.split(r'\n{2,}', text) if b.strip()]
    if not blocks:
        return None
    # LLM text is ordered top-to-bottom.  Limit search to the first
    # (y0_norm * N + 2) blocks — those are above the table position.
    cutoff = max(1, int(table_y0_norm * len(blocks)) + 2)
    for blk in reversed(blocks[:cutoff]):
        mt = caption_re.match(blk)
        if mt:
            return mt.group(1)
    return None


def _find_caption_below(llm_texts, page_idx, fig_y1_norm, caption_re):
    """Search text blocks BELOW the figure's bottom edge for a caption.
    ISO documents place figure captions below the figure image."""
    text = (llm_texts or {}).get(page_idx, '')
    blocks = [b.strip() for b in re.split(r'\n{2,}', text) if b.strip()]
    if not blocks:
        return None
    start = max(0, int(fig_y1_norm * len(blocks)) - 1)
    for blk in blocks[start:]:
        mt = caption_re.match(blk)
        if mt:
            return mt.group(1)
    return None


def _call_textract_page(png_bytes, textract):
    """Send a page PNG to AWS Textract LAYOUT+TABLES analysis.

    Uses TABLES feature for precise cell-level table detection (handles
    continuation pages without headers) and LAYOUT for figure detection.

    Returns dict with keys 'tables' and 'figures', each a list of
    [x0, y0, x1, y1] normalized bounding boxes (0.0-1.0, origin top-left).
    Falls back to empty lists on any error so callers can safely use LLM bbox.
    """
    try:
        response = textract(png_bytes)
    except Exception as exc:
        # Empty lists rather than a raise: the caller then uses the model's own bbox,
        # which is worse but still produces a document.
        logger.info("[textract] analyze_document failed: %s", exc)
        return {'tables': [], 'figures': []}

    blocks = response.get('Blocks', [])
    block_map = {b['Id']: b for b in blocks if 'Id' in b}

    # ── Tables: compute bbox from CELL blocks under each TABLE block ─────────
    # TABLES feature gives cell-level geometry — more reliable than LAYOUT_TABLE
    # on continuation pages that lack column headers or visible borders.
    tables = []
    figures = []
    layout_table_bboxes = []

    for block in blocks:
        btype = block.get('BlockType', '')
        bb = block.get('Geometry', {}).get('BoundingBox')

        if btype == 'LAYOUT_FIGURE' and bb:
            figures.append([
                bb['Left'], bb['Top'],
                bb['Left'] + bb['Width'], bb['Top'] + bb['Height'],
            ])

        elif btype == 'LAYOUT_TABLE' and bb:
            layout_table_bboxes.append([
                bb['Left'], bb['Top'],
                bb['Left'] + bb['Width'], bb['Top'] + bb['Height'],
            ])

        elif btype == 'TABLE':
            # Collect all CELL children and compute the outer envelope bbox
            cell_ids = [
                rel['Ids']
                for rel in block.get('Relationships', [])
                if rel.get('Type') == 'CHILD'
            ]
            cell_ids_flat = [cid for ids in cell_ids for cid in ids]
            xs0, ys0, xs1, ys1 = [], [], [], []
            for cid in cell_ids_flat:
                cell = block_map.get(cid)
                if not cell:
                    continue
                cbb = cell.get('Geometry', {}).get('BoundingBox')
                if not cbb:
                    continue
                xs0.append(cbb['Left'])
                ys0.append(cbb['Top'])
                xs1.append(cbb['Left'] + cbb['Width'])
                ys1.append(cbb['Top']  + cbb['Height'])
            if xs0:
                tables.append([min(xs0), min(ys0), max(xs1), max(ys1)])

    # Fall back to LAYOUT_TABLE bboxes if TABLES feature found nothing
    if not tables and layout_table_bboxes:
        tables = layout_table_bboxes
        logger.info("[textract] using LAYOUT_TABLE fallback: %d table(s)", len(tables))

    logger.info(
        "[textract] page analysis: %d table(s), %d figure(s)", len(tables), len(figures)
    )
    return {'tables': tables, 'figures': figures}


def _best_textract_match(llm_bbox_norm, textract_bboxes, pw, ph, min_iou=0.10):
    """Return the Textract bbox (as fitz.Rect in page points) with the best
    IoU overlap against the LLM bbox_norm.  Returns None if no match meets
    min_iou so the caller can fall back to the LLM coordinates.
    """
    if not textract_bboxes:
        return None
    llm_rect = fitz.Rect(
        llm_bbox_norm[0] * pw, llm_bbox_norm[1] * ph,
        llm_bbox_norm[2] * pw, llm_bbox_norm[3] * ph,
    )
    llm_area = _rect_area(llm_rect)
    if llm_area <= 0:
        return None

    best_rect, best_iou = None, 0.0
    for tb in textract_bboxes:
        t_rect = fitz.Rect(tb[0] * pw, tb[1] * ph, tb[2] * pw, tb[3] * ph)
        inter  = _intersect_area(llm_rect, t_rect)
        union  = llm_area + _rect_area(t_rect) - inter
        if union <= 0:
            continue
        iou = inter / union
        if iou > best_iou:
            best_iou, best_rect = iou, t_rect

    if best_iou >= min_iou:
        logger.info("[textract] matched bbox iou=%.2f", best_iou)
        return best_rect
    logger.info("[textract] no match found (best_iou=%.2f < %s)", best_iou, min_iou)
    return None
