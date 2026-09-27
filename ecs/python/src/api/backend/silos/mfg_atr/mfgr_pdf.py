# Ported verbatim from src/mfgr_pdf_builder.py.
#
# Renders to a path with reportlab and returns it; the stage gives it a scratch
# workspace and reads the bytes back. Unchanged - it imports only reportlab and its own
# shaped report, so there was nothing to reseam.
"""Render an MFGR PDF matching template A-RDLU-000087-T02, Version 1.0.

Sections (per the controlled template):
  Title page + Signature page + Table of Contents
  1  ABSTRACT
  2  DESCRIPTION OF CHANGE
  3  TOPIC
      Table 1 — General information
  4  COMPOSITION OF THE FORMULATION
      Table 2 — Composition of the bulk solution
  5  PROCESS FLOW CHART              (image placeholder — author supplies)
  6  PROCEDURE / OBSERVATIONS
  7  DEVELOPMENT SAMPLES RESULTS
  8  COMPATIBILITY / HOLD TIME SAMPLE RESULTS
  9  CONCLUSION
  10 REFERENCES

Read-only (auto-filled) values come from the report object. The 8 MANUAL fields
identified in the RepBatch Data Mapping — clinical phase, DS quality, site, form,
storage temp, DP dose (lyo), fill volume, closing conclusion + narrative sections —
are rendered as AcroForm text widgets. ``finalize.py`` later flattens and locks them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .mfgr_report import MfgrReport

TEMPLATE_ID = "A-RDLU-000087-T02, Version 1.0"

# AcroForm field names — the ONLY editable fields in the document.
F_CLINICAL_PHASE = "clinical_phase"
F_DS_QUALITY = "ds_quality"
F_DS_SITE = "ds_site"
F_DS_FORM = "ds_form"
F_DS_CHARACTERISTICS = "ds_characteristics"
F_STORAGE_TEMP = "storage_temp"
F_FILL_VOLUME = "fill_volume"
F_DOSE_LYO = "dose_lyo"
F_RECON_MEDIUM = "recon_medium"
F_RECON_VOLUME = "recon_volume"
F_RECON_CONC = "recon_concentration"
F_FORMAT = "format_desc"
F_STOPPER = "stopper_desc"
F_CRIMP = "crimp_desc"
F_DEV_SAMPLES = "dev_samples"
F_COMPATIBILITY = "compatibility"
F_CONCLUSION = "conclusion"
F_PROCEDURE_NOTES = "procedure_notes"
F_TOPIC_NOTES = "topic_notes"


@dataclass
class ManualDefaults:
    """Optional pre-fill for the editable fields (normally blank for the author)."""
    clinical_phase: str = ""
    ds_quality: str = ""
    ds_site: str = ""
    ds_form: str = ""
    ds_characteristics: str = ""
    storage_temp: str = ""
    fill_volume: str = ""
    dose_lyo: str = ""
    recon_medium: str = ""
    recon_volume: str = ""
    recon_concentration: str = ""
    format_desc: str = ""
    stopper_desc: str = ""
    crimp_desc: str = ""
    dev_samples: str = ""
    compatibility: str = ""
    conclusion: str = ""
    procedure_notes: str = ""
    topic_notes: str = ""


# --- styles -----------------------------------------------------------------
def _styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=10, leading=13)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    cell = ParagraphStyle("cell", parent=body, fontSize=9, leading=11)
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold", fontSize=10)
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                        fontSize=13, spaceBefore=14, spaceAfter=6,
                        textColor=colors.HexColor("#071D49"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                        fontSize=11, spaceBefore=8, spaceAfter=4,
                        textColor=colors.HexColor("#071D49"))
    title = ParagraphStyle("title", parent=body, fontName="Helvetica-Bold",
                           fontSize=14, leading=17, alignment=1)
    return {"body": body, "small": small, "cell": cell, "label": label,
            "h1": h1, "h2": h2, "title": title}


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


# --- AcroForm widget as a Flowable ------------------------------------------
# When ``AcroField.flatten`` is True (set by generate_mfgr_pdf(flatten=True)),
# the flowable draws the value as plain text instead of an interactive widget.
# The flat variant is what the DOCX pipeline uses, because pdf2docx cannot
# extract PDF widget values — flattening them to text makes the Word output
# populated and normally editable.
class AcroField(Flowable):
    """Interactive form widget placed at its flowed position (canvas.acroForm)."""

    flatten: bool = False  # class-level toggle, flipped per-render

    def __init__(self, name, w, h, value="", multiline=False):
        super().__init__()
        self.name = name
        self.w, self.h = w, h
        self.value = _winansi_safe(value)
        self.multiline = multiline

    def wrap(self, avail_w, avail_h):
        if self.w is None:
            self.w = max(40, avail_w)
        return self.w, self.h

    def draw(self):
        canv = self.canv
        if AcroField.flatten:
            # Draw the value as plain text so pdf2docx converts it to normal
            # editable Word text. Empty widgets become a light underline so
            # the reviewer can still see there was a fillable spot.
            canv.setFont("Helvetica", 9)
            canv.setFillColor(colors.HexColor("#0A1F44"))
            if self.value:
                text = str(self.value)
                if self.multiline:
                    y_line = self.h - 10
                    for line in text.splitlines() or [text]:
                        canv.drawString(2, y_line, line)
                        y_line -= 11
                else:
                    canv.drawString(2, 3, text)
            else:
                canv.setStrokeColor(colors.HexColor("#BBBBBB"))
                canv.setLineWidth(0.4)
                canv.line(2, 1, self.w - 2, 1)
            return
        # acroForm.textfield uses raw page coordinates and ignores the CTM,
        # so we resolve (0,0) through the current transformation matrix to
        # get the flowed position of the Flowable on the physical page.
        m = canv._currentMatrix
        x, y = m[4], m[5]
        canv.acroForm.textfield(name=self.name, value=self.value, x=x, y=y,
                                width=self.w, height=self.h,
                                fontName="Helvetica", fontSize=9,
                                borderColor=colors.HexColor("#888888"),
                                fillColor=colors.HexColor("#FFFFF8"),
                                borderWidth=0.5, borderStyle="inset",
                                fieldFlags="multiline" if self.multiline else "")


def _f(name, defaults_val, w=None, h=12, multiline=False):
    """Shortcut for an inline AcroField."""
    return AcroField(name, w, h, value=defaults_val or "", multiline=multiline)


# --- title / signature / TOC pages ------------------------------------------
def _title_page(report: MfgrReport, st) -> list:
    h = report.header
    title = (f"Manufacturing of Representative Batch {h.batch_id_display} "
             f"of {h.project_code}")
    subtitle = h.title_line or ""
    return [
        Spacer(1, 60 * mm),
        Paragraph(h.project_code or "&lt;ABT- or ABBV-Project&gt;", st["title"]),
        Spacer(1, 6 * mm),
        Paragraph(title, st["title"]),
        Spacer(1, 4 * mm),
        Paragraph(subtitle or "&lt;Dose&gt; mg &lt;S.INJ&gt; &lt;Volume&gt; mL in &lt;PFS or Vial&gt;",
                  st["title"]),
        Spacer(1, 20 * mm),
        Paragraph(f"Template: {TEMPLATE_ID}", st["body"]),
    ]


def _signature_page(st) -> list:
    def sig_row(label):
        return [Paragraph(f"<b>{label}</b>", st["body"]),
                Paragraph("________________________________________", st["body"]),
                Paragraph("____________________", st["body"])]
    t = Table(
        [
            [Paragraph("", st["body"]), Paragraph("<b>Name</b>", st["small"]),
             Paragraph("<b>Date</b>", st["small"])],
            sig_row("Compiled"), sig_row("Verified"), sig_row("Approved"),
        ],
        colWidths=[25 * mm, 100 * mm, 45 * mm],
    )
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return [Spacer(1, 40 * mm), Paragraph("<b>Signatures</b>", st["h1"]),
            Spacer(1, 6 * mm), t]


def _toc(st) -> list:
    rows = [
        ("1 ABSTRACT", "4"), ("2 DESCRIPTION OF CHANGE", "4"),
        ("3 TOPIC", "4"), ("4 COMPOSITION OF THE FORMULATION", "7"),
        ("5 PROCESS FLOW CHART", "8"), ("6 PROCEDURE / OBSERVATIONS", "9"),
        ("7 DEVELOPMENT SAMPLES RESULTS", "9"),
        ("8 COMPATIBILITY / HOLD TIME SAMPLES", "9"),
        ("9 CONCLUSION", "10"), ("10 REFERENCES", "10"),
    ]
    t = Table([[Paragraph(r[0], st["body"]), Paragraph(r[1], st["body"])] for r in rows],
              colWidths=[140 * mm, 20 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.2, colors.HexColor("#BBBBBB")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    return [Paragraph("CONTENTS", st["h1"]), Spacer(1, 4 * mm), t]


# --- Section 1 Abstract -----------------------------------------------------
_BLANK = "____________"  # fill-in blank for a genuinely-absent/manual value
# Seeded example for the manual "DS Characteristics" field, per the reference template
# (Apoorva). Shown as a guide for the author to overwrite.
_DS_CHARACTERISTICS_HINT = "e.g. high viscosity / yellow solution / ready-to-fill BDS"


def _descriptor(h, form: str) -> str:
    """Clean product descriptor, e.g. '360 mg S.INJ 2.00 mL in Vial' or, when the
    withdraw volume / container aren't recorded (dev samples), just '100 mg S.INJ'.
    Never emits dangling 'mL in' or <placeholder> fragments."""
    parts = []
    if h.dose_mg:
        parts.append(f"{h.dose_mg} mg")
    if form:
        parts.append(form)
    if h.withdraw_volume_ml:
        parts.append(f"{h.withdraw_volume_ml} mL")
    if h.container:
        parts.append(f"in {h.container}")
    return " ".join(parts) or "the drug product"


def _abstract(report: MfgrReport, defaults: ManualDefaults, st) -> list:
    h = report.header
    site = defaults.ds_site or h.manufactured_by or _BLANK
    batch_size = report.general_info.batch_size or _BLANK
    yield_ = report.yield_percent or _BLANK
    mfg_date = (h.mfg_start or "").split("T")[0] or _BLANK
    txt = (
        f"The scope of this report is to summarize the manufacturing process of "
        f"<b>{h.project_code or _BLANK}</b> {_descriptor(h, h.dose_form_label)}. "
        f"The manufacturing was performed on <b>{mfg_date}</b> in <b>{site}</b> with a "
        f"theoretical batch size of <b>{batch_size}</b>, resulting in a yield of "
        f"<b>{yield_}</b> after visual inspection. A detailed description of the process "
        f"is documented in the batch record of <b>{h.batch_id_display}</b> stored in the "
        f"electronic Laboratory Notebook (eLN) — refer to Section 10."
    )
    return [Paragraph("1 ABSTRACT", st["h1"]), Paragraph(txt, st["body"])]


# --- Section 2 Description of Change ----------------------------------------
def _description_of_change(st) -> list:
    return [Paragraph("2 DESCRIPTION OF CHANGE", st["h1"]),
            Paragraph("New document.", st["body"])]


# --- Section 3 Topic + Table 1 ---------------------------------------------
def _topic(report: MfgrReport, defaults: ManualDefaults, st) -> list:
    h = report.header
    gi = report.general_info
    conc = gi.concentration or gi.dose_strength or _BLANK
    intro = (
        f"An {h.project_code or '&lt;Project&gt;'} {h.dose_mg or '&lt;dose&gt;'} mg "
        f"{h.dose_form_label or '&lt;form&gt;'} {h.withdraw_volume_ml or '&lt;vol&gt;'} mL in "
        f"{h.container or '&lt;container&gt;'} was developed for clinical phase "
    )
    intro_end = (
        f", resulting in {conc} solution. The bulk drug product was manufactured from "
        f"drug substance <b>{(report.ds_components[0].lot if report.ds_components else '&lt;MMID / Lot&gt;')}</b>. "
        f"Drug substance was provided as "
    )
    intro_end2 = " material by "
    intro_tail = ". After manufacturing, samples are stored at "
    tail = "°C. Additional samples to evaluate hold times and material compatibilities were pulled."

    # sentence with three inline editable fields (clinical phase, DS quality, DS site, storage temp)
    inline = Table([[
        Paragraph(intro, st["body"]),
        _f(F_CLINICAL_PHASE, defaults.clinical_phase, w=18, h=12),
        Paragraph(intro_end, st["body"]),
        _f(F_DS_QUALITY, defaults.ds_quality, w=40, h=12),
        Paragraph(intro_end2, st["body"]),
        _f(F_DS_SITE, defaults.ds_site, w=45, h=12),
        Paragraph(intro_tail, st["body"]),
        _f(F_STORAGE_TEMP, defaults.storage_temp, w=25, h=12),
        Paragraph(tail, st["body"]),
    ]], colWidths=[70*mm, 20*mm, None, 42*mm, None, 47*mm, None, 27*mm, None])
    inline.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    notes_hint = Paragraph(
        "<i>Additional notes / special observations (formulation rationale, non-platform "
        "decisions, new packaging systems, etc.):</i>", st["small"])
    return [
        Paragraph("3 TOPIC", st["h1"]),
        Paragraph(
            "The concept representative stability batch manufacturing and the respective "
            "rationale for parameter selection are described in document A-RDLU-000087.",
            st["body"]),
        Spacer(1, 3 * mm),
        Paragraph(
            f"An {h.project_code} {_descriptor(h, h.dose_form_code or h.dose_form_label)} "
            f"was developed. Clinical phase, DS quality, DS manufacturing site, "
            f"DS form, and storage temperature are captured in Table 1 below "
            f"(manual entry — no system source). The bulk drug product was manufactured from drug "
            f"substance lot <b>{(report.ds_components[0].lot if report.ds_components else _BLANK)}</b>. "
            f"The bulk-solution concentration is {conc}.",
            st["body"]),
        Spacer(1, 2 * mm),
        Paragraph(
            f"<b>Bulk solution composition:</b> "
            f"{report.ds_composition_narrative or '&lt;auto-derived from Table 2 — no formulation rows found&gt;'}.",
            st["body"]),
        Spacer(1, 2 * mm),
        Paragraph("<b>Clinical phase:</b>", st["small"]),
        _f(F_CLINICAL_PHASE, defaults.clinical_phase, w=60 * mm, h=12),
        Spacer(1, 1 * mm),
        Paragraph("<b>Storage temperature (°C):</b>", st["small"]),
        _f(F_STORAGE_TEMP, defaults.storage_temp, w=60 * mm, h=12),
        Spacer(1, 3 * mm),
        notes_hint,
        _f(F_TOPIC_NOTES, defaults.topic_notes, w=175 * mm, h=30 * mm, multiline=True),
        Spacer(1, 4 * mm),
        Paragraph("Table 1. General information", st["h2"]),
        _table_1_general_info(report, defaults, st),
    ]


def _packaging_desc(report: MfgrReport) -> tuple[str, str, str]:
    """Split resolved packaging names into (format/container, stopper, crimp cap)
    for Table 1. First match of each wins; empty string when none found."""
    fmt = stopper = crimp = ""
    for p in report.packaging:
        low = (p.display_name or "").lower()
        if not fmt and any(k in low for k in ("vial", "syringe", "pfs", "cartridge")):
            fmt = p.display_name
        if not stopper and "stopper" in low:
            stopper = p.display_name
        if not crimp and ("crimp" in low or "cap" in low):
            crimp = p.display_name
    return fmt, stopper, crimp


# --- Table 1 (General information) ------------------------------------------
def _table_1_general_info(report: MfgrReport, defaults: ManualDefaults, st) -> Table:
    gi = report.general_info
    ds_lot = report.ds_components[0].lot if report.ds_components else ""
    pkg_format, pkg_stopper, pkg_crimp = _packaging_desc(report)
    # (parameter label, value_or_widget)
    rows = [
        [Paragraph("<b>Parameter</b>", st["cell"]),
         Paragraph("<b>Description</b>", st["cell"])],

        [Paragraph("Format", st["cell"]),
         _f(F_FORMAT, defaults.format_desc or pkg_format or gi.packaging_system,
            w=None, h=24, multiline=True)],
        [Paragraph("Stopper", st["cell"]),
         _f(F_STOPPER, defaults.stopper_desc or pkg_stopper, w=None, h=20, multiline=True)],
        [Paragraph("Crimp Cap", st["cell"]),
         _f(F_CRIMP, defaults.crimp_desc or pkg_crimp, w=None, h=20, multiline=True)],

        [Paragraph("Dosage form", st["cell"]),
         Paragraph(gi.dose_form or report.header.dose_form_label or "—", st["cell"])],
        [Paragraph("Dose (Lyophilizate) [mL]", st["cell"]),
         _f(F_DOSE_LYO, defaults.dose_lyo, w=None, h=12)],
        [Paragraph("Dosage volume (Liquid) [mL]", st["cell"]),
         Paragraph(report.header.withdraw_volume_ml or "—", st["cell"])],
        [Paragraph("Dosage strength [mg]", st["cell"]),
         Paragraph(report.header.dose_mg or "—", st["cell"])],

        [Paragraph("<b>Bulk solution</b> — Concentration [mg/mL]", st["cell"]),
         Paragraph(gi.concentration or gi.dose_strength or "—", st["cell"])],
        [Paragraph("<b>Bulk solution</b> — Density (20 °C) [g/mL]", st["cell"]),
         Paragraph(gi.density or "—", st["cell"])],
        [Paragraph("<b>Bulk solution</b> — pH", st["cell"]),
         Paragraph(gi.ph or "—", st["cell"])],

        [Paragraph("Reconstitution medium (lyo)", st["cell"]),
         _f(F_RECON_MEDIUM, defaults.recon_medium, w=None, h=12)],
        [Paragraph("Reconstitution volume [mL] (lyo)", st["cell"]),
         _f(F_RECON_VOLUME, defaults.recon_volume, w=None, h=12)],
        [Paragraph("Concentration of reconstituted solution [mg/mL] (lyo)", st["cell"]),
         _f(F_RECON_CONC, defaults.recon_concentration, w=None, h=12)],
        [Paragraph("Fill volume [mL] (including overfill)", st["cell"]),
         _f(F_FILL_VOLUME, defaults.fill_volume or gi.fill_volume, w=None, h=12)],

        [Paragraph("Batch size", st["cell"]),
         Paragraph(gi.batch_size or "—", st["cell"])],
        [Paragraph("Yield (= final-stage amount ÷ first-stage amount)", st["cell"]),
         Paragraph(report.yield_percent or "—", st["cell"])],

        [Paragraph("<b>Drug substance</b> — Composition", st["cell"]),
         Paragraph("As applicable — refer to Section 4", st["cell"])],
        [Paragraph("<b>Drug substance</b> — Manufacturing site", st["cell"]),
         _f(F_DS_SITE, defaults.ds_site, w=None, h=12)],
        [Paragraph("<b>Drug substance</b> — Quality", st["cell"]),
         _f(F_DS_QUALITY, defaults.ds_quality, w=None, h=12)],
        [Paragraph("<b>Drug substance</b> — Form", st["cell"]),
         _f(F_DS_FORM, defaults.ds_form, w=None, h=12)],
        [Paragraph("<b>Drug substance</b> — Lot / MMID", st["cell"]),
         Paragraph(ds_lot or "—", st["cell"])],
        [Paragraph("<b>Drug substance</b> — Characteristics", st["cell"]),
         _f(F_DS_CHARACTERISTICS,
            defaults.ds_characteristics or _DS_CHARACTERISTICS_HINT,
            w=None, h=20, multiline=True)],
    ]
    t = Table(rows, colWidths=[65 * mm, 110 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D8DEE9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


# --- Section 4 Composition (Table 2) ----------------------------------------
def _composition(report: MfgrReport, st) -> list:
    intro = Paragraph(
        "The following table shows the composition of the bulk solution and amount per unit. "
        "Refer to document A-500482-E for significant digits.", st["body"])
    if not report.composition:
        return [Paragraph("4 COMPOSITION OF THE FORMULATION", st["h1"]), intro,
                Paragraph("No formulation rows recorded — composition is manual.", st["body"])]
    head = [Paragraph(f"<b>{h}</b>", st["cell"]) for h in
            ("Component", "Quality standard", "Function", "Composition",
             "Amount per unit (mg)")]
    rows = [head]
    # Table 2 is the bulk-solution composition only — packaging + intermediate
    # "bulk solution" rows are excluded (they surface in Table 1 / the process flow).
    ingredients = [c for c in report.composition if c.is_ingredient]
    for c in ingredients:
        rows.append([
            Paragraph(c.component_name, st["cell"]),
            Paragraph(c.quality_standard or "&lt;manual&gt;", st["cell"]),
            Paragraph(c.component_function or "—", st["cell"]),
            Paragraph(c.amount_per_unit or "—", st["cell"]),
            Paragraph(c.amount_per_unit_mg or "—", st["cell"]),
        ])
    t = Table(rows, colWidths=[40 * mm, 32 * mm, 32 * mm, 32 * mm, 32 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D8DEE9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return [Paragraph("4 COMPOSITION OF THE FORMULATION", st["h1"]),
            intro, Spacer(1, 2 * mm),
            Paragraph("Table 2. Composition of the bulk solution", st["h2"]), t]


# --- Section 5 Process Flow Chart (image placeholder + per-stage table) -----
def _process_flow(report: MfgrReport, st) -> list:
    items = [
        Paragraph("5 PROCESS FLOW CHART", st["h1"]),
        Paragraph("<i>Insert process flow-chart image below (author-supplied). "
                  "Per-stage volumes are auto-generated from the batch record.</i>", st["small"]),
        Spacer(1, 2 * mm),
    ]
    if report.stages:
        head = [Paragraph(f"<b>{h}</b>", st["cell"]) for h in
                ("Stage", "Unit operation", "Batch ID (stage)", "Amount", "Confident?")]
        rows = [head]
        for s in report.stages:
            rows.append([
                Paragraph(str(s.stage_index), st["cell"]),
                Paragraph(s.unit_operation or "—", st["cell"]),
                Paragraph(s.batch_id_stage or "—", st["cell"]),
                Paragraph(s.amount or "—", st["cell"]),
                Paragraph("Yes" if s.confident else "review", st["cell"]),
            ])
        t = Table(rows, colWidths=[15 * mm, 60 * mm, 45 * mm, 30 * mm, 25 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D8DEE9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        items.append(t)
    return items


# --- Sections 6-9 -----------------------------------------------------------
def _unit_ops_bullets(report: MfgrReport) -> str:
    """Seed text for the Procedure notes field — the batch's unit operations as
    bullets so the author doesn't start from a blank field (Waldemar [17])."""
    return "\n".join(f"• {s.unit_operation}" for s in report.stages if s.unit_operation)


def _procedure(report: MfgrReport, defaults: ManualDefaults, st) -> list:
    seed = defaults.procedure_notes or _unit_ops_bullets(report)
    return [Paragraph("6 PROCEDURE / OBSERVATIONS", st["h1"]),
            Paragraph(f"The manufacturing procedure is documented in the eLN "
                      f"(eLN experiment ID: <b>{report.header.eln_experiment_id or _BLANK}</b>). "
                      f"Unit operations are pre-filled below — add observations as needed:", st["body"]),
            Spacer(1, 2 * mm),
            _f(F_PROCEDURE_NOTES, seed, w=175 * mm, h=45 * mm, multiline=True)]


def _dev_samples(defaults: ManualDefaults, st) -> list:
    return [Paragraph("7 DEVELOPMENT SAMPLES RESULTS", st["h1"]),
            Paragraph("Reference the associated Analytical Report (ATR) by document number. "
                      "Author-supplied summary:", st["body"]),
            Spacer(1, 2 * mm),
            _f(F_DEV_SAMPLES, defaults.dev_samples, w=175 * mm, h=40 * mm, multiline=True)]


def _compatibility(defaults: ManualDefaults, st) -> list:
    return [Paragraph("8 COMPATIBILITY / HOLD TIME SAMPLE RESULTS", st["h1"]),
            _f(F_COMPATIBILITY, defaults.compatibility, w=175 * mm, h=45 * mm, multiline=True)]


def _conclusion(defaults: ManualDefaults, st) -> list:
    return [Paragraph("9 CONCLUSION", st["h1"]),
            _f(F_CONCLUSION, defaults.conclusion, w=175 * mm, h=45 * mm, multiline=True)]


def _references(report: MfgrReport, st) -> list:
    h = report.header
    lines = [
        Paragraph("10 REFERENCES", st["h1"]),
        Paragraph(f"• eLN experiment ID: <b>{h.eln_experiment_id or _BLANK}</b>", st["body"]),
        Paragraph(f"• eLN experiment URL: {h.eln_url or '&mdash;'}", st["small"]),
        # NEST rep batches are not linked to a PEGA/CMC request (no REQUESTS row),
        # so there is no PA number to auto-fill — flag it as manual rather than
        # echoing the project name (which is not a request number).
        Paragraph("• PA / CMC Request number: <i>manual — batch not linked to a "
                  "CMC request</i>", st["small"]),
        Paragraph(f"• Batch record: {h.batch_id_display} ({report.batch_id_query})", st["body"]),
        Paragraph("• Rationale document: A-RDLU-000087", st["body"]),
        Paragraph("• LIMS study number: <i>manual — source not confirmed</i>", st["small"]),
    ]
    return lines


# --- Audit block (final page) -----------------------------------------------
def _audit_block(report: MfgrReport, st, generated_at: str | None):
    n_ingredients = sum(1 for c in report.composition if c.is_ingredient)
    n_other = len(report.composition) - n_ingredients
    items = [Paragraph("<b>Audit trail (auto-generated)</b>", st["h2"]),
             Paragraph(f"Batch ID: {report.batch_id_display} ({report.batch_id_query})", st["body"]),
             Paragraph(f"Project: {report.header.project_code}", st["body"]),
             Paragraph(f"Stages: {len(report.stages)}  ·  ingredients: {n_ingredients} "
                       f"(+{n_other} packaging/bulk)  ·  DS components: {len(report.ds_components)}  ·  "
                       f"packaging: {len(report.packaging)}",
                       st["body"]),
             Paragraph(f"Yield: {report.yield_percent or '—'}", st["body"]),
             Paragraph(f"Generated: {generated_at or '(set at generation)'}", st["body"]),
             Paragraph(f"Template: {TEMPLATE_ID}", st["body"])]
    if report.flags:
        items.append(Paragraph("<b>Review flags:</b>", st["label"]))
        for f in report.flags:
            items.append(Paragraph(f"[{f.severity}] {f.area}: {f.message}", st["small"]))
    return items


# --- page header / footer ---------------------------------------------------
def _make_header_footer(report: MfgrReport):
    h = report.header
    title_line = h.title_line or ""
    project = h.project_code or ""

    def _hf(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        w, page_h = A4
        # header
        canvas.drawString(15 * mm, page_h - 12 * mm,
                          "AbbVie Deutschland GmbH & Co. KG  |  Global Pharmaceutical Sciences")
        canvas.drawRightString(w - 12 * mm, page_h - 12 * mm,
                               f"Document type: Laboratory Report  |  Page {doc.page}")
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(15 * mm, page_h - 17 * mm,
                          f"{project}  —  Manufacturing of Representative Batch "
                          f"{h.batch_id_display}")
        canvas.setFont("Helvetica", 7)
        canvas.drawString(15 * mm, page_h - 21 * mm, title_line)
        # footer
        canvas.drawCentredString(w / 2, 10 * mm,
                                 f"Template: {TEMPLATE_ID}   |   AbbVie Confidential General")
        canvas.restoreState()
    return _hf


# --- entry point ------------------------------------------------------------
def build_story(report: MfgrReport, defaults: ManualDefaults, generated_at: str | None):
    st = _styles()
    story = []
    story += _title_page(report, st)
    story += [PageBreak()]
    story += _signature_page(st)
    story += [PageBreak()]
    story += _toc(st)
    story += [PageBreak()]
    story += _abstract(report, defaults, st)
    story += [Spacer(1, 4 * mm)]
    story += _description_of_change(st)
    story += [Spacer(1, 4 * mm)]
    story += _topic(report, defaults, st)
    story += [PageBreak()]
    story += _composition(report, st)
    story += [PageBreak()]
    story += _process_flow(report, st)
    story += [PageBreak()]
    story += _procedure(report, defaults, st)
    story += [Spacer(1, 4 * mm)]
    story += _dev_samples(defaults, st)
    story += [Spacer(1, 4 * mm)]
    story += _compatibility(defaults, st)
    story += [PageBreak()]
    story += _conclusion(defaults, st)
    story += [Spacer(1, 4 * mm)]
    story += _references(report, st)
    story += [PageBreak()]
    story += _audit_block(report, st, generated_at)
    return story


def generate_mfgr_pdf(report: MfgrReport, out_path: str | Path, *,
                      defaults: ManualDefaults | None = None,
                      generated_at: str | None = None,
                      flatten: bool = False) -> Path:
    """Render the fillable MFGR PDF to ``out_path`` and return the path.

    ``flatten=True`` replaces AcroForm widgets with plain text drawn on the page.
    Use this for the DOCX conversion path — pdf2docx cannot extract widget
    values, so widgets must be flattened first if the Word output is to contain
    the DWH-populated values and be normally editable.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            leftMargin=15 * mm, rightMargin=12 * mm,
                            topMargin=25 * mm, bottomMargin=16 * mm,
                            title=f"MFGR {report.batch_id_display}")
    prev = AcroField.flatten
    AcroField.flatten = flatten
    try:
        doc.build(build_story(report, defaults or ManualDefaults(), generated_at),
                  onFirstPage=_make_header_footer(report),
                  onLaterPages=_make_header_footer(report))
    finally:
        AcroField.flatten = prev
    return out_path
