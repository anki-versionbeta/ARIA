"""Export a cap-colour recommendation to a Word (.docx) that mirrors the web interface.

The web shows each colour as a SWATCH CHIP — a small square colour block with the name, supplier and
ΔE beneath it — in a wrapping grid. `build(...)` reproduces that as one fixed-layout table per section
whose columns are tiles (row 0 = the chip, rows 1–3 = name / supplier / ΔE, all left-aligned).

**The chip is a generated PNG, not a shaded cell, and that is deliberate.** Cell shading fills the
whole cell, so the swatch is always as wide as the column its LABEL needs — which is why this export
previously rendered one merged full-width band instead of separate squares. An inline picture's
`wp:extent` is absolute EMU, so the chip stays a small square whatever the table's layout, grid or
cell widths do, and the bug cannot regress if a later edit disturbs a table flag. It also lets a white
or hex-less colour carry a visible border and the web's 45° hatch, neither of which shading can do.

UI-agnostic + import-safe; used by the router's `/cap-export` endpoint.
"""
import io
from functools import lru_cache

import fitz                                 # PyMuPDF. Module scope is safe and costs nothing new:
                                            # ARIA declares PyMuPDF, and extract_docs.py already
                                            # imports fitz at module scope on the router's import
                                            # chain — so this cannot reproduce the openpyxl
                                            # silent-router-death failure.
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_ROW_HEIGHT_RULE

# 4 chips × 1.50in = 6.00in = exactly the default template's text width (Letter, 1.25in side margins).
# Sized for the LABEL, not the chip: that is where the white space between chips comes from. Four
# rather than the web's six because "Magenta 2063C (L9320)" needs ~1.3in at 8pt and would wrap at 1.0in.
_CHIPS_PER_ROW = 4
_COL_IN = 1.50
_CHIP_IN = 0.36                             # web `small` (36 CSS px @ 96px/in)
_CHIP_IN_LARGE = 0.50                       # web `large` (56 CSS px), the recommended grid
_CHIP_DPI = 200

_MUTED = RGBColor(0x63, 0x63, 0x63)         # Unity --un-text-muted
_GREEN = RGBColor(0x33, 0x87, 0x00)         # Unity --un-success-default
_BORDER_RGB = (0xD5 / 255, 0xD3 / 255, 0xD3 / 255)    # Unity --un-border-color-container
_HATCH_RGB = (0xF1 / 255, 0xF3 / 255, 0xFF / 255)     # Unity --un-background-02
_RISK_RGB = {"High": RGBColor(0xCF, 0x45, 0x1C),      # Unity filled-Tag error
             "Medium": RGBColor(0xBB, 0x5A, 0x00),    # ...          warning
             "Low": RGBColor(0x33, 0x87, 0x00)}       # ...          success

# CT_TblPr enforces child order. Indices from its _tag_seq: w:tblBorders = 10, w:tblLayout = 12,
# w:tblLook = 14 — so tblBorders must be INSERTED BEFORE its successors, never appended (appending
# lands it after w:tblLook and produces schema-invalid XML).
_TBLBORDERS_SUCCESSORS = ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
                          "w:tblCaption", "w:tblDescription", "w:tblPrChange")


def _rgb01(hexv):
    """'#RRGGBB' → (r, g, b) floats 0–1, or None when there is no solid hex."""
    h = (hexv or "").lstrip("#").strip()
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None


@lru_cache(maxsize=256)
def _chip_png(hexv, size_in):
    """A square swatch chip as PNG bytes.

    Solid fill, or the web's 45° hatch when there is no hex, plus a 1px container border so a white
    or transparent chip is still visible against white paper. Cell shading can do neither: a white
    fill is invisible, and a missing hex used to become flat #EEEEEE, which reads as a real grey cap
    rather than "unknown".
    """
    side = size_in * 72.0
    doc = fitz.open()
    try:
        page = doc.new_page(width=side, height=side)
        rect = fitz.Rect(0.5, 0.5, side - 0.5, side - 0.5)
        fill = _rgb01(hexv)
        if fill is None:
            # band = side/6, period = side/3 → equal bands and gaps, the web's 6px/12px on a 36px chip.
            band, period = side / 6.0, side / 3.0
            sh = page.new_shape()
            n = int(side / period) + 2
            for k in range(-n, n + 1):
                x = k * period
                sh.draw_line(fitz.Point(rect.x0 + x, rect.y0),
                             fitz.Point(rect.x0 + x + rect.height, rect.y1))
            sh.finish(color=_HATCH_RGB, width=band)
            sh.commit()
        sh = page.new_shape()
        sh.draw_rect(rect)
        # fill=None strokes only, so the hatch beneath stays visible.
        sh.finish(fill=fill, color=_BORDER_RGB, width=0.75)
        sh.commit()
        return page.get_pixmap(dpi=_CHIP_DPI).tobytes("png")
    finally:
        doc.close()


def _fix_layout(table, ncols):
    """Pin the table so Word cannot re-size it. ALL FOUR of these are required.

    1. `w:tblLayout w:type="fixed"` — via `table.autofit = False`. Without it Word runs the AUTO
       layout algorithm and treats `w:tcW` as a mere hint. Use the property rather than a hand-built
       element: `get_or_add_tblLayout()` inserts it in schema order.
    2. `w:tblW w:type="dxa"` — `add_table` emits `type="auto" w="0"` and python-docx exposes no
       setter, so mutate the existing element in place (it is already correctly positioned).
    3. `w:gridCol/@w:w` — via `column.width`. This is the one auto layout was overriding, because
       `add_table` sizes the grid to the full text width divided by the column count.
    4. `w:tcW` — via `cell.width`.

    Setting only (4) was the original bug: the code asked for 1.05in cells while the grid still said
    6.0in/N, and under auto layout the grid wins.
    """
    table.autofit = False
    tblPr = table._tbl.tblPr
    tblW = tblPr.find(qn("w:tblW"))
    if tblW is not None:                     # add_table always supplies one
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(int(round(_COL_IN * ncols * 1440))))
    for col in table.columns:
        col.width = Inches(_COL_IN)
    for row in table.rows:
        for cell in row.cells:
            cell.width = Inches(_COL_IN)


def _no_borders(table):
    """Belt-and-braces: no gridlines, whatever default table style is in force."""
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "none")
        borders.append(e)
    table._tbl.tblPr.insert_element_before(borders, *_TBLBORDERS_SUCCESSORS)


def _line(cell, text, size=8, bold=False, color=None):
    """One tight, LEFT-aligned line of tile text.

    Empty text still resets the paragraph, so the padding cells of a short final row do not inherit
    Normal's spacing. Zeroing the spacing is what keeps the four stacked cells reading as one compact
    tile instead of a loose column.
    """
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    pf = p.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(0), 1.0
    if not text:
        return p
    r = p.add_run(text)
    r.font.size = Pt(size)
    r.bold = bold
    if color is not None:
        r.font.color.rgb = color
    return p


def _de(v):
    """The web's ΔE line. `format(…, "g")` matches the JS template literal: 96.6 → "ΔE 96.6",
    49.0 → "ΔE 49". Plain `str()` would wrongly render "ΔE 49.0"."""
    if v is None:
        return "free"
    try:
        return "ΔE " + format(float(v), "g")
    except (TypeError, ValueError):
        return "ΔE " + str(v)


def _chip_grid(doc, items, chip_in=_CHIP_IN):
    """Render a wrapping grid of swatch chips, mirroring the web tile.

    `items` are dicts: `hex`, `name`, `supplier`, `tail`, and optionally `tail_rgb` (colour + bold the
    tail — used for the risk grade), `badge` (an extra green line above the tail) and `bold` (green
    bold name, the reserved first choice).

    Always emits `_CHIPS_PER_ROW` columns even for a short final chunk, so its labels stay aligned
    with the rows above. Using `len(chunk)` is why a 10-item recommended list previously produced a
    6-column table followed by a 4-column one, with visibly different chip widths.
    """
    if not items:
        return
    for start in range(0, len(items), _CHIPS_PER_ROW):
        chunk = items[start:start + _CHIPS_PER_ROW]
        t = doc.add_table(rows=4, cols=_CHIPS_PER_ROW)
        _no_borders(t)
        _fix_layout(t, _CHIPS_PER_ROW)
        t.rows[0].height = Inches(chip_in + 0.08)
        # AT_LEAST, never EXACTLY: an exact rule CLIPS an inserted picture.
        t.rows[0].height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        for j in range(_CHIPS_PER_ROW):
            for ri in range(4):
                _line(t.cell(ri, j), "")
            if j >= len(chunk):
                continue
            it = chunk[j]
            first = bool(it.get("bold"))
            _line(t.cell(0, j), "").add_run().add_picture(
                io.BytesIO(_chip_png(it.get("hex") or "", chip_in)),
                width=Inches(chip_in), height=Inches(chip_in))
            _line(t.cell(1, j), it.get("name"), 8, bold=first, color=(_GREEN if first else None))
            _line(t.cell(2, j), it.get("supplier"), 8, color=_MUTED)
            tail_cell = t.cell(3, j)
            if it.get("badge"):
                _line(tail_cell, it["badge"], 8, bold=True, color=_GREEN)
                p = tail_cell.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                pf = p.paragraph_format
                pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(0), 1.0
                r = p.add_run(it.get("tail") or "")
                r.font.size = Pt(8)
                r.bold = bool(it.get("tail_rgb"))
                r.font.color.rgb = it.get("tail_rgb") or _MUTED
            else:
                _line(tail_cell, it.get("tail"), 8, bold=bool(it.get("tail_rgb")),
                      color=it.get("tail_rgb") or _MUTED)
    doc.add_paragraph()


def _caption(doc, text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.italic = True
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    return p


def build(path, res, program_label, presentation_label):
    """Write the recommendation `res` to `path` as a .docx mirroring the web view. Returns path."""
    doc = Document()
    doc.add_heading(program_label or "Cap Colour Recommendation", level=1)

    bar = doc.add_paragraph()
    bar.add_run(f"Presentation: {presentation_label}").bold = True

    subj = res.get("subject", {}) or {}
    _caption(doc, f"Vial size: {subj.get('vial_size', '?')}   ·   State: {subj.get('state') or '—'}   ·   "
                  f"Manufacturing site(s): {', '.join(res.get('sites') or []) or '—'}   ·   "
                  f"{res.get('n_colocated', 0)} same-state co-located product(s)   ·   "
                  f"Seal manufacturer: {res.get('vendor', '')}")

    # ---- current cap / already selected ----
    sel = res.get("subject_selected")
    caps_display = subj.get("caps_display") or []
    if sel:
        doc.add_heading("✓ Already selected (current cap)", level=2)
        # No tail: the heading already says it. The web dropped that duplicated sublabel too.
        _chip_grid(doc, [{"hex": sel.get("hex"),
                          "name": sel.get("raw") or sel.get("canonical") or "—",
                          "supplier": "", "tail": ""}])
    elif caps_display:
        doc.add_heading("Current cap", level=2)
        _chip_grid(doc, [{"hex": c.get("hex"), "name": c.get("raw") or "—",
                          "supplier": "", "tail": "current"} for c in caps_display])
    else:
        doc.add_heading("Current cap", level=2)
        doc.add_paragraph("none recorded")

    if not res.get("sites"):
        _caption(doc, res.get("note", ""))
        doc.save(path)
        return path

    # ---- recommended (chip grid; unique first choice highlighted) ----
    doc.add_heading("✓ Recommended  (most visually distinct first)", level=2)
    rec = res.get("recommended") or []
    first_unique = res.get("first_unique")
    if rec:
        items = []
        for i, e in enumerate(rec):
            reserved = bool(i == 0 and first_unique
                            and e.get("vendor_color_name") == first_unique)
            supplier = e.get("vendor") or ""
            # `is False` on purpose: None means unknown, and must not gain the suffix.
            if e.get("off_the_shelf") is False:
                supplier = (supplier + " · not stock").strip()
            items.append({"hex": e.get("hex"),
                          "name": e.get("vendor_color_name") or "",
                          "supplier": supplier,
                          "tail": _de(e.get("min_delta_e")),
                          "badge": "1st choice · unique" if reserved else None,
                          "bold": reserved})
        _chip_grid(doc, items, chip_in=_CHIP_IN_LARGE)
    else:
        doc.add_paragraph("No off-the-shelf colour is free — review.")

    # ---- highest similarity risks: the already-utilised caps, graded (chip grid + metadata bullets) ----
    doc.add_heading("Highest similarity risks", level=2)
    taken = res.get("taken") or []
    if taken:
        _caption(doc, "Caps already in use at these site(s), ranked by how easily each could be mixed up "
                      "with this presentation — the two drivers are the same vial size and a similar shade "
                      "of cap colour.")
        items = []
        for t in taken:
            colour, raw = t.get("color"), t.get("raw")
            if raw and raw != colour:
                sub = raw + (" · custom" if t.get("is_custom") else "")
            else:
                sub = "custom" if t.get("is_custom") else ""
            grade = t.get("risk")
            items.append({"hex": t.get("hex"), "name": colour or "—", "supplier": sub,
                          "tail": (f"{grade} similarity risk" if grade else ""),
                          "tail_rgb": _RISK_RGB.get(grade)})
        _chip_grid(doc, items)
        for t in taken:                      # the per-presentation metadata behind each grade
            de = t.get("delta_e_to_subject")
            doc.add_paragraph(
                f"{t.get('risk') or '?'} — {t.get('product')}  ·  "
                f"vial {t.get('vial_size') or t.get('vial_key') or '—'}"
                + ("  (same as this presentation)" if t.get("same_vial") else "")
                + f"  ·  cap {t.get('raw') or t.get('color') or '—'}"
                + (f"  ·  state {t.get('state')}" if t.get("state") else "")
                + (f"  ·  ΔE {de} to the current cap" if de is not None else "  ·  shade not compared")
                + f". {t.get('risk_reason') or ''}", style="List Bullet")
    else:
        doc.add_paragraph("No cap is recorded on any co-located, same-state presentation.")

    # ---- discouraged (chip grid + the reason as bullets, which will not fit a tile) ----
    doc.add_heading("✗ Discouraged  (same colour × vial size)", level=2)
    disc = res.get("discouraged") or []
    if disc:
        items = []
        for e in disc:
            supplier = e.get("vendor") or ""
            if e.get("off_the_shelf") is False:
                supplier = (supplier + " · not stock").strip()
            items.append({"hex": e.get("hex"), "name": e.get("vendor_color_name") or "",
                          "supplier": supplier, "tail": _de(e.get("min_delta_e"))})
        _chip_grid(doc, items)
        for e in disc:                       # the reason is a sentence; it cannot fit a 1.5in tile
            doc.add_paragraph(f"{e.get('vendor_color_name')} — {e.get('reason')}", style="List Bullet")
    else:
        doc.add_paragraph("None.")

    _caption(doc, res.get("note", ""))
    doc.save(path)
    return path
