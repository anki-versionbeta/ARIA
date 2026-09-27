# Ported from src/pdf_builder.py.
#
# Verbatim: this module imports only third-party libraries and its siblings, so it
# needed no platform seam changes. It writes to a filesystem path, so the calling
# stage materialises a scratch directory and reads the bytes back.
#
# Note: generate_atr_pdf accepts generated_at and threads it into build_story, which never reads it. The module docstring also mentions an audit page that build_story does not produce. Both are pre-existing; ported as-is rather than tidied.
"""Render an ATR to a single PDF: read-only automated content + two editable sections.

Automated sections (Header, §1 Scope, §2 Summary & Conclusion, §3 Methods, §4 Results,
audit page) are drawn as static content — no form fields, so they are not editable in a
reader. The **Regulatory Report** and **HQC Assessment** sections are the only interactive
parts, rendered as **AcroForm** fields (a custom Flowable places each widget at its flowed
position via ``canvas.acroform`` with ``relative=True``). ``finalize.py`` later flattens
and locks the document.

Field names (asserted by tests): ``regulatory`` (radio Yes/No), ``hqc_spec_id`` (text),
``hqc_assessment`` (radio, 4 options), ``hqc_remarks`` (text), ``hqc_remarks_na`` (checkbox).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .atr_report import AtrReport

TEMPLATE_ID = "QPP05-01-024-T01, Version 5.0, Title: Analytical Report"

# AcroForm field names (the ONLY editable fields in the document).
F_REGULATORY = "regulatory"
F_SPEC_ID = "hqc_spec_id"
F_HQC = "hqc_assessment"
F_REMARKS = "hqc_remarks"
F_REMARKS_NA = "hqc_remarks_na"

HQC_OPTIONS = [
    ("meets", "Results meet acceptance criteria"),
    ("not_meets", "Results do not meet acceptance criteria (assessment required)"),
    ("not_spec", "Analyzed attributes are not part of the specification. Data was only generated "
                 "for development purposes and therefore don't compromise the quality of the product."),
    ("other", "Other"),
]


@dataclass
class ManualDefaults:
    """Optional pre-fill for the editable fields (normally left blank for the author)."""
    regulatory: str = ""      # "Yes" | "No" | ""
    spec_id: str = ""
    hqc: str = ""             # one of the HQC option keys
    remarks: str = ""
    remarks_na: bool = False


# --- styles -----------------------------------------------------------------
def _styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=9, leading=12)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10)
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold", fontSize=9)
    h = ParagraphStyle("h", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                       spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#071D49"))
    # Same look as `h`, but glued to the flowable that follows so a §4.x batch heading
    # can never be left stranded at the foot of a page while its table starts on the next.
    hkeep = ParagraphStyle("hkeep", parent=h, keepWithNext=1)
    return {"body": body, "cell": cell, "label": label, "h": h, "hkeep": hkeep}


def _fit_widths(widths, avail_mm):
    """Scale a column-width list down proportionally so the table never exceeds the
    frame. A table wider than the page makes reportlab abort layout, so this guards
    against the 'flowable too large' crash for wide result sets."""
    total_mm = sum(widths) / mm
    if avail_mm > 0 and total_mm > avail_mm:
        scale = avail_mm / total_mm
        return [w * scale for w in widths]
    return widths


def _effective_cols(bm, is_stability: bool) -> int:
    """Column count a batch matrix will actually render with — after the transpose rule.
    A transposed dev-sample table is Test + Parameter + one column per sample."""
    if not is_stability and len(bm.sub_headers) > len(bm.rows):
        return 2 + len(bm.rows)
    return len(bm.lead_headers) + len(bm.sub_headers)


# --- AcroForm widget as a Flowable ------------------------------------------
# AcroForm text fields render with the standard-14 font's WinAnsi (cp1252)
# encoding, and reportlab's escapePDF only handles codepoints 0-255. Any char
# beyond that (e.g. • U+2022, “ ” smart quotes, – — dashes, ≤ ≥) crashes field
# generation with a bare ``KeyError: <codepoint>``. Round-tripping through cp1252
# maps the WinAnsi-representable ones to their byte (so • still shows as a bullet)
# and replaces the rest with '?'.
def _winansi_safe(value):
    if not value:
        return value
    return str(value).encode("cp1252", "replace").decode("latin-1")


class AcroField(Flowable):
    """A single interactive form widget placed at its flowed position."""

    def __init__(self, kind, name, w, h, value="", checked=False, selected=False):
        super().__init__()
        self.kind, self.name = kind, name
        self.w, self.h = w, h
        self.value, self.checked, self.selected = _winansi_safe(value), checked, selected

    def wrap(self, avail_w, avail_h):
        if self.w is None:            # text fields expand to the available cell width
            self.w = max(40, avail_w)
        return self.w, self.h

    def draw(self):
        af = self.canv.acroForm
        common = dict(relative=True, borderColor=colors.black, fillColor=colors.white,
                      borderWidth=0.7)
        # All choice widgets render as a square box with a cross when set — same style as
        # the DOCX (☒/☐) and visible whether or not it is set (business feedback: unchecked
        # boxes must stay visible and every box must look the same).
        if self.kind == "checkbox":
            af.checkbox(name=self.name, x=0, y=0, size=self.h, checked=self.checked,
                        buttonStyle="cross", shape="square", **common)
        elif self.kind == "radio":
            af.radio(name=self.name, value=self.value, selected=self.selected, x=0, y=0,
                     size=self.h, buttonStyle="cross", shape="square",
                     fieldFlags="noToggleToOff", **common)
        elif self.kind == "text":
            af.textfield(name=self.name, value=self.value, x=0, y=0, width=self.w,
                         height=self.h, fontName="Helvetica", fontSize=9,
                         borderStyle="inset", **{k: v for k, v in common.items()
                                                 if k != "borderStyle"})


# --- automated (read-only) blocks -------------------------------------------
def _header_block(report: AtrReport, st) -> Table:
    rows = [
        [Paragraph("<b>ANALYTICAL REPORT</b>", st["label"]), ""],
        [Paragraph("<b>CMC Request ID:</b>", st["label"]), Paragraph(report.header["cmc_request_id"], st["body"])],
        [Paragraph("<b>Project Code:</b>", st["label"]), Paragraph(report.header["project_code"], st["body"])],
        [Paragraph("<b>Batch/Sample ID:</b>", st["label"]), Paragraph(report.header["batch_sample_id"], st["body"])],
    ]
    t = Table(rows, colWidths=[45 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("SPAN", (0, 0), (1, 0)), ("BACKGROUND", (0, 0), (1, 0), colors.whitesmoke),
        ("BACKGROUND", (0, 1), (0, -1), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _methods_table(report: AtrReport, st, section_no: int = 3) -> Table:
    head = [Paragraph(f"<b>{h}</b>", st["cell"]) for h in
            ("Test no.", "Test description", "Test method reference", "ELN Unique ID")]
    rows = [head]
    for i, m in enumerate(report.methods, start=1):
        rows.append([Paragraph(f"{section_no}.{i}", st["cell"]), Paragraph(m.technique, st["cell"]),
                     Paragraph(m.method_reference or "&mdash;", st["cell"]),
                     Paragraph(m.eln_unique_id or "", st["cell"])])
    t = Table(rows, colWidths=[16 * mm, 80 * mm, 50 * mm, 29 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _results_table(report: AtrReport, st, avail_mm: float):
    samples = report.sample_columns
    if not report.results:
        return Paragraph("No automated results available.", st["body"])
    head = [Paragraph("<b>Test</b>", st["cell"]), Paragraph("", st["cell"])]
    head += [Paragraph(f"<b>{s}</b>", st["cell"]) for s in samples]
    data = [head]
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black),
             ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("FONTSIZE", (0, 0), (-1, -1), 7),
             ("SPAN", (0, 0), (1, 0))]
    r = 1
    for tech in report.results:
        start = r
        for p in tech.params:
            label = tech.technique if p is tech.params[0] else ""
            row = [Paragraph(f"<b>{label}</b>", st["cell"]), Paragraph(p.label, st["cell"])]
            row += [Paragraph(str(p.values.get(s, "")), st["cell"]) for s in samples]
            data.append(row); r += 1
        if r - start > 1:
            style.append(("SPAN", (0, start), (0, r - 1)))
        style.append(("LINEBELOW", (0, r - 1), (-1, r - 1), 0.75, colors.black))
    n = len(samples)
    label_w = 32 + 30                                   # the two leading label columns (mm)
    sample_w = max(12, (avail_mm - label_w) / max(n, 1))
    col_widths = _fit_widths([32 * mm, 30 * mm] + [sample_w * mm] * n, avail_mm)
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


# --- editable blocks (AcroForm) ---------------------------------------------
def _regulatory_block(defaults: ManualDefaults, st) -> Table:
    stmt = ("<b>Regulatory report intended to support regulatory submissions, pre-approval "
            "inspections or GMP activities.</b> (only required for PDS&amp;T)")
    yes = [AcroField("radio", F_REGULATORY, 11, 11, value="Yes", selected=defaults.regulatory == "Yes"),
           Paragraph("Yes", st["cell"]),
           AcroField("radio", F_REGULATORY, 11, 11, value="No", selected=defaults.regulatory == "No"),
           Paragraph("No", st["cell"])]
    inner = Table([yes], colWidths=[7 * mm, 12 * mm, 7 * mm, 12 * mm])
    inner.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 1)]))
    t = Table([[Paragraph(stmt, st["cell"]), inner]], colWidths=[137 * mm, 38 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 5)]))
    return t


def _hqc_block(defaults: ManualDefaults, st) -> Table:
    rows = [[Paragraph("<b>HQC Assessment</b> (only required if clinical material with associated "
                       "specification was analyzed)", st["cell"]), ""]]
    rows.append([Paragraph("<b>Specification ID:</b>", st["cell"]),
                 AcroField("text", F_SPEC_ID, None, 12, value=defaults.spec_id)])
    for key, text in HQC_OPTIONS:
        widget = AcroField("radio", F_HQC, 11, 11, value=key, selected=defaults.hqc == key)
        opt = Table([[widget, Paragraph(text, st["cell"])]], colWidths=[8 * mm, 122 * mm])
        opt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 1)]))
        rows.append([opt, ""])
    na = AcroField("checkbox", F_REMARKS_NA, 10, 10, checked=defaults.remarks_na)
    remarks = Table([[Paragraph("<b>Remarks:</b>", st["cell"]), na, Paragraph("N/A", st["cell"]),
                      AcroField("text", F_REMARKS, None, 12, value=defaults.remarks)]],
                    colWidths=[18 * mm, 6 * mm, 10 * mm, 96 * mm])
    remarks.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 1)]))
    rows.append([remarks, ""])

    t = Table(rows, colWidths=[45 * mm, 130 * mm])
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black),
             ("BACKGROUND", (0, 0), (1, 0), colors.whitesmoke), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 3),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for i in (0, 2, 3, 4, 5, 6):  # rows that span both columns (heading + option/remarks rows)
        if i < len(rows):
            style.append(("SPAN", (0, i), (1, i)))
    t.setStyle(TableStyle(style))
    return t


def _stability_result_tables(report: AtrReport, st, avail_mm: float, section_no: int = 4):
    """§4 for stability ATRs: one matrix per batch, two-level grouped header
    (test -> sub-parameter), rows = timepoint / storage condition."""
    flows = []
    # No Actual-Value amber/legend in the PDF (business decision): Actual-sourced values
    # are surfaced as a pre-download review remark in the UI instead. The DOCX keeps its
    # amber shading. See build_atr_report, which raises the review flag.
    for i, bm in enumerate(report.batch_matrices, start=1):
        # `hkeep` glues the heading to the table, so the §4.x title never sits alone at
        # the bottom of a page (business feedback: the title must sit above its table).
        flows.append(Paragraph(f"<b>{section_no}.{i}&nbsp;&nbsp;Batch {bm.batch_id}</b>", st["hkeep"]))
        # Dev-sample ATRs have many measurements and few samples, so laying samples across
        # the columns overflows the page and shreds the headers mid-word. When there are
        # more measurements than samples, transpose (measurements down, samples across) so
        # it reads top-to-bottom in portrait. Stability ATRs (samples/timepoints as rows)
        # keep the original layout.
        if not report.is_stability and len(bm.sub_headers) > len(bm.rows):
            flows.append(_transposed_matrix_table(bm, st, avail_mm))
        else:
            flows.append(_wide_matrix_table(bm, st, avail_mm))
        flows.append(Spacer(1, 4 * mm))
    return flows


def _wide_matrix_table(bm, st, avail_mm) -> Table:
    """Samples-as-rows layout (stability ATRs): two-level grouped header, one row per
    sample/timepoint, one column per measurement."""
    n_lead, n_sub = len(bm.lead_headers), len(bm.sub_headers)

    row0 = [Paragraph(f"<b>{h}</b>", st["cell"]) for h in bm.lead_headers]
    for disp, span in bm.group_headers:
        row0.append(Paragraph(f"<b>{disp}</b>", st["cell"]))
        row0 += [Paragraph("", st["cell"])] * (span - 1)
    row1 = [Paragraph("", st["cell"]) for _ in range(n_lead)]
    row1 += [Paragraph(f"<b>{s}</b>", st["cell"]) for s in bm.sub_headers]
    data = [row0, row1] + [[Paragraph(str(x), st["cell"]) for x in r] for r in bm.rows]

    # lead-column widths: Sample is wide; Timepoint/Storage narrow. Works for any n_lead.
    _lead_defaults = {"Sample": 34 * mm, "Timepoint": 16 * mm, "Storage": 14 * mm}
    lead_w = [_lead_defaults.get(h, 22 * mm) for h in bm.lead_headers]
    sub_avail = avail_mm - sum(v / mm for v in lead_w)
    sub_w = max(9, sub_avail / max(n_sub, 1))
    col_widths = _fit_widths(lead_w + [sub_w * mm] * n_sub, avail_mm)

    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.black),
             ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#BDD7EE")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("FONTSIZE", (0, 0), (-1, -1), 6),
             ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
             ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    for c in range(n_lead):                       # lead headers span both header rows
        style.append(("SPAN", (c, 0), (c, 1)))
    col = n_lead                                  # group headers span their sub-columns
    for _disp, span in bm.group_headers:
        if span > 1:
            style.append(("SPAN", (col, 0), (col + span - 1, 0)))
        col += span
    for ri in range(2, len(data)):                # zebra data rows
        if (ri - 2) % 2 == 1:
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#F2F2F2")))
    # No amber for Actual-sourced cells in the PDF — surfaced as a UI remark instead.
    t = Table(data, colWidths=col_widths, repeatRows=2)
    t.setStyle(TableStyle(style))
    return t


def _transposed_matrix_table(bm, st, avail_mm) -> Table:
    """Measurements-as-rows layout (wide dev-sample ATRs): Test / Parameter down the left,
    one column per sample. Reads top-to-bottom in portrait instead of overflowing sideways,
    which is what the business asked for."""
    n_lead = len(bm.lead_headers)
    sample_labels = []
    for r in bm.rows:
        leads = [str(r[c]) for c in range(n_lead) if str(r[c]).strip()]
        sample_labels.append(" / ".join(leads) or "Sample")
    n_samp = len(sample_labels)

    # technique for each measurement column, expanded from the group-header spans
    tech_of_sub: list[str] = []
    for disp, span in bm.group_headers:
        tech_of_sub += [disp] * span

    header = [Paragraph("<b>Test</b>", st["cell"]), Paragraph("<b>Parameter</b>", st["cell"])]
    header += [Paragraph(f"<b>{s}</b>", st["cell"]) for s in sample_labels]
    data = [header]
    for j, sub in enumerate(bm.sub_headers):
        tech = tech_of_sub[j] if j < len(tech_of_sub) else ""
        row = [Paragraph(tech, st["cell"]), Paragraph(str(sub), st["cell"])]
        for si in range(n_samp):
            row.append(Paragraph(str(bm.rows[si][n_lead + j]), st["cell"]))
        data.append(row)

    test_w, param_w = 30 * mm, 58 * mm
    val_w = max(14, (avail_mm - (test_w + param_w) / mm) / max(n_samp, 1)) * mm
    col_widths = _fit_widths([test_w, param_w] + [val_w] * n_samp, avail_mm)

    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.black),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#BDD7EE")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("FONTSIZE", (0, 0), (-1, -1), 7),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
             ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]
    for ri in range(1, len(data)):                # zebra by measurement row
        if (ri - 1) % 2 == 1:
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#F2F2F2")))
    # No amber for Actual-sourced cells in the PDF — surfaced as a UI remark instead.
    # merge the Test column over consecutive rows of the same technique group
    r = 1
    for _disp, span in bm.group_headers:
        if span > 1:
            style.append(("SPAN", (0, r), (0, r + span - 1)))
            for rr in range(r + 1, r + span):     # blank the repeated technique labels
                data[rr][0] = Paragraph("", st["cell"])
        r += span

    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    w, h = doc.pagesize
    canvas.drawCentredString(w / 2, h - 12 * mm,
                             "OneV-1327075 v 5.0  Effective on 09 Feb 2026  AbbVie Confidential General")
    canvas.drawCentredString(w / 2, 10 * mm, f"Template Name/ID: {TEMPLATE_ID}   |   Pg {doc.page}")
    canvas.restoreState()


def build_story(report: AtrReport, defaults: ManualDefaults, generated_at: str | None,
                frame_w_mm: float):
    st = _styles()
    story = [
        Paragraph(f"Document Title: NBE_AR_Analytical Report - {report.header['project_code']} - "
                  f"{report.request_id_display}", st["body"]),
        Spacer(1, 4 * mm), _header_block(report, st),
        Spacer(1, 4 * mm), _regulatory_block(defaults, st),
        Spacer(1, 4 * mm), _hqc_block(defaults, st),
        Spacer(1, 5 * mm),
    ]
    # §1 Scope and §2 Summary and Conclusion are omitted when the warehouse holds no
    # entry (they previously rendered an empty "—"). Remaining sections renumber so the
    # sequence never has a gap; Methods and Results are always present.
    n = 0
    if report.scope:
        n += 1
        story += [Paragraph(f"{n} SCOPE", st["h"]), Paragraph(report.scope, st["body"])]
    if report.summary_conclusion:
        n += 1
        story += [Paragraph(f"{n} SUMMARY AND CONCLUSION", st["h"]),
                  Paragraph(report.summary_conclusion, st["body"])]
    n += 1
    story += [Paragraph(f"{n} ANALYTICAL METHODS", st["h"]), _methods_table(report, st, n)]
    n += 1
    story.append(Paragraph(f"{n} RESULTS", st["h"]))
    if report.batch_matrices:
        story += _stability_result_tables(report, st, frame_w_mm, n)  # one matrix per batch
    else:
        story.append(_results_table(report, st, frame_w_mm))          # fallback if no rows shaped
    return story


def generate_atr_pdf(report: AtrReport, out_path: str | Path, *,
                     defaults: ManualDefaults | None = None,
                     generated_at: str | None = None) -> Path:
    """Render the fillable ATR PDF to ``out_path`` and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Stability matrices are wide; dev-samples reports with many sample columns also
    # overflow portrait, so switch them to landscape too. Count columns *after* the
    # transpose rule so a transposed dev-sample table stays portrait.
    _matrix_cols = max((_effective_cols(bm, report.is_stability) for bm in report.batch_matrices),
                       default=0)
    wide = report.is_stability or len(report.sample_columns) > 8 or _matrix_cols > 8
    pagesize = landscape(A4) if wide else A4
    frame_w_mm = pagesize[0] / mm - 18 - 12 - 8   # usable width minus margins & frame padding
    doc = SimpleDocTemplate(str(out_path), pagesize=pagesize, leftMargin=18 * mm, rightMargin=12 * mm,
                            topMargin=18 * mm, bottomMargin=16 * mm,
                            title=f"ATR {report.request_id_display}")
    doc.build(build_story(report, defaults or ManualDefaults(), generated_at, frame_w_mm),
              onFirstPage=_header_footer, onLaterPages=_header_footer)
    return out_path
