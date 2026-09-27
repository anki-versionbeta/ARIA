"""Shared regexes, ported verbatim from backend.py:2091-2151.

Its own module because the table-of-contents code, the caption matching and the media
pre-scan all need them, and two copies of `_TABLE_CAPTION_RE` would drift apart under
later edits and produce exactly the silent output change this port exists to avoid.
Import these; never re-declare one.

Every pattern and every explanatory comment is the source's own. The comments record
which real documents each pattern was tuned against, so they are part of the evidence
that the port did not change behaviour.
"""

from __future__ import annotations

import re

_WATERMARK_RE = re.compile(
    r'Copyrighted material|licensed to|Techstreet|ABBVIE', re.IGNORECASE
)
# Running header pattern: short block that is just an ISO/IEC/BS standard number line
# Covers forms like:
#   "ISO 10943:2023(E)"
#   "BS EN ISO 10943:2023\nISO 10943:2023(E)"
#   "ISO 10943:2023(E) Foreword"
#   "INTERNATIONAL STANDARD   ISO 10943:2023(E)"
_RUNNING_HEADER_RE = re.compile(
    r'^(?:INTERNATIONAL\s+STANDARD\s*[\t ]*)?'
    r'(?:BS\s+)?(?:EN\s+)?(?:ISO|IEC|EN)\s+[\w\-]+[:\-]\d{4}(?:\s*\([^)]{1,3}\))?'
    r'(?:\s+\S[^\r\n]*)?'
    r'(?:\s*[\r\n]+\s*(?:BS\s+)?(?:EN\s+)?(?:ISO|IEC|EN)\s+[\w\-]+[:\-]\d{4}(?:\s*\([^)]{1,3}\))?(?:\s+\S[^\r\n]*)?)?$',
    re.IGNORECASE
)
# Prefix-strip variant: matches only the leading standard-number token (with
# optional edition suffix like "+AMD1:2012+AMD2:2020" and "(E)") so it can be
# removed from TOC entry titles that the vision LLM prepended with the page
# running header.  Example: "ISO 11608-4:2022(E) 8.10.6 NIS-E …" → "8.10.6 NIS-E …"
_STD_PREFIX_RE = re.compile(
    r'^(?:INTERNATIONAL\s+STANDARD\s+)?'
    r'(?:BS\s+)?(?:EN\s+)?(?:ISO|IEC|EN)\s+[\w/\-]+[:\-]\d{4}'
    r'(?:\+[\w:\-]+)*'           # optional amendment suffixes e.g. +AMD1:2012
    r'(?:\s*\([^)]{1,5}\))?'    # optional edition marker e.g. (E)
    r'\s+',                       # must be followed by a space (more text after)
    re.IGNORECASE
)
# Footer pattern: "N All rights reserved", "All rights reserved",
# or "© ISO 2022 – All rights reserved [page#]"
_FOOTER_RE = re.compile(
    r'^\d{0,4}\s*\n?\s*All rights reserved\s*$'
    r'|^©\s*(?:ISO|IEC|BSI|EN)\b.*All rights reserved.*$',
    re.IGNORECASE
)
# Back matter keywords — stop extraction when any of these appear as a standalone block
_BACK_MATTER_RE = re.compile(
    r'^(?:Bibliography|NO COPYING WITHOUT BSI|British Standards Institution|'
    r'This page deliberately left blank|BSI Group Headquarters|'
    r'ICS\s+\d+\.\d+)',
    re.IGNORECASE
)
# Require dash after figure number to avoid false positives from table cell text
_FIGURE_CAPTION_RE  = re.compile(r'^(?:Figure|FIGURE)\s+(\d+)\s*[—–\-]')
# Matches Annex figure captions: "Figure A.1 —", "Figure H.2 —", etc.
_ANNEX_FIG_CAP_RE   = re.compile(r'^(?:Figure|FIGURE)\s+([A-Z]\.\d+|\d+\.\d+)\s*[—–\-]')
# Matches inline references to Annex figures in body text
_ANNEX_FIG_REF_RE   = re.compile(r'\bFigure\s+([A-Z]\.\d+|\d+\.\d+)\b', re.IGNORECASE)


def _parse_fig_ref(ref_str):
    """Convert a figure reference string to a stable integer key.

    Numeric refs ('1', '12') → positive int.
    Annex refs ('A.1', 'H.2') → large stable negative int using
    -(ord(letter)*1000 + number), e.g. A.1 → -65001, H.2 → -72002.
    These never collide with the counter-based negatives (-1, -2, ...).
    Returns None when ref_str is unrecognised.
    """
    if not ref_str:
        return None
    s = str(ref_str).strip()
    m_num = re.match(r'^(\d+)$', s)
    if m_num:
        return int(m_num.group(1))
    # Section-numbered figures ("6.2", "7.10") — common in ANSI/AAMI-style docs.
    # Encode as -(section*1000 + sub).  Section is capped < 65 so these keys
    # (-1001 .. -64999) can never collide with the annex encoding, which uses
    # the LETTER ordinal as the high part (A=65 .. Z=90 → -65001 .. -90999).
    # Without this, "6.1"/"6.2"/… all fell through to a bare-integer fallback
    # that keyed them ALL as section 6 → they overwrote each other and only one
    # figure per section survived.
    m_sec = re.match(r'^(\d+)\.(\d+)$', s)
    if m_sec:
        sec, sub = int(m_sec.group(1)), int(m_sec.group(2))
        if 1 <= sec < 65 and sub < 1000:
            return -(sec * 1000 + sub)
        return None
    m_ann = re.match(r'^([A-Z])\.(\d+)$', s)
    if m_ann:
        return -(ord(m_ann.group(1)) * 1000 + int(m_ann.group(2)))
    return None


def _fig_ref_display(fig_num):
    """Reverse of _parse_fig_ref: turn a figure KEY back into its display ref.

    Positive  -> "12".
    Annex key -> "B.1"  (key -66001 -> chr(66)='B', 66001 % 1000 = 1).
    Small counter negatives (-1..-999, truly unlabeled figures) -> None.
    Without this, the flush label f'Figure {abs(fig_num)}' printed an annex
    figure as "Figure 66001".
    """
    if fig_num is None:
        return None
    if fig_num > 0:
        return str(fig_num)
    if fig_num <= -1000:                     # section OR annex encoded key
        n = -fig_num
        hi, lo = n // 1000, n % 1000
        if hi < 65:                          # section-numbered figure (e.g. 6.2)
            return f"{hi}.{lo}"
        return f"{chr(hi)}.{lo}"             # annex figure (e.g. B.1)
    return None                              # counter-based negative → unlabeled
# Matches:
#   "Table D.1 —"            (em-dash / en-dash / hyphen)
#   "Table 3 (continued)"    (parenthesized variant marker)
#   "Table 14"  on its own line / end of block
#   "Table 14\nDescription"  (caption number then title on next line)
# Trailing context uses [^\S\n\r]* — horizontal whitespace only, so the
# lookahead for newline/end-of-string still fires (\s* would have eaten the \n
# itself), but U+00A0 counts as horizontal space.  ISO/IEC typesets captions
# with non-breaking spaces ("Table\xa01\xa0— Title"); a literal [ \t] class
# does NOT match U+00A0, which silently broke every primary table caption.
# Body sentences like "Table 14 lists the following" don't match because the
# next character after the optional spaces would be a lowercase letter, which
# is none of [—–\-(\n\r$].
_TABLE_CAPTION_RE   = re.compile(
    r'^(?:Table|TABLE)\s+([A-Z](?:\.\d+)+|\d+(?:\.\d+)*)'
    r'(?=[^\S\n\r]*(?:[—–\-]|\(|$|\n|\r))'
)
_TABLE_CONT_RE      = re.compile(r'^(?:Table|TABLE)\s+\S.*\(continued\)', re.IGNORECASE)
_FORMULA_TRAIL_RE   = re.compile(r'\((\d+)\)\s*$')       # "(N)" at end of block
_MATH_CHARS_RE      = re.compile(r'[=×÷±∑∫√≤≥≠°μαβγΣδεθλπσφψω]')

# ─────────────────────────────────────────────────────────────────────────────
# From backend.py:3969-3982. Declared in the document-assembly section of the
# source, but the formula anchoring in `equations.py` needs _PAGE_NUM_RE first, so
# all three live here and both consumers import them.
# ─────────────────────────────────────────────────────────────────────────────

# XML 1.0 forbids the C0 control characters except tab (0x09), LF (0x0A) and
# CR (0x0D). python-docx hands run text straight to lxml, which raises
# "All strings must be XML compatible..." the moment any other control byte is
# present. Some source PDFs embed stray control bytes in their text layer
# (e.g. 0x08 after the running footer in ISO 13485:2016) and PyMuPDF extracts
# them verbatim. Strip them before writing any run so one invisible byte can't
# abort the whole report.
_XML_ILLEGAL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _xml_safe(s):
    """Remove XML-illegal control characters from *s* (None/'' pass through)."""
    if not s:
        return s
    return _XML_ILLEGAL_RE.sub('', s)


_PAGE_NUM_RE    = re.compile(r'^\d{1,4}\s*$')
# ISO/IEC table footnote markers: "a  text", "b  text", "1)  text", "* text".
# Used to identify repeated footnote blocks that appear on every continuation
# page of a multi-page table and should be deduplicated within a section.
_FOOTNOTE_MARKER_RE = re.compile(r'^(?:[a-z]\s{1,4}\S|\d+\)\s|\*\s)', re.IGNORECASE)

# Combined pattern for highlighting references in the DOCX description column.
# Matches: "Figure 3", "Fig. 3.1", "Table 3.1", "Table A.1", "Formula (3)"
_REF_HIGHLIGHT_RE = re.compile(
    r'\bFig(?:ure)?\.?\s+\d+(?:[.\-]\d+)*'                       # Figure 3 / Fig. 3.1
    r'|\bTable\s+(?:[A-Z](?:\.\d+)+|\d+(?:\.\d+)*)\b'            # Table 3.1 / Table A.1
    r'|\bFormula\s+\(\d+\)',                                       # Formula (3)
    re.IGNORECASE,
)
