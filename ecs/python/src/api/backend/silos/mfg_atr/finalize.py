# Ported verbatim from src/finalize.py.
#
# Imports only pathlib (pypdf is imported lazily inside the functions), so it needed no
# platform seam changes. Note the docstring is candid that this is a record LOCK rather
# than a true appearance flatten - pypdf cannot bake widget appearances into the page
# content stream - and that full flattening is an open document-control decision.
#
# src/mfgr_finalize.py is the same module with one behavioural difference: its
# owner_password default is "mfgr-locked". Rather than keep a second near-identical copy
# that could drift, mfgr_pipeline passes that value explicitly. Do not change the default
# below - it is ATR's record password.
"""Finalize an ATR PDF into a locked record copy.

After the author completes the Regulatory + HQC fields, finalize produces the archival
version: every form field is set **read-only** (so the completed values are frozen and no
longer editable), the widgets keep the appearance streams the builder embedded, and the
document is **owner-password encrypted** with permissions that disallow modification.

Note on "flatten": pypdf does not natively bake widget appearances into the page content
stream, so we lock by (a) setting the ReadOnly flag on every field and (b) restricting
permissions via encryption — a defensible record lock. We do **not** set NeedAppearances:
that would make each viewer regenerate its own appearance, and Chrome/PDFium then drops
the checkbox box outline (see below). Full appearance-flattening can be added later per
QA's document-control decision (plan open item B).
"""
from __future__ import annotations

from pathlib import Path

FF_READ_ONLY = 1  # /Ff bit 1


def _set_read_only(field_obj) -> None:
    from pypdf.generic import NameObject, NumberObject

    current = 0
    if "/Ff" in field_obj:
        try:
            current = int(field_obj["/Ff"])
        except (TypeError, ValueError):
            current = 0
    field_obj[NameObject("/Ff")] = NumberObject(current | FF_READ_ONLY)
    for kid in field_obj.get("/Kids", []) or []:
        _set_read_only(kid.get_object())


def finalize_pdf(in_path: str | Path, out_path: str | Path,
                 owner_password: str = "atr-locked") -> Path:
    """Write a locked (read-only fields + permission-restricted) copy of ``in_path``."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.constants import UserAccessPermissions as UAP

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(in_path))
    writer = PdfWriter()
    writer.append(reader)

    # 1) freeze every form field as read-only
    root = writer._root_object
    if "/AcroForm" in root:
        acro = root["/AcroForm"].get_object()
        for field in acro.get("/Fields", []) or []:
            _set_read_only(field.get_object())
    # Deliberately do NOT set NeedAppearances. The builder (reportlab) already embeds a
    # full appearance stream for every widget — the box + cross for check/radio fields and
    # the text for text fields, reflecting the author's values. Setting NeedAppearances
    # tells each viewer to regenerate its own appearance instead: Adobe/Edge/MuPDF redraw
    # the boxes, but Chrome/PDFium draws a bare check with no box (and nothing when
    # unchecked), so the boxes vanish in Chrome. Leaving it off makes every viewer use the
    # embedded, box-drawn appearances — consistent across all PDF readers.

    # 2) permission-restricted encryption (open freely; modification disallowed)
    writer.encrypt(user_password="", owner_password=owner_password,
                   permissions_flag=UAP.PRINT | UAP.PRINT_TO_REPRESENTATION)

    with out_path.open("wb") as fh:
        writer.write(fh)
    return out_path
