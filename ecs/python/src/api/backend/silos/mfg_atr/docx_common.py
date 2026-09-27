# Ported from src/docx_common.py.
#
# Verbatim: this module imports only third-party libraries and its siblings, so it
# needed no platform seam changes. It writes to a filesystem path, so the calling
# stage materialises a scratch directory and reads the bytes back.
"""Shared Word-formatting helpers so the ATR/MFGR .docx output matches the PDFs.

reportlab and Word render differently by default (Word falls back to Calibri, black
grid borders, no shaded headers, auto column widths, no running header/footer). These
helpers close that gap: a sans-serif base font, navy headings, shaded table-header rows,
fixed column widths, page breaks, and a running header/footer with a live PAGE field —
so ``atr_docx`` / ``mfgr_docx`` can mirror their reportlab layouts closely.
"""
from __future__ import annotations

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, qn
from docx.shared import Mm, Pt, RGBColor

# Word 2010 checkbox content controls live in the w14 namespace; register it so qn()
# can resolve w14:* tags (older python-docx nsmaps don't include it).
nsmap.setdefault("w14", "http://schemas.microsoft.com/office/word/2010/wordml")

_CHECKED_GLYPH, _UNCHECKED_GLYPH = "☒", "☐"   # ☒ / ☐
_CHECKBOX_FONT = "MS Gothic"
_cb_id = [0x0A710000]   # monotonically increasing content-control ids

NAVY = RGBColor(0x07, 0x1D, 0x49)          # heading colour (matches PDF #071D49)
HEADER_FILL = "D8DEE9"                      # MFGR table header fill (PDF #D8DEE9)
BLUE_FILL = "BDD7EE"                        # ATR §4 matrix header fill (PDF #BDD7EE)
GREY_FILL = "D9D9D9"                        # ATR §3 methods header (PDF lightgrey)
ZEBRA_FILL = "F2F2F2"                       # ATR §4 zebra rows


def new_document(font: str = "Arial", size: float = 10) -> Document:
    """A Document whose Normal style uses the given sans-serif font/size (PDF uses
    Helvetica; Arial is its standard Word substitute)."""
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.font.size = Pt(size)
    # make the east-asian/hAnsi slots match so Word doesn't fall back to Calibri
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(attr), font)
    return doc


def configure_page(doc: Document, *, left=18, right=12, top=18, bottom=16) -> None:
    """Set page margins (mm) to match the PDF frame."""
    sec = doc.sections[0]
    sec.left_margin, sec.right_margin = Mm(left), Mm(right)
    sec.top_margin, sec.bottom_margin = Mm(top), Mm(bottom)


def heading(doc: Document, text: str, *, size: float = 13) -> None:
    """A navy bold section heading (h1=13, h2=11), matching the PDF heading colour."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.color.rgb = NAVY


def page_break(doc: Document) -> None:
    doc.add_page_break()


def grid(doc: Document, n_rows: int, n_cols: int):
    t = doc.add_table(rows=n_rows, cols=n_cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.LEFT
    return t


def shade_cell(cell, fill_hex: str) -> None:
    """Set a cell background fill (python-docx has no direct API for this)."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    tcPr.append(shd)


def set_cell(cell, text: str, *, bold: bool = False, size: float = 9,
             fill: str | None = None, center: bool = False) -> None:
    cell.text = ""
    para = cell.paragraphs[0]
    if center:
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run("" if text is None else str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if fill:
        shade_cell(cell, fill)


def shade_header_row(table, fill: str = HEADER_FILL) -> None:
    """Bold + shade the first row (the table header)."""
    for cell in table.rows[0].cells:
        shade_cell(cell, fill)
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True


def zebra_rows(table, *, start: int, fill: str = ZEBRA_FILL) -> None:
    """Shade alternating body rows (matches the PDF §4 zebra)."""
    for i in range(start, len(table.rows)):
        if (i - start) % 2 == 1:
            for cell in table.rows[i].cells:
                shade_cell(cell, fill)


def set_col_widths(table, widths_mm: list[float]) -> None:
    """Pin column widths (mm) so Word doesn't auto-size the table."""
    table.autofit = False
    table.allow_autofit = False
    for row in table.rows:
        for i, w in enumerate(widths_mm):
            if i < len(row.cells):
                row.cells[i].width = Mm(w)


def add_checkbox(paragraph, *, checked: bool = False) -> None:
    """Append a Word checkbox content control (clickable — toggles ☐/☒ in Word) to
    ``paragraph``. Falls back gracefully: older readers still show the glyph."""
    _cb_id[0] += 1
    sdt = OxmlElement("w:sdt")
    sdt_pr = OxmlElement("w:sdtPr")
    cid = OxmlElement("w:id"); cid.set(qn("w:val"), str(_cb_id[0]))
    checkbox = OxmlElement("w14:checkbox")
    checked_el = OxmlElement("w14:checked"); checked_el.set(qn("w14:val"), "1" if checked else "0")
    cs = OxmlElement("w14:checkedState")
    cs.set(qn("w14:val"), "2612"); cs.set(qn("w14:font"), _CHECKBOX_FONT)
    us = OxmlElement("w14:uncheckedState")
    us.set(qn("w14:val"), "2610"); us.set(qn("w14:font"), _CHECKBOX_FONT)
    checkbox.append(checked_el); checkbox.append(cs); checkbox.append(us)
    sdt_pr.append(cid); sdt_pr.append(checkbox)

    content = OxmlElement("w:sdtContent")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rfonts = OxmlElement("w:rFonts")
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), _CHECKBOX_FONT)
    rpr.append(rfonts)
    text = OxmlElement("w:t")
    text.text = _CHECKED_GLYPH if checked else _UNCHECKED_GLYPH
    run.append(rpr); run.append(text)
    content.append(run)

    sdt.append(sdt_pr); sdt.append(content)
    paragraph._p.append(sdt)


def add_page_field(paragraph) -> None:
    """Append a live PAGE field to a paragraph (renders the current page number)."""
    run = paragraph.add_run()
    r = run._r
    begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = "PAGE"
    end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end")
    r.append(begin); r.append(instr); r.append(end)


def _hf_paragraph(container):
    """First paragraph of a header/footer, cleared and ready to write."""
    p = container.paragraphs[0]
    p.text = ""
    return p


def set_centered_header(doc: Document, text: str, *, size: float = 7) -> None:
    p = _hf_paragraph(doc.sections[0].header)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(text).font.size = Pt(size)


def set_centered_footer(doc: Document, text: str, *, size: float = 7,
                        page_suffix: str | None = None) -> None:
    """Centered footer text; when ``page_suffix`` is given, append it + a PAGE field."""
    p = _hf_paragraph(doc.sections[0].footer)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(text).font.size = Pt(size)
    if page_suffix is not None:
        p.add_run(page_suffix).font.size = Pt(size)
        add_page_field(p)


def set_running_header(doc: Document, *, left: str, right: str,
                       title: str = "", subtitle: str = "",
                       width_mm: float = 183) -> None:
    """Multi-line running header (left/right split via a right tab stop), used by MFGR."""
    hdr = doc.sections[0].header
    p = _hf_paragraph(hdr)
    p.paragraph_format.tab_stops.add_tab_stop(Mm(width_mm), WD_TAB_ALIGNMENT.RIGHT)
    p.add_run(f"{left}\t").font.size = Pt(7)
    p.add_run(f"{right} ").font.size = Pt(7)
    add_page_field(p)
    if title:
        pt = hdr.add_paragraph()
        r = pt.add_run(title); r.bold = True; r.font.size = Pt(8)
    if subtitle:
        ps = hdr.add_paragraph()
        ps.add_run(subtitle).font.size = Pt(7)
