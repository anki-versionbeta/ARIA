# Ported verbatim from src/mfgr_docx.py.
#
# One edit: the PDF builder is `mfgr_pdf` here, matching this silo's naming (the ATR
# port renamed `pdf_builder` -> `atr_pdf` for the same reason). `docx_common` is shared
# with ATR and is imported, not re-ported - two copies would drift.
"""Render an MFGR to a native Word (.docx) document.

The MFGR counterpart of ``atr_docx`` (and a sibling of ``mfgr_pdf_builder``): builds an
editable .docx straight from the shaped ``MfgrReport`` so the content, section order and
formatting match the PDF — title block, signatures, contents, §1 Abstract … §10 References,
incl. Table 1 (General information) and Table 2 (Composition), the §5 process-flow stage
table, and the audit block.

Formatting is aligned to the reportlab PDF via ``docx_common`` (Arial base font, navy
headings, shaded table headers, fixed column widths, page breaks between sections, and the
same running header/footer). Auto-filled values come from the report; the manual fields
render as ``[manual entry]`` placeholders (or their ``ManualDefaults`` values), mirroring
the PDF's blank form fields.
"""
from __future__ import annotations

from pathlib import Path

from . import docx_common as dc
from .mfgr_pdf import TEMPLATE_ID, ManualDefaults, _packaging_desc
from .mfgr_report import MfgrReport

_MANUAL = "[manual entry]"


def _first(*vals: str, ph: str = _MANUAL) -> str:
    for v in vals:
        if v and str(v).strip():
            return str(v).strip()
    return ph


def _field_line(doc, label: str, value: str) -> None:
    p = doc.add_paragraph()
    p.add_run(f"{label}: ").bold = True
    p.add_run(value or _MANUAL)


# --- sections ---------------------------------------------------------------
def _title_block(doc, report: MfgrReport) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    h = report.header
    for text, size in (
        (h.project_code or "<ABT- or ABBV-Project>", 14),
        (f"Manufacturing of Representative Batch {h.batch_id_display} of {h.project_code}", 14),
        (h.title_line or "<Dose> mg <S.INJ> <Volume> mL in <PFS or Vial>", 12),
    ):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.bold = True
        run.font.size = dc.Pt(size)
    p = doc.add_paragraph(f"Template: {TEMPLATE_ID}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _signatures(doc) -> None:
    dc.heading(doc, "Signatures", size=11)
    t = dc.grid(doc, 4, 3)
    for c, label in enumerate(("", "Name", "Date")):
        dc.set_cell(t.cell(0, c), label, bold=True)
    for r, role in enumerate(("Compiled", "Verified", "Approved"), start=1):
        dc.set_cell(t.cell(r, 0), role, bold=True)
    dc.set_col_widths(t, [25, 103, 55])


def _contents(doc) -> None:
    dc.heading(doc, "Contents", size=11)
    for line in (
        "1 ABSTRACT", "2 DESCRIPTION OF CHANGE", "3 TOPIC",
        "4 COMPOSITION OF THE FORMULATION", "5 PROCESS FLOW CHART",
        "6 PROCEDURE / OBSERVATIONS", "7 DEVELOPMENT SAMPLES RESULTS",
        "8 COMPATIBILITY / HOLD TIME SAMPLES", "9 CONCLUSION", "10 REFERENCES",
    ):
        doc.add_paragraph(line)


_BLANK = "____________"  # fill-in blank for a genuinely-absent/manual value


def _descriptor(h, form: str) -> str:
    """Clean product descriptor, e.g. '360 mg S.INJ 2.00 mL in Vial' or just
    '100 mg S.INJ' when volume/container aren't recorded. No dangling 'mL in'."""
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


def _abstract(doc, report: MfgrReport, d: ManualDefaults) -> None:
    h = report.header
    site = _first(d.ds_site, h.manufactured_by, ph=_BLANK)
    batch_size = report.general_info.batch_size or _BLANK
    yield_ = report.yield_percent or _BLANK
    mfg_date = (h.mfg_start or "").split("T")[0] or _BLANK
    dc.heading(doc, "1 ABSTRACT")
    doc.add_paragraph(
        f"The scope of this report is to summarize the manufacturing process of "
        f"{h.project_code or _BLANK} {_descriptor(h, h.dose_form_label)}. "
        f"The manufacturing was performed on {mfg_date} "
        f"in {site} with a theoretical batch size of {batch_size}, resulting in a yield of "
        f"{yield_} after visual inspection. A detailed description of the process is "
        f"documented in the batch record of {h.batch_id_display} stored in the electronic "
        f"Laboratory Notebook (eLN) — refer to Section 10.")


def _topic(doc, report: MfgrReport, d: ManualDefaults) -> None:
    h = report.header
    gi = report.general_info
    conc = gi.concentration or gi.dose_strength or _BLANK
    ds_lot = report.ds_components[0].lot if report.ds_components else _BLANK
    dc.heading(doc, "3 TOPIC")
    doc.add_paragraph(
        "The concept representative stability batch manufacturing and the respective "
        "rationale for parameter selection are described in document A-RDLU-000087.")
    doc.add_paragraph(
        f"An {h.project_code} {_descriptor(h, h.dose_form_code or h.dose_form_label)} "
        f"was developed. Clinical phase, DS quality, DS manufacturing site, "
        f"DS form, and storage temperature are captured in Table 1 below (manual entry — no "
        f"system source). The bulk drug product was manufactured from drug substance lot "
        f"{ds_lot}. The bulk-solution concentration is {conc}.")
    p = doc.add_paragraph()
    p.add_run("Bulk solution composition: ").bold = True
    p.add_run(report.ds_composition_narrative
              or "<auto-derived from Table 2 — no formulation rows found>")
    _field_line(doc, "Clinical phase", d.clinical_phase)
    _field_line(doc, "Storage temperature (°C)", d.storage_temp)
    _field_line(doc, "Additional notes / special observations", d.topic_notes)
    dc.heading(doc, "Table 1. General information", size=11)
    _table_1(doc, report, d)


def _table_1(doc, report: MfgrReport, d: ManualDefaults) -> None:
    gi = report.general_info
    ds_lot = report.ds_components[0].lot if report.ds_components else ""
    pkg_format, pkg_stopper, pkg_crimp = _packaging_desc(report)
    rows = [
        ("Format", _first(d.format_desc, pkg_format, gi.packaging_system)),
        ("Stopper", _first(d.stopper_desc, pkg_stopper)),
        ("Crimp Cap", _first(d.crimp_desc, pkg_crimp)),
        ("Dosage form", _first(gi.dose_form, report.header.dose_form_label, ph="—")),
        ("Dose (Lyophilizate) [mL]", _first(d.dose_lyo)),
        ("Dosage volume (Liquid) [mL]", _first(report.header.withdraw_volume_ml, ph="—")),
        ("Dosage strength [mg]", _first(report.header.dose_mg, ph="—")),
        ("Bulk solution — Concentration [mg/mL]", _first(gi.concentration, gi.dose_strength, ph="—")),
        ("Bulk solution — Density (20 °C) [g/mL]", _first(gi.density, ph="—")),
        ("Bulk solution — pH", _first(gi.ph, ph="—")),
        ("Reconstitution medium (lyo)", _first(d.recon_medium)),
        ("Reconstitution volume [mL] (lyo)", _first(d.recon_volume)),
        ("Concentration of reconstituted solution [mg/mL] (lyo)", _first(d.recon_concentration)),
        ("Fill volume [mL] (including overfill)", _first(d.fill_volume, gi.fill_volume)),
        ("Batch size", _first(gi.batch_size, ph="—")),
        ("Yield (= final-stage amount ÷ first-stage amount)", _first(report.yield_percent, ph="—")),
        ("Drug substance — Composition", "As applicable — refer to Section 4"),
        ("Drug substance — Manufacturing site", _first(d.ds_site)),
        ("Drug substance — Quality", _first(d.ds_quality)),
        ("Drug substance — Form", _first(d.ds_form)),
        ("Drug substance — Lot / MMID", _first(ds_lot, ph="—")),
        ("Drug substance — Characteristics",
         _first(d.ds_characteristics, ph="e.g. high viscosity / yellow solution / ready-to-fill BDS")),
    ]
    t = dc.grid(doc, 1 + len(rows), 2)
    dc.set_cell(t.cell(0, 0), "Parameter", bold=True)
    dc.set_cell(t.cell(0, 1), "Description", bold=True)
    for i, (label, value) in enumerate(rows, start=1):
        dc.set_cell(t.cell(i, 0), label)
        dc.set_cell(t.cell(i, 1), value)
    dc.shade_header_row(t)
    dc.set_col_widths(t, [65, 110])


def _composition(doc, report: MfgrReport) -> None:
    dc.heading(doc, "4 COMPOSITION OF THE FORMULATION")
    doc.add_paragraph(
        "The following table shows the composition of the bulk solution and amount per unit. "
        "Refer to document A-500482-E for significant digits.")
    ingredients = [c for c in report.composition if c.is_ingredient]
    if not ingredients:
        doc.add_paragraph("No formulation rows recorded — composition is manual.")
        return
    dc.heading(doc, "Table 2. Composition of the bulk solution", size=11)
    headers = ("Component", "Quality standard", "Function", "Composition",
               "Amount per unit (mg)")
    t = dc.grid(doc, 1 + len(ingredients), len(headers))
    for c, hh in enumerate(headers):
        dc.set_cell(t.cell(0, c), hh, bold=True, size=8)
    for i, c in enumerate(ingredients, start=1):
        dc.set_cell(t.cell(i, 0), c.component_name, size=8)
        dc.set_cell(t.cell(i, 1), c.quality_standard or "<manual>", size=8)
        dc.set_cell(t.cell(i, 2), c.component_function or "—", size=8)
        dc.set_cell(t.cell(i, 3), c.amount_per_unit or "—", size=8)
        dc.set_cell(t.cell(i, 4), c.amount_per_unit_mg or "—", size=8)
    dc.shade_header_row(t)
    dc.set_col_widths(t, [40, 32, 32, 32, 32])


def _process_flow(doc, report: MfgrReport) -> None:
    dc.heading(doc, "5 PROCESS FLOW CHART")
    doc.add_paragraph(
        "Insert process flow-chart image below (author-supplied). "
        "Per-stage volumes are auto-generated from the batch record.")
    if not report.stages:
        return
    headers = ("Stage", "Unit operation", "Batch ID (stage)", "Amount", "Confident?")
    t = dc.grid(doc, 1 + len(report.stages), len(headers))
    for c, hh in enumerate(headers):
        dc.set_cell(t.cell(0, c), hh, bold=True, size=8)
    for i, s in enumerate(report.stages, start=1):
        dc.set_cell(t.cell(i, 0), str(s.stage_index), size=8)
        dc.set_cell(t.cell(i, 1), s.unit_operation or "—", size=8)
        dc.set_cell(t.cell(i, 2), s.batch_id_stage or "—", size=8)
        dc.set_cell(t.cell(i, 3), s.amount or "—", size=8)
        dc.set_cell(t.cell(i, 4), "Yes" if s.confident else "review", size=8)
    dc.shade_header_row(t)
    dc.set_col_widths(t, [15, 60, 45, 30, 25])


def _references(doc, report: MfgrReport) -> None:
    h = report.header
    dc.heading(doc, "10 REFERENCES")
    for line in (
        f"• eLN experiment ID: {h.eln_experiment_id or _BLANK}",
        f"• eLN experiment URL: {h.eln_url or '—'}",
        "• PA / CMC Request number: manual — batch not linked to a CMC request",
        f"• Batch record: {h.batch_id_display} ({report.batch_id_query})",
        "• Rationale document: A-RDLU-000087",
        "• LIMS study number: manual — source not confirmed",
    ):
        doc.add_paragraph(line)


def _audit_block(doc, report: MfgrReport, generated_at: str | None) -> None:
    n_ing = sum(1 for c in report.composition if c.is_ingredient)
    n_other = len(report.composition) - n_ing
    dc.heading(doc, "Audit trail (auto-generated)", size=11)
    for line in (
        f"Batch ID: {report.batch_id_display} ({report.batch_id_query})",
        f"Project: {report.header.project_code}",
        f"Stages: {len(report.stages)}  ·  ingredients: {n_ing} (+{n_other} packaging/bulk)  ·  "
        f"DS components: {len(report.ds_components)}  ·  packaging: {len(report.packaging)}",
        f"Yield: {report.yield_percent or '—'}",
        f"Generated: {generated_at or '(set at generation)'}",
        f"Template: {TEMPLATE_ID}",
    ):
        doc.add_paragraph(line)
    if report.flags:
        p = doc.add_paragraph()
        p.add_run("Review flags:").bold = True
        for f in report.flags:
            doc.add_paragraph(f"[{f.severity}] {f.area}: {f.message}")


def generate_mfgr_docx(report: MfgrReport, out_path: str | Path, *,
                       defaults: ManualDefaults | None = None,
                       generated_at: str | None = None) -> Path:
    """Render the MFGR as an editable .docx to ``out_path`` and return the path."""
    d = defaults or ManualDefaults()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    h = report.header

    doc = dc.new_document(font="Arial", size=10)
    dc.configure_page(doc, left=15, right=12, top=25, bottom=16)
    dc.set_running_header(
        doc,
        left="AbbVie Deutschland GmbH & Co. KG  |  Global Pharmaceutical Sciences",
        right="Document type: Laboratory Report  |  Page",
        title=f"{h.project_code or ''}  —  Manufacturing of Representative Batch {h.batch_id_display}",
        subtitle=h.title_line or "")
    dc.set_centered_footer(doc, f"Template: {TEMPLATE_ID}   |   AbbVie Confidential General")

    # Section order + page breaks mirror mfgr_pdf_builder.build_story().
    _title_block(doc, report)
    dc.page_break(doc)
    _signatures(doc)
    dc.page_break(doc)
    _contents(doc)
    dc.page_break(doc)
    _abstract(doc, report, d)
    dc.heading(doc, "2 DESCRIPTION OF CHANGE")
    doc.add_paragraph("New document.")
    _topic(doc, report, d)
    dc.page_break(doc)
    _composition(doc, report)
    dc.page_break(doc)
    _process_flow(doc, report)
    dc.page_break(doc)
    dc.heading(doc, "6 PROCEDURE / OBSERVATIONS")
    doc.add_paragraph(
        f"The manufacturing procedure is documented in the eLN "
        f"(eLN experiment ID: {h.eln_experiment_id or _BLANK}). "
        f"Unit operations are pre-filled below — add observations as needed:")
    if d.procedure_notes:
        doc.add_paragraph(d.procedure_notes)
    else:  # seed unit operations as bullets (Waldemar [17])
        for s in report.stages:
            if s.unit_operation:
                doc.add_paragraph(s.unit_operation, style="List Bullet")
    dc.heading(doc, "7 DEVELOPMENT SAMPLES RESULTS")
    doc.add_paragraph("Reference the associated Analytical Report (ATR) by document number. "
                      "Author-supplied summary:")
    doc.add_paragraph(d.dev_samples or _MANUAL)
    dc.heading(doc, "8 COMPATIBILITY / HOLD TIME SAMPLE RESULTS")
    doc.add_paragraph(d.compatibility or _MANUAL)
    dc.page_break(doc)
    dc.heading(doc, "9 CONCLUSION")
    doc.add_paragraph(d.conclusion or _MANUAL)
    _references(doc, report)
    dc.page_break(doc)
    _audit_block(doc, report, generated_at)

    doc.save(str(out_path))
    return out_path
