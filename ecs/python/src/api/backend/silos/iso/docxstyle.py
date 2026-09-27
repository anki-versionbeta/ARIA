"""python-docx and OOXML primitives. Ported from backend.py:3309-3325 and 5019-5151.

The generated report is a two-column table — section number on the left, description on
the right — so these helpers are what give it borders, shading, fixed column widths and
in-cell images. Every measurement here is load-bearing for how the document looks, and
none of it is adjustable: the widths are chosen to total the printable width of A4 with
2.54 cm margins, and the shades distinguish the header row from a section divider row.

`_set_picture_alt_text` matters more than it looks. Formula images carry their LaTeX
source as alt text, which is what makes an equation searchable in Word's alt-text pane
and survives a re-export — the image itself is just pixels.

No seam changes in this module; it touches neither the network nor the filesystem.
"""

from __future__ import annotations

import io

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from PIL import Image as PILImage

from .patterns import _xml_safe


def _set_picture_alt_text(run, alt_text):
    """Set descr/title (alt text) on the most recently added picture in *run*."""
    if not alt_text:
        return
    WP_NS = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
    try:
        inlines = run._element.findall(f'.//{{{WP_NS}}}inline')
        if not inlines:
            return
        docPr = inlines[-1].find(f'{{{WP_NS}}}docPr')
        if docPr is None:
            return
        # descr is the real alt text; title is a short label (trim long LaTeX).
        docPr.set('descr', alt_text)
        docPr.set('title', (alt_text[:120] + '…') if len(alt_text) > 120 else alt_text)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# DOCX GENERATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Column widths in twips (1 inch = 1440 twips)
# Content area ≈ 6.27" on A4 with 2.54 cm margins each side
_COL_W = [
    int(1.5  * 1440),   # Section Number
    int(4.77 * 1440),   # Description
]   # total ≈ 6.27"

_HDR_SHADE  = 'C0C0C0'   # header row
_SEC_SHADE  = 'D9D9D9'   # section divider row
_FONT_SIZE  = Pt(9)
_FONT_SMALL = Pt(8)


def _cell_border(cell, color='000000', sz='6'):
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = OxmlElement('w:tcBorders')
    for side in ('top', 'left', 'bottom', 'right'):
        el = OxmlElement(f'w:{side}')
        el.set(qn('w:val'), 'single')
        el.set(qn('w:sz'), sz)
        el.set(qn('w:space'), '0')
        el.set(qn('w:color'), color)
        tcBorders.append(el)
    tcPr.append(tcBorders)


def _cell_shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill)
    tcPr.append(shd)


def _cell_width(cell, twips):
    tcPr = cell._tc.get_or_add_tcPr()
    tcW = OxmlElement('w:tcW')
    tcW.set(qn('w:w'), str(int(twips)))
    tcW.set(qn('w:type'), 'dxa')
    tcPr.append(tcW)


def _row_min_height(row, twips):
    trPr = row._tr.get_or_add_trPr()
    trH = OxmlElement('w:trHeight')
    trH.set(qn('w:val'), str(int(twips)))
    trH.set(qn('w:hRule'), 'atLeast')
    trPr.append(trH)


def _cell_font(cell, bold=False, size=_FONT_SIZE, italic=False):
    for para in cell.paragraphs:
        for run in para.runs:
            run.font.size  = size
            run.font.bold  = bold
            run.font.italic = italic


def _add_image_to_cell(cell, img_bytes, caption='', max_w_in=4.5, alt_text=''):
    """Append an inline image (+ optional caption) to a table cell.

    *alt_text*: if provided, set as the image's alt-text (descr attribute).
    For formulas we pass the LaTeX source so it's accessible / searchable
    in Word's alt-text pane and preserved when the doc is re-exported.
    """
    # caption / alt_text can originate from LLM output (vision path); strip
    # XML-illegal control chars so the caption paragraph and alt-text attr
    # never crash the serializer.
    caption  = _xml_safe(caption)
    alt_text = _xml_safe(alt_text)
    try:
        # Images captured at 200 DPI — use correct DPI for accurate display size.
        # Fix E: No pixel-level splitting. prescan_media now stores one PNG per
        # PDF page (Fix B), so each call to this function receives exactly one
        # page's worth of content — rows are always complete, never cut in half.
        # We just scale the image to fit within the column width AND page height.
        _CAP_DPI      = 200
        _MAX_H_IN     = 6.5   # max display height (fits A4 portrait with margins)

        with PILImage.open(io.BytesIO(img_bytes)) as pil_img:
            img_w_px, img_h_px = pil_img.size

        w_in = img_w_px / _CAP_DPI
        h_in = img_h_px / _CAP_DPI

        # Scale down to fit column width first (preserve aspect ratio).
        if w_in > max_w_in:
            scale = max_w_in / w_in
            w_in  = max_w_in
            h_in *= scale

        # Then scale down further if the image is still too tall for one page.
        if h_in > _MAX_H_IN:
            scale = _MAX_H_IN / h_in
            h_in  = _MAX_H_IN
            w_in *= scale

        img_para = cell.add_paragraph()
        img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        img_run = img_para.add_run()
        img_run.add_picture(io.BytesIO(img_bytes), width=Inches(w_in))
        if alt_text:
            _set_picture_alt_text(img_run, alt_text)

        if caption:
            cap_p = cell.add_paragraph(caption)
            cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if cap_p.runs:
                cap_p.runs[0].font.italic = True
                cap_p.runs[0].font.size   = _FONT_SMALL
    except Exception as exc:
        # A single unrenderable image must not lose the whole report; the cell says so
        # instead, which is also how a reviewer finds it.
        cell.add_paragraph(f'[{caption or "Image"}: rendering failed — {exc}]')


def _apply_fixed_cols(table_xml, widths):
    """Stamp fixed-layout + tblGrid column widths onto a table's XML."""
    tblPr = table_xml.find(qn('w:tblPr'))
    if tblPr is None:
        tblPr = OxmlElement('w:tblPr')
        table_xml.insert(0, tblPr)

    lyt = OxmlElement('w:tblLayout')
    lyt.set(qn('w:type'), 'fixed')
    tblPr.append(lyt)

    tblGrid = OxmlElement('w:tblGrid')
    for w in widths:
        gc = OxmlElement('w:gridCol')
        gc.set(qn('w:w'), str(int(w)))
        tblGrid.append(gc)
    table_xml.insert(1, tblGrid)
