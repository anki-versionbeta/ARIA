"""Body text for one section. Ported from backend.py:4638-5012.

`_extract_body` walks the pages a section covers and turns each text block into a content
row. Most of its length is filtering, and every filter is there because something real
leaked into a report without it:

* running headers, bare page numbers, "All rights reserved" footers and copyright
  watermarks, which otherwise appear as content rows;
* text sitting inside a detected table, figure or accepted formula — suppressed by
  rectangle overlap, because fitz returns a table as one block per cell and an equation
  as a cloud of positioned glyphs. Figure *captions* are deliberately let through so the
  row that carries them can be matched to its image;
* repeated table footnotes on continuation pages, deduplicated within the section;
* the section's own heading, skipped once.

It stops as soon as it meets a heading belonging to another section, which is what keeps
content from being emitted twice when several TOC entries share a page.

Two end-of-page flushes make sure nothing captured is silently lost: an unnumbered formula
that matched no anchor, and an unlabeled table with no caption, each become a standalone
row.

No seam changes — this module touches neither the network nor the filesystem. It imports
from `rows` at module scope; `rows` imports `_extract_body` inside its function to keep
that from being a cycle.
"""

from __future__ import annotations

import logging
import re

import fitz

from .equations import _ANCHOR_MIN_LEN
from .geometry import _intersect_area, _rect_area
from .mediastore import _media_as_bytes, _table_img_list
from .patterns import (
    _BACK_MATTER_RE,
    _FIGURE_CAPTION_RE,
    _FOOTER_RE,
    _FOOTNOTE_MARKER_RE,
    _PAGE_NUM_RE,
    _RUNNING_HEADER_RE,
    _TABLE_CAPTION_RE,
    _TABLE_CONT_RE,
    _WATERMARK_RE,
)
from .rows import (
    _SUB_ITEM_PATTERNS,
    _build_row_images,
    _detect_inline_subsection,
    _is_stop_heading,
    _split_llm_blocks,
)
from .sections import _section_num

logger = logging.getLogger(__name__)


def _extract_body(doc, section, start_0, end_0,
                  base_num, figures, tables, formulas, stop_nums=None,
                  stop_titles=None, llm_texts=None, emitted=None,
                  captions_in_range=None, unnumbered_formulas=None,
                  formula_emitted=None, table_rects_by_page=None,
                  fig_rects_by_page=None, unlabeled_tables_by_page=None,
                  media_first_page=None, range_start_0=None, range_end_0=None):
    """Extract paragraph rows for one section between start_0 and end_0 (0-indexed).

    Stops extraction as soon as a child/sibling section heading is encountered
    (prevents content duplication when multiple ToC entries share the same page).

    llm_texts: optional dict {page_0_index: text_string}. When provided for a
    page, the pre-extracted LLM text is used instead of the (garbled) fitz text.
    The y-coordinate header/footer filter is skipped for those pages since the
    LLM already excludes running headers and footers.

    unnumbered_formulas: list of (index, formula_dict) tuples from
        find_unnumbered_formulas, pre-filtered to this section.  After each
        content row is emitted, we check whether the block's text contains the
        formula's anchor_text (whitespace-normalized) on the same page_0 and,
        if so, attach the formula's PNG to the row's inline_images.
    formula_emitted: shared set of indices tracking which unnumbered formulas
        have already been inserted (prevents duplicates across sections).

    table_rects_by_page: optional dict {page_0: [(x0, y0, x1, y1), ...]} of
        detected table bboxes from prescan_media.  Text blocks substantially
        contained inside a table rect are suppressed so the table's individual
        cell text is not re-emitted as content rows alongside the inserted
        table image.  Only effective on the normal (fitz-blocks) path; the
        LLM path already excludes per-cell text from its narrative output.
    """
    rows = []
    if stop_nums is None:
        stop_nums = set()
    if stop_titles is None:
        stop_titles = set()
    if emitted is None:
        emitted = set()
    if captions_in_range is None:
        captions_in_range = set()
    if unnumbered_formulas is None:
        unnumbered_formulas = []
    if formula_emitted is None:
        formula_emitted = set()
    if table_rects_by_page is None:
        table_rects_by_page = {}
    if fig_rects_by_page is None:
        fig_rects_by_page = {}
    if unlabeled_tables_by_page is None:
        unlabeled_tables_by_page = {}

    heading_re = re.compile(
        r'^\s*' + re.escape(_section_num(section['title'])) + r'\b',
        re.IGNORECASE
    )
    heading_skipped = False
    sub_stack = [None, None, None, None]   # sub-item markers at each depth
    last_page_0 = None
    # Inline numbered subsection (e.g. 'B.1', 'B.7.2.1', '7.32.1') detected
    # within the body and used as col0 for subsequent content rows.  None
    # means "use base_num".  Reset whenever a new inline heading is seen.
    cur_section_num = None
    section_ended = False
    # Tracks footnote blocks seen in this section so multi-page table footnotes
    # (identical text repeated on every continuation page) are not emitted twice.
    _seen_footnotes: set = set()

    for page_0 in range(start_0, min(end_0 + 1, len(doc))):
        if section_ended:
            break

        # Reset sub-item stack on page change so letter bullets from one page
        # don't carry over and corrupt the section number on the next page.
        if last_page_0 is not None and page_0 != last_page_0:
            sub_stack = [None, None, None, None]
        last_page_0 = page_0

        # Per-page list of unnumbered formulas (with their global index)
        page_formulas = [(idx, uf) for (idx, uf) in unnumbered_formulas
                         if uf.get('page_0') == page_0
                         and idx not in formula_emitted]

        # Bboxes of accepted formulas on this page — used to suppress the
        # glyph-cloud text that fitz returns for equations typeset as
        # positioned glyphs (e.g. "x k s USL + × ( )").  Without this, the
        # equation's raw glyph text appears as a content row alongside the
        # inserted image.
        page_formula_rects = [fitz.Rect(uf['bbox']) for (_i, uf) in page_formulas]

        # Bboxes of detected tables on this page — used to suppress the
        # per-cell text that fitz returns for tables, which would otherwise
        # be re-emitted as one content row per cell alongside the inserted
        # table image.
        page_table_rects = [
            fitz.Rect(*r) for r in (table_rects_by_page.get(page_0) or [])
        ]

        # Bboxes of detected figures on this page — used to suppress text
        # drawn inside circuit diagrams / illustrations (key labels, axis
        # titles, embedded table values) that would otherwise leak as body
        # rows in the DOCX alongside the inserted figure image.
        page_figure_rects = [
            fitz.Rect(*r) for r in (fig_rects_by_page.get(page_0) or [])
        ]

        def _inside_any_rect(bx0, by0, bx1, by1, rects):
            if not rects:
                return False
            br = fitz.Rect(bx0, by0, bx1, by1)
            ba = _rect_area(br)
            if ba <= 0:
                return False
            for fr in rects:
                # Drop when the block is substantially covered by the rect.
                if _intersect_area(br, fr) / ba > 0.5:
                    return True
            return False

        if llm_texts and page_0 in llm_texts:
            # ── Vision path: split LLM text into paragraph-level blocks ─────
            # _split_llm_blocks first splits on double-newlines, then further
            # splits any block whose interior line starts a figure/table caption,
            # ensuring captions always become standalone blocks.
            blocks = _split_llm_blocks(llm_texts[page_0])
        else:
            # ── Normal fitz path ─────────────────────────────────────────────
            page   = doc[page_0]
            page_h = page.rect.height
            blocks = []
            for blk in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, _, btype = blk
                if btype != 0:
                    continue
                # Header / footer filter (normal path only)
                if y1 < 65 or y0 > page_h - 65:
                    continue
                # Suppress glyph-cloud text inside an accepted formula rect.
                if _inside_any_rect(x0, y0, x1, y1, page_formula_rects):
                    continue
                # Suppress per-cell text inside a detected table rect.
                if _inside_any_rect(x0, y0, x1, y1, page_table_rects):
                    continue
                # Suppress text drawn inside a detected figure region,
                # but always allow figure caption rows through so they can
                # be matched inline by _FIGURE_CAPTION_RE.
                if _inside_any_rect(x0, y0, x1, y1, page_figure_rects):
                    if not _FIGURE_CAPTION_RE.match(text):
                        continue
                text = text.strip()
                if text:
                    blocks.append(text)

        for text in blocks:
            if not text or len(text) < 3:
                continue

            # ── Strip watermark / license overlay ───────────────────────────
            if _WATERMARK_RE.search(text[:120]):
                continue

            # ── Strip bare page numbers ──────────────────────────────────────
            if _PAGE_NUM_RE.match(text):
                continue

            # ── Strip running headers (ISO/IEC/BS standard number lines) ────
            if _RUNNING_HEADER_RE.match(text.strip()):
                continue

            # ── Suppress figure key/legend blocks (vision path only) ─────────
            # Normal path uses _inside_any_rect to drop text inside figure bboxes.
            # Vision path has no block coordinates, so pattern-match instead:
            # suppress ONLY the bare "Key" header line (single line, no newlines).
            # Multi-line blocks that start with "Key" are key-item lists
            # (e.g. "Key\n1 position from where...\n2 extension of handle\n...")
            # and must NOT be suppressed — those are body content below the figure.
            if (llm_texts and page_0 in llm_texts
                    and page_figure_rects
                    and re.match(r'^Key\b', text, re.IGNORECASE)
                    and not _FIGURE_CAPTION_RE.match(text)
                    and '\n' not in text.strip()):
                continue

            # ── Strip "N All rights reserved" footers ────────────────────────
            if _FOOTER_RE.match(text.strip()):
                continue

            # ── Deduplicate repeated table footnotes (vision path) ───────────
            # Multi-page tables in ISO/IEC standards repeat footnote lines
            # (e.g. "a  This test applies…") at the bottom of every continuation
            # page.  The vision LLM extracts them once per page, so the same
            # footnote appears multiple times in the section.  Skip any footnote
            # block whose exact text was already emitted within this section.
            if _FOOTNOTE_MARKER_RE.match(text):
                if text in _seen_footnotes:
                    continue
                _seen_footnotes.add(text)

            # ── Stop at back matter (Bibliography, BSI copyright pages) ──────
            if _BACK_MATTER_RE.match(text.strip()):
                section_ended = True
                break

            # ── Skip the section heading itself (first occurrence) ───────────
            if not heading_skipped:
                if heading_re.match(text) or section['title'].strip() in text:
                    heading_skipped = True
                    continue
                if page_0 == start_0:
                    continue   # still before heading on the start page

            # ── Stop at child / sibling section headings ─────────────────────
            if (stop_nums or stop_titles) and _is_stop_heading(text, stop_nums, stop_titles):
                section_ended = True
                break

            # ── Filter layout artifacts ──────────────────────────────────────
            if _TABLE_CONT_RE.match(text):
                continue
            # Skip table captions that repeat on continuation pages in
            # print-optimised PDFs (caption appears again on each page the
            # table spans, but the image was already inserted on the first
            # occurrence).  Without this, every extra caption becomes an
            # empty row with no image, which looks like truncated content.
            _mt_dup = _TABLE_CAPTION_RE.match(text)
            if _mt_dup and f'table:{_mt_dup.group(1)}' in emitted:
                continue

            # ── Detect inline numbered subsection heading ───────────────────
            # When the ToC is coarser than the body (e.g. ToC has 'Annex B'
            # only, but the body contains 'B.1', 'B.7.2.1', ...) we still
            # want col0 to track those deeper subsections.  Updates the
            # running cur_section_num and resets a)/b) sub-item state.
            inline_num = _detect_inline_subsection(text, base_num)
            heading_text = text
            body_remainder = ''
            if inline_num is None:
                # Fallback: LLM may merge heading + body into one block (one newline, not two).
                # Try just the first line so "B.1 Title\nBody text..." still gets detected.
                _lines = text.split('\n')
                _first = _lines[0].strip()
                if _first and len(_lines) > 1:
                    inline_num = _detect_inline_subsection(_first, base_num)
                    if inline_num:
                        heading_text = _first
                        body_remainder = '\n'.join(_lines[1:]).strip()

            if inline_num:
                cur_section_num = inline_num
                sub_stack = [None, None, None, None]
                # Emit a divider row so the deeper subsection is visually
                # separated, mirroring the top-level section header row.
                rows.append({
                    'type':          'header',
                    'section_num':   inline_num,
                    'text':          heading_text,
                    'level':         section['level'],
                    'inline_images': [],
                })
                if not body_remainder:
                    continue
                text = body_remainder  # fall through to emit remainder as content row

            # ── Detect sub-item markers ──────────────────────────────────────
            for level, pattern in _SUB_ITEM_PATTERNS:
                m = pattern.match(text)
                if m:
                    sub_stack[level] = m.group(1) + ')'
                    for deeper in range(level + 1, 4):
                        sub_stack[deeper] = None
                    break

            # Build compound section number
            section_num = cur_section_num or base_num
            for item in sub_stack:
                if item:
                    section_num += ' ' + item
                else:
                    break

            row_images = _build_row_images(text, figures, tables, formulas,
                                           emitted, captions_in_range,
                                           media_first_page=media_first_page,
                                           range_start_0=range_start_0,
                                           range_end_0=range_end_0)

            # ── Attach unnumbered formulas anchored to THIS block ───────────
            # Anchor match is whitespace-collapsed contains-check against the
            # *tail* of the block (anchor_text is the last ~60 chars of the
            # preceding block, per _find_preceding_anchor).
            if page_formulas:
                collapsed = re.sub(r'\s+', ' ', text).strip()
                still_pending = []
                for (idx, uf) in page_formulas:
                    anchor = (uf.get('anchor_text') or '').strip()
                    if not anchor:
                        still_pending.append((idx, uf))
                        continue
                    # Match strategy:
                    #  - endswith is always safe (tight tail match)
                    #  - substring ("contains") is only allowed for LONG
                    #    anchors (>= _ANCHOR_MIN_LEN).  Short anchors like
                    #    "and" or "where" would otherwise false-match as
                    #    substrings of unrelated words (e.g. "and" inside
                    #    "standard").
                    match = collapsed.endswith(anchor)
                    if not match and len(anchor) >= _ANCHOR_MIN_LEN:
                        # Word-boundary check — anchor must sit between
                        # non-alphanumerics (or string ends) in the block.
                        match = bool(re.search(
                            r'(?:^|\W)' + re.escape(anchor) + r'(?:\W|$)',
                            collapsed,
                        ))
                    if match:
                        formula_emitted.add(idx)
                        row_images.append({
                            'type':    'formula',
                            'data':    _media_as_bytes(uf['image']),
                            'caption': uf.get('eqn_number') or '',
                            'latex':   uf.get('latex', ''),
                        })
                    else:
                        still_pending.append((idx, uf))
                page_formulas = still_pending

            rows.append({
                'type':          'content',
                'section_num':   section_num,
                'text':          text,
                'level':         section['level'],
                'inline_images': row_images,
            })

        # ── End-of-page flush: any formulas that never matched a block on
        # this page get appended as a standalone row so their PNG is not lost.
        for (idx, uf) in page_formulas:
            if idx in formula_emitted:
                continue
            formula_emitted.add(idx)
            rows.append({
                'type':        'content',
                'section_num': cur_section_num or base_num,
                'text':        '',
                'level':       section['level'],
                'inline_images': [{
                    'type':    'formula',
                    'data':    _media_as_bytes(uf['image']),
                    'caption': uf.get('eqn_number') or '',
                    'latex':   uf.get('latex', ''),
                }],
            })

        # ── End-of-page flush: unlabeled tables (no matching caption) are
        # emitted here so their PNG is never silently dropped.
        for tbl_ref in unlabeled_tables_by_page.get(page_0, []):
            key = f'table:{tbl_ref}'
            if key in emitted or tbl_ref not in tables:
                continue
            emitted.add(key)
            rows.append({
                'type':        'content',
                'section_num': cur_section_num or base_num,
                'text':        '',
                'level':       section['level'],
                'inline_images': [
                    {'type': 'table', 'ref': tbl_ref, 'data': _pg,
                     'caption': f'Table (page {page_0 + 1})'}
                    for _pg in _table_img_list(tables[tbl_ref])
                ],
            })

        if section_ended:
            break

    return rows
