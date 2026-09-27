"""Turning selected sections into assessment rows. Ported from backend.py:3959-4636.

`extract_all_rows` is the driver: for every TOC entry in the chosen range it emits a
header row and then the body rows for that section, and finally flushes anything that was
captured during the pre-scan but never referenced by any text. A row is:

    {"type": "header" | "content", "section_num": str, "text": str,
     "level": int, "inline_images": [ ... ]}

The interesting problem here is **each figure, table and formula must appear exactly
once**, and the text that mentions it may come before, after, or instead of its caption.
`_build_row_images` resolves that with a four-way rule — caption wins; a reference defers
to a caption that is coming later in range; a reference to something whose caption is out
of range attaches the image itself; anything already emitted is highlighted only. The
`emitted` set is shared across every section for exactly this reason.

Two flush passes at the end exist because a caption can fall between two sections' TOC
boundaries and so never be seen by any row. Placement is page-based first (which section
owns the capture page), then by first text reference, then appended.

`_xml_safe` is imported from `patterns` rather than redefined here: `docxstyle` needs it
too, and two copies of a control-character filter would be a bad thing to let drift.
`_extract_body` is imported inside `extract_all_rows` because `body.py` imports this
module at import time — a function-level import breaks the cycle without moving any logic.
"""

from __future__ import annotations

import logging
import os
import re

import fitz
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement

from .mediastore import _media_as_bytes, _table_img_list
from .patterns import (
    _ANNEX_FIG_CAP_RE,
    _ANNEX_FIG_REF_RE,
    _FIGURE_CAPTION_RE,
    _FORMULA_TRAIL_RE,
    _MATH_CHARS_RE,
    _parse_fig_ref,
    _REF_HIGHLIGHT_RE,
    _TABLE_CAPTION_RE,
    _xml_safe,
)
from .sections import _section_end_page_0, _section_num

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONTENT EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_SUB_ITEM_PATTERNS = [
    (0, re.compile(r'^([a-z])\)\s+')),                              # a) b) c)
    (1, re.compile(r'^(\d+)\)\s+')),                                # 1) 2) 3)
    (2, re.compile(r'^([ivxlcdm]+)\)\s+', re.IGNORECASE)),         # i) ii) iii)
    (3, re.compile(r'^([IVX]+)\)\s+')),                             # I) II) III)
]

_FIG_REF_RE     = re.compile(r'\bFigure\s+(\d+)\b',                              re.IGNORECASE)
_TABLE_REF_RE   = re.compile(r'\bTable\s+([A-Z](?:\.\d+)+|\d+(?:\.\d+)*)\b',   re.IGNORECASE)
_FORMULA_REF_RE = re.compile(r'\bFormula\s+\((\d+)\)|\((\d+)\)\s*$', re.IGNORECASE)


def _split_llm_blocks(page_text):
    """Split LLM page text into paragraph blocks for the vision path.

    First splits on two-or-more newlines (paragraph separator).
    Then re-splits any block that has a figure or table caption embedded
    on an interior line, so the caption becomes a standalone block and
    is recognised by _FIGURE_CAPTION_RE / _TABLE_CAPTION_RE.
    """
    raw = [b.strip() for b in re.split(r'\n{2,}', page_text) if b.strip()]
    blocks = []
    for blk in raw:
        lines = blk.split('\n')
        current = []
        for line in lines:
            ls = line.strip()
            if current and (_FIGURE_CAPTION_RE.match(ls) or _TABLE_CAPTION_RE.match(ls)):
                accumulated = '\n'.join(current).strip()
                if accumulated:
                    blocks.append(accumulated)
                current = [ls]
            else:
                current.append(line)
        if current:
            remaining = '\n'.join(current).strip()
            if remaining:
                blocks.append(remaining)
    return blocks


def _add_run_with_breaks(para, text, font_size, highlight=False):
    """Add *text* as run(s) to *para*, converting \\n to Word line-break elements."""
    parts = text.split('\n')
    for idx, part in enumerate(parts):
        if part:
            run = para.add_run(part)
            run.font.size = font_size
            if highlight:
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        if idx < len(parts) - 1:
            br = OxmlElement('w:br')
            if para.runs:
                para.runs[-1]._r.append(br)
            else:
                run = para.add_run()
                run.font.size = font_size
                run._r.append(br)


def _write_text_highlighted(para, text, font_size):
    """Write *text* into *para*, highlighting Figure/Table/Formula references in yellow.

    Newlines in the text are rendered as Word line breaks so key-item lists
    and multi-line blocks are visually separated in the output table cell.
    """
    text = _xml_safe(text)
    last = 0
    for m in _REF_HIGHLIGHT_RE.finditer(text):
        if m.start() > last:
            _add_run_with_breaks(para, text[last:m.start()], font_size, highlight=False)
        _add_run_with_breaks(para, m.group(), font_size, highlight=True)
        last = m.end()
    if last < len(text):
        _add_run_with_breaks(para, text[last:], font_size, highlight=False)


def _build_row_images(text, figures, tables, formulas, emitted,
                      captions_in_range=None,
                      media_first_page=None, range_start_0=None, range_end_0=None):
    """Collect images using caption-first, first-reference-fallback strategy.

    Each figure/table/formula appears at most once across the entire output:
    1. If the text IS the caption → attach image, mark emitted.
    2. Else if the text references an item whose caption IS in the selected
       range (present in *captions_in_range*) → skip, let the caption row
       handle it later.
    3. Else if the text references an item whose caption is NOT in the range
       and hasn't been emitted yet → attach image (fallback), mark emitted.
    4. Else (already emitted) → skip, yellow highlight only.

    *emitted*: shared set across all rows — tracks what has been pasted.
    *captions_in_range*: set of keys whose captions exist somewhere in the
        selected page range (built by a pre-scan before extraction starts).
    *media_first_page*: dict mapping 'table:ref'/'figure:N' → first capture page.
    *range_start_0/range_end_0*: selected page bounds (0-indexed). Reference-based
        emissions of items captured outside this range are suppressed.
    """
    imgs = []
    if captions_in_range is None:
        captions_in_range = set()

    def _dedup_img_append(img_dict):
        """Append img_dict only if its content hash has not been emitted before.
        Prevents the same PNG from appearing twice when a table bleed captures
        a figure's region and the figure is later placed at its own caption row.
        """
        raw = img_dict.get('data', b'')
        if raw:
            # Use first 512 bytes as a fast fingerprint — PNG content headers
            # are unique per image even when the file metadata differs.
            _hkey = f'imghash:{hash(raw[:512]) & 0xFFFFFFFF}'
            if _hkey in emitted:
                return
            emitted.add(_hkey)
        imgs.append(img_dict)

    def _in_media_range(key):
        """Return False if this key's capture page is known and outside range."""
        if media_first_page is None or range_start_0 is None:
            return True
        pg = media_first_page.get(key)
        if pg is None:
            return True
        return range_start_0 <= pg <= range_end_0

    # ── Figures ───────────────────────────────────────────────────────────
    mf = _FIGURE_CAPTION_RE.match(text)
    mf_ann = _ANNEX_FIG_CAP_RE.match(text) if not mf else None
    if mf or mf_ann:
        # Text IS the caption → paste here; suppress redundant subtitle (caption already in row text)
        fn = int(mf.group(1)) if mf else _parse_fig_ref(mf_ann.group(1))
        key = f'figure:{fn}'
        if fn is not None and fn in figures and key not in emitted:
            emitted.add(key)
            _dedup_img_append({'type': 'figure', 'number': fn,
                         'data': _media_as_bytes(figures[fn]), 'caption': ''})
    else:
        # Text is a reference → fallback only when caption is NOT in range
        for fig_str in _FIG_REF_RE.findall(text):
            fn = int(fig_str)
            key = f'figure:{fn}'
            if key in captions_in_range:
                continue   # caption is coming later — skip
            if not _in_media_range(key):
                continue   # figure captured outside selected page range — skip
            if fn in figures and key not in emitted:
                emitted.add(key)
                _dedup_img_append({'type': 'figure', 'number': fn,
                             'data': _media_as_bytes(figures[fn]), 'caption': f'Figure {fn}'})
        # Also check for Annex figure references (Figure A.1, H.2, etc.)
        for ann_ref in _ANNEX_FIG_REF_RE.findall(text):
            fn = _parse_fig_ref(ann_ref)
            if fn is None:
                continue
            key = f'figure:{fn}'
            if key in captions_in_range:
                continue
            if not _in_media_range(key):
                continue
            if fn in figures and key not in emitted:
                emitted.add(key)
                _dedup_img_append({'type': 'figure', 'number': fn,
                             'data': _media_as_bytes(figures[fn]), 'caption': f'Figure {ann_ref}'})

    # ── Tables ────────────────────────────────────────────────────────────
    mt = _TABLE_CAPTION_RE.match(text)
    if mt:
        tbl_ref = mt.group(1)
        key = f'table:{tbl_ref}'
        if tbl_ref in tables and key not in emitted:
            emitted.add(key)
            # Caption is already in row text; pass empty so _add_image_to_cell
            # does not render a second bare "Table X.Y" label below the image.
            # Each page slice is inserted as a separate image entry (Fix B/D).
            for _pg_bytes in _table_img_list(tables[tbl_ref]):
                _dedup_img_append({'type': 'table', 'ref': tbl_ref,
                             'data': _pg_bytes, 'caption': ''})
    else:
        for tbl_ref in _TABLE_REF_RE.findall(text):
            key = f'table:{tbl_ref}'
            if key in captions_in_range:
                continue
            if not _in_media_range(key):
                continue   # table captured outside selected page range — skip
            if tbl_ref in tables and key not in emitted:
                emitted.add(key)
                for _pg_bytes in _table_img_list(tables[tbl_ref]):
                    _dedup_img_append({'type': 'table', 'ref': tbl_ref,
                                 'data': _pg_bytes, 'caption': f'Table {tbl_ref}'})

    # ── Formulas ──────────────────────────────────────────────────────────
    fm = _FORMULA_TRAIL_RE.search(text)
    if fm and _MATH_CHARS_RE.search(text):
        fn = int(fm.group(1))
        key = f'formula:{fn}'
        if fn in formulas and key not in emitted:
            emitted.add(key)
            _dedup_img_append({'type': 'formula', 'number': fn,
                         'data': _media_as_bytes(formulas[fn]), 'caption': f'Formula ({fn})'})
    else:
        for fm_tuple in _FORMULA_REF_RE.findall(text):
            fn = int(fm_tuple[0] or fm_tuple[1])
            key = f'formula:{fn}'
            if key in captions_in_range:
                continue
            if fn in formulas and key not in emitted:
                emitted.add(key)
                _dedup_img_append({'type': 'formula', 'number': fn,
                             'data': _media_as_bytes(formulas[fn]), 'caption': f'Formula ({fn})'})

    return imgs


def extract_all_rows(pdf_path, toc, start_idx, end_idx, figures, tables, formulas,
                     llm_texts=None, unnumbered_formulas=None,
                     table_rects_by_page=None, fig_rects_by_page=None,
                     unlabeled_tables_by_page=None, media_first_page=None):
    """Return a flat list of row dicts covering selected TOC sections.

    llm_texts: optional dict {page_0_index: text_string} for the vision path.
    unnumbered_formulas: optional list of enriched formula dicts from
        find_unnumbered_formulas().  Entries are placed into the DOCX at the
        paragraph whose text ends with / contains the entry's anchor_text.
    table_rects_by_page: optional dict {page_0: [(x0, y0, x1, y1), ...]} of
        detected table bboxes from prescan_media.  Used by _extract_body to
        suppress text blocks substantially contained inside a table region
        (parallel to the existing formula-rect suppression).
    """
    # Imported here, not at module scope: body.py imports this module, so a
    # module-level import would be circular.
    from .body import _extract_body

    doc = fitz.open(pdf_path)
    all_rows = []
    doc_last_0 = len(doc) - 1

    # Shared set tracking which figures/tables/formulas have already been
    # emitted into the DOCX.  Passed through every _extract_body call so
    # each image appears at most once across the entire output.
    emitted = set()

    # Absolute end page (0-indexed) of the whole selection
    selection_end_0 = _section_end_page_0(toc, end_idx, doc_last_0)

    # Page range of the selected sections — used to filter out-of-range media
    _range_start_0 = toc[start_idx]['page'] - 1
    _range_end_0   = selection_end_0

    # ── Pre-scan: find which captions exist in the selected page range ───
    # Scan text blocks on pages covered by the selection for caption
    # patterns.  This tells _build_row_images whether a caption is coming
    # later so it can skip the reference-fallback and let the caption row
    # handle the image.
    range_start_0 = toc[start_idx]['page'] - 1
    captions_in_range = set()
    for page_0 in range(range_start_0, min(selection_end_0 + 1, len(doc))):
        # Fix A: always use fitz text for caption detection.
        # LLM text paraphrases captions ("Table 4 shows the conditions...")
        # and may not reproduce the exact "Table N —" format that
        # _TABLE_CAPTION_RE requires, causing captions to be missed and
        # the reference-fallback to fire twice for the same table.
        # Fitz text is verbatim — always use it here.
        blocks = []
        for blk in doc[page_0].get_text("blocks", sort=True):
            if blk[6] == 0 and blk[4].strip():
                blocks.append(blk[4].strip())
        for text in blocks:
            mf = _FIGURE_CAPTION_RE.match(text)
            if mf:
                captions_in_range.add(f'figure:{int(mf.group(1))}')
            mf_ann = _ANNEX_FIG_CAP_RE.match(text)
            if mf_ann:
                _ann_key = _parse_fig_ref(mf_ann.group(1))
                if _ann_key is not None:
                    captions_in_range.add(f'figure:{_ann_key}')
            mt = _TABLE_CAPTION_RE.match(text)
            if mt:
                captions_in_range.add(f'table:{mt.group(1)}')
            fm = _FORMULA_TRAIL_RE.search(text)
            if fm and _MATH_CHARS_RE.search(text):
                captions_in_range.add(f'formula:{int(fm.group(1))}')

    # ── Defer reference-based emission for captured figures (VISION only) ───
    # On the vision path the LLM's clean text is nondeterministic: it reproduces
    # some figure captions in full ("Figure C.2 — …") but omits others, leaving
    # only bare in-text references.  Adding every captured real-figure key here
    # makes the reference-fallback DEFER them (captions_in_range gates only the
    # reference branch, never the caption branch), so a figure emits at its real
    # "Figure N —" caption when the LLM text has it.  Unlabeled fallback keys
    # (-1, -2, …) are left out.  Gated to the vision path: the NORMAL path relies
    # on the reference-fallback to place figures whose caption row was suppressed.
    if llm_texts:
        for _cf_key in figures:
            if _cf_key > 0 or _cf_key <= -1000:
                captions_in_range.add(f'figure:{_cf_key}')

    if captions_in_range:
        logger.info("[extract] pre-scan found %d captions in range", len(captions_in_range))

    # Numbers of all sections that appear after each position — used as stop signals
    all_nums_after = {}
    # Full TOC titles of subsequent sections — used as stop signals for
    # unnumbered headings (e.g. "Scope", "Classification") that don't have
    # a section number prefix and would be missed by _STOP_HDG_RE.
    all_titles_after = {}
    for si in range(start_idx, end_idx + 1):
        all_nums_after[si] = {
            _section_num(toc[j]['title'])
            for j in range(si + 1, len(toc))
            if _section_num(toc[j]['title'])   # non-empty
        }
        all_titles_after[si] = {
            toc[j]['title'].strip()
            for j in range(si + 1, len(toc))
            if toc[j]['title'].strip()
        }

    # ── Index unnumbered formulas by (section_idx, page_0) for O(1) lookup ──
    # Each entry is consumed at most once — tracked via formula_emitted.
    fml_by_section = {}
    formula_emitted = set()
    for i, uf in enumerate(unnumbered_formulas or []):
        sec_i = uf.get('section_idx')
        if sec_i is None:
            continue
        fml_by_section.setdefault(sec_i, []).append((i, uf))
    if unnumbered_formulas:
        logger.info(
            "[extract] unnumbered formulas per section: %s",
            {k: len(v) for k, v in fml_by_section.items()},
        )

    for sec_idx in range(start_idx, end_idx + 1):
        sec = toc[sec_idx]
        base_num = _section_num(sec['title'])

        sec_start_0 = sec['page'] - 1
        sec_end_0   = min(_section_end_page_0(toc, sec_idx, doc_last_0),
                          selection_end_0)

        # Section divider / header row
        all_rows.append({
            'type':         'header',
            'section_num':  base_num,
            'text':         sec['title'],
            'level':        sec['level'],
            'inline_images': [],
        })

        # Body content rows – stop at the first child/sibling heading encountered
        body = _extract_body(doc, sec, sec_start_0, sec_end_0,
                             base_num, figures, tables, formulas,
                             stop_nums=all_nums_after[sec_idx],
                             stop_titles=all_titles_after[sec_idx],
                             llm_texts=llm_texts,
                             emitted=emitted,
                             captions_in_range=captions_in_range,
                             unnumbered_formulas=fml_by_section.get(sec_idx, []),
                             formula_emitted=formula_emitted,
                             table_rects_by_page=table_rects_by_page,
                             fig_rects_by_page=fig_rects_by_page,
                             unlabeled_tables_by_page=unlabeled_tables_by_page or {},
                             media_first_page=media_first_page,
                             range_start_0=_range_start_0,
                             range_end_0=_range_end_0)
        all_rows.extend(body)

    doc.close()

    # ── Flush any unnumbered formulas that never matched a text anchor.
    # These get appended as standalone rows to the LAST section they fell in,
    # so content is never silently dropped.
    for sec_idx, entries in fml_by_section.items():
        if sec_idx < start_idx or sec_idx > end_idx:
            continue
        for i, uf in entries:
            if i in formula_emitted:
                continue
            formula_emitted.add(i)
            all_rows.append({
                'type':        'content',
                'section_num': _section_num(toc[sec_idx]['title']),
                'text':        '',   # standalone formula row, no descriptive text
                'level':       toc[sec_idx]['level'],
                'inline_images': [{
                    'type':    'formula',
                    'data':    _media_as_bytes(uf['image']),
                    'caption': uf.get('eqn_number') or '',
                    'latex':   uf.get('latex', ''),
                }],
            })

    # ── Flush tables/figures captured in prescan but never triggered by text.
    # Happens when the caption page falls between section TOC boundaries so
    # _build_row_images never sees the caption line.
    #
    # Placement strategy (in priority order):
    #  1. Page-based: use tables_first_page to find which section owns the
    #     capture page, then insert at the END of that section's rows. This
    #     correctly places "Table 3" in Section 9 even when Section 9's TOC
    #     end-page is one page short of the actual table page.
    #  2. Text reference: fall back to inserting after the first row whose
    #     body text references the item ("...see Table 3..."). Used when no
    #     page-based section can be found.
    #  3. Append at end as last resort.
    _flush_sec_num   = _section_num(toc[end_idx]['title'])
    _flush_sec_level = toc[end_idx]['level']

    # Selected page range (0-indexed) — used to drop out-of-range captures
    _range_start_0 = toc[start_idx]['page'] - 1
    _range_end_0   = _section_end_page_0(toc, end_idx, doc_last_0)
    logger.info(
        "[extract] range=%s-%s end_sec=toc[%s]page=%s next=%s",
        _range_start_0,
        _range_end_0,
        end_idx,
        toc[end_idx]['page'],
        toc[end_idx + 1] if end_idx + 1 < len(toc) else 'EOF',
    )

    # Build section_num → (start_page_0, end_page_0) from TOC for page-based lookup
    _sec_page_ranges = {}
    for _si in range(start_idx, end_idx + 1):
        _snum = _section_num(toc[_si]['title'])
        _s0   = toc[_si]['page'] - 1
        _e0   = _section_end_page_0(toc, _si, doc_last_0)
        _sec_page_ranges[_snum] = (_s0, _e0)

    def _in_selected_range(capture_pg):
        if capture_pg is None:
            return True  # unknown page — don't filter
        return _range_start_0 <= capture_pg <= _range_end_0

    def _find_section_end_insert_idx(capture_page_0):
        """Return index AFTER the last row of the section that starts on or
        before capture_page_0 (latest start wins when sections share a page)."""
        best_snum = None
        best_s0   = -1
        for snum, (s0, e0) in _sec_page_ranges.items():
            if s0 <= capture_page_0 and s0 >= best_s0:
                best_s0   = s0
                best_snum = snum
        if best_snum is None:
            return None
        last_idx = None
        for i, row in enumerate(all_rows):
            if row.get('section_num') == best_snum:
                last_idx = i
        return (last_idx + 1) if last_idx is not None else None

    def _find_reference_insert_idx(label_re):
        """Return index AFTER the first all_rows entry whose text matches label_re."""
        for i, row in enumerate(all_rows):
            if label_re.search(row.get('text', '')):
                return i + 1
        return None

    _mfp = media_first_page or {}

    for tbl_ref, tbl_data in tables.items():
        key = f'table:{tbl_ref}'
        if key not in emitted:
            emitted.add(key)
            capture_pg = _mfp.get(f'table:{tbl_ref}')
            # Drop tables captured outside the selected section range
            if not _in_selected_range(capture_pg):
                logger.info(
                    "[extract] skip out-of-range table %s (page %s)", tbl_ref, capture_pg
                )
                continue
            # Page-based placement: find section that owns the capture page
            insert_idx = None
            if capture_pg is not None:
                insert_idx = _find_section_end_insert_idx(capture_pg)
                if insert_idx is not None:
                    logger.info(
                        "[extract] flush table %s → page-based after row %s (page %s)",
                        tbl_ref,
                        insert_idx - 1,
                        capture_pg,
                    )
            # Fall back to first text reference
            if insert_idx is None:
                ref_re = re.compile(
                    r'\bTable\s+' + re.escape(str(tbl_ref)) + r'\b', re.IGNORECASE
                )
                insert_idx = _find_reference_insert_idx(ref_re)
                if insert_idx is not None:
                    logger.info(
                        "[extract] flush table %s → ref-based after row %s",
                        tbl_ref,
                        insert_idx - 1,
                    )
            flush_row = {
                'type':        'content',
                'section_num': all_rows[insert_idx - 1]['section_num'] if insert_idx else _flush_sec_num,
                'text':        f'Table {tbl_ref}',
                'level':       all_rows[insert_idx - 1]['level'] if insert_idx else _flush_sec_level,
                'inline_images': [
                    {'type': 'table', 'ref': tbl_ref, 'data': _pg, 'caption': ''}
                    for _pg in _table_img_list(tbl_data)
                ],
            }
            if insert_idx is not None:
                all_rows.insert(insert_idx, flush_row)
            else:
                all_rows.append(flush_row)
                logger.info(
                    "[extract] flush unemitted table: %s (no reference, appended at end)",
                    tbl_ref,
                )

    for fig_num, fig_data in figures.items():
        key = f'figure:{fig_num}'
        if key not in emitted:
            # Skip tiny noise images — real figures are always larger than 2 KB
            _fig_size = len(fig_data) if isinstance(fig_data, (bytes, bytearray)) else os.path.getsize(fig_data)
            if _fig_size < 2000:
                logger.info(
                    "[extract] skip tiny figure %s (%s bytes, likely noise)",
                    fig_num,
                    _fig_size,
                )
                continue
            emitted.add(key)
            capture_pg = _mfp.get(f'figure:{fig_num}')
            # Drop figures captured outside the selected section range
            if not _in_selected_range(capture_pg):
                logger.info(
                    "[extract] skip out-of-range figure %s (page %s)", fig_num, capture_pg
                )
                continue
            # Positive-keyed figures: place where image physically appears in PDF (page-based),
            # fall back to first text reference only if page-based placement fails.
            # Negative-keyed figures: try page-based placement; skip if none found
            if fig_num > 0:
                insert_idx = None
                if capture_pg is not None:
                    insert_idx = _find_section_end_insert_idx(capture_pg)
                if insert_idx is None:
                    ref_re = re.compile(
                        r'\bFig(?:ure)?\.?\s+' + re.escape(str(fig_num)) + r'\b', re.IGNORECASE
                    )
                    insert_idx = _find_reference_insert_idx(ref_re)
            else:
                # No reliable caption/reference — place after the section that owns the page,
                # but only if the page is known. If we can't place it, skip to avoid
                # creating a spurious "Figure N" entry at the end of the document.
                insert_idx = _find_section_end_insert_idx(capture_pg) if capture_pg is not None else None
                if insert_idx is None:
                    logger.info(
                        "[extract] skip unplaced figure %s (page %s)", fig_num, capture_pg
                    )
                    continue
            flush_row = {
                'type':        'content',
                'section_num': all_rows[insert_idx - 1]['section_num'] if insert_idx else _flush_sec_num,
                'text':        f'Figure {abs(fig_num)}',
                'level':       all_rows[insert_idx - 1]['level'] if insert_idx else _flush_sec_level,
                'inline_images': [{
                    'type':    'figure',
                    'number':  fig_num,
                    'data':    _media_as_bytes(fig_data),
                    'caption': '',
                }],
            }
            if insert_idx is not None:
                all_rows.insert(insert_idx, flush_row)
                logger.info(
                    "[extract] flush figure %s → inserted after row %s (page %s)",
                    fig_num,
                    insert_idx - 1,
                    capture_pg,
                )
            else:
                all_rows.append(flush_row)
                logger.info("[extract] flush unemitted figure: %s (appended at end)", fig_num)

    return all_rows


# Matches "8.3.1 Title", "8.3.1\t\nTitle" (tab+newline split), or bare "8.3.1"
_STOP_HDG_RE = re.compile(r'^(\d+(?:\.\d+)*|Annex\s+[A-Z])(?:\s+\S|\s*$)', re.IGNORECASE)

# Matches a numbered subsection identifier at the start of a block:
#   "B.1", "B.7.2.1", "7.32.1", "10.3.1"
# Used to detect inline subsection headings inside a body block so that
# col0 of subsequent rows can be updated to the deeper number, even when
# the ToC stops at a coarser level (e.g. a single "Annex B" entry covering
# all of B.1..B.n).
_INLINE_SUB_NUM_RE = re.compile(r'^([A-Z]\.\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)+)\b')


def _annex_letter(base_num):
    """Return the annex letter ('B') if base_num is 'Annex B' (or 'Annex\xa0B'); else None."""
    m = re.match(r'^Annex[\s\xa0]+([A-Z])\b', base_num or '', re.IGNORECASE)
    return m.group(1).upper() if m else None


def _is_descendant_num(new_num, base_num):
    """True if new_num is a deeper subsection of base_num.

    Examples:
        ('B.1',     'Annex B') → True
        ('B.7.2.1', 'Annex B') → True
        ('C.1',     'Annex B') → False
        ('7.32.1',  '7')        → True
        ('7.32.1',  '7.4')      → True   (still under clause 7)
        ('7.32.1',  '8')        → False
        ('B.1',     '7')        → False
    """
    if not new_num or not base_num:
        return False
    letter = _annex_letter(base_num)
    if letter:
        return new_num.upper().startswith(letter + '.')
    # Numeric base like "7" or "7.4" — child must share the leading clause number
    base_head = base_num.split('.', 1)[0]
    new_head  = new_num.split('.', 1)[0]
    return base_head == new_head and '.' in new_num


def _detect_inline_subsection(text, base_num):
    """If *text* is an inline numbered subsection heading under *base_num*, return its number; else None.

    Heuristics for "looks like a heading":
      - First non-empty line starts with a NUM matched by _INLINE_SUB_NUM_RE.
      - NUM is a descendant of base_num (see _is_descendant_num).
      - The block is short overall (heading-like): collapsed length <= 200 chars
        and at most 4 lines.  Body paragraphs that happen to start with a
        cross-reference like '7.4.5 specifies ...' would normally exceed these
        bounds and are not misclassified.

    Returns the bare subsection number string (e.g. 'B.1', 'B.7.2.1') or None.
    """
    if not text:
        return None
    stripped = text.lstrip()
    m = _INLINE_SUB_NUM_RE.match(stripped)
    if not m:
        return None
    new_num = m.group(1)
    if not _is_descendant_num(new_num, base_num):
        return None

    # Heading-shape filter: short block, few lines.
    lines = [ln for ln in text.strip().split('\n') if ln.strip()]
    if len(lines) > 4:
        return None
    collapsed = re.sub(r'\s+', ' ', text).strip()
    if len(collapsed) > 200:
        return None

    # The first line should be the number alone or "NUM Title" (not "NUM is ...").
    # We rule out body sentences by requiring the post-NUM tail (across all lines)
    # to look like a title — short, mostly a noun phrase, no terminal punctuation.
    tail = collapsed[m.end():].strip()
    if tail and tail.endswith(('.', ':', ';', ',')) and len(tail) > 60:
        # Long sentence ending in '.' is almost certainly body text, not a heading.
        return None

    return new_num


def _is_stop_heading(text, stop_nums, stop_titles=None):
    """Return True if text is a standalone section heading that ends the current section.

    Four checks:
    1. Numbered headings: text starts with a section number (e.g. '8.3.1') or
       'Annex A' that is in stop_nums.
    2. Unnumbered headings: any line in the text block matches a full TOC title
       in stop_titles (e.g. 'Scope', 'Classification').
    3. Split headings: some PDFs store headings as '1\\nScope' where the number
       and title are on separate lines within the same block.  Check if joining
       them (e.g. '1 Scope') matches a stop_title.
    4. Whitespace-collapsed match: collapse ALL whitespace (newlines, tabs,
       multiple spaces) in the text block to single spaces, then check if the
       collapsed text matches a stop_title — either directly or after stripping
       a leading section number.  This catches headings where newlines can
       appear between any words (e.g. 'Shape and\\ndimensions').
    """
    lines = [ln.strip() for ln in text.strip().split('\n') if ln.strip()]
    if not lines:
        return False
    first_line = lines[0]

    # Check 1: numbered / Annex headings (first line)
    m = _STOP_HDG_RE.match(first_line)
    if m and m.group(1).strip() in stop_nums:
        return True

    if stop_titles:
        # Check 2: any individual line matches a stop title
        for ln in lines:
            if ln in stop_titles:
                return True

        # Check 3: split heading — join first two lines and check
        # Handles blocks like '1\nScope' where the TOC title is 'Scope'
        # and also '1\nScope' where a stop_title might be '1 Scope'
        if len(lines) >= 2:
            joined = f"{lines[0]} {lines[1]}"
            if joined in stop_titles:
                return True

        # Check 4: collapse all whitespace and compare
        # Handles newlines between any words, e.g. 'Shape and\ndimensions'
        collapsed = re.sub(r'\s+', ' ', text).strip()
        if collapsed in stop_titles:
            return True
        # Also try after stripping a leading section number
        # e.g. '1 Scope' → 'Scope', '3.1 General' → 'General'
        stripped = re.sub(r'^\d+(?:\.\d+)*\s+', '', collapsed)
        if stripped != collapsed and stripped in stop_titles:
            return True

    return False
