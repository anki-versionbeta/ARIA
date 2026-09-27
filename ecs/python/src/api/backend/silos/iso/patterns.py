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
# Matches:
#   "Table D.1 —"            (em-dash / en-dash / hyphen)
#   "Table 3 (continued)"    (parenthesized variant marker)
#   "Table 14"  on its own line / end of block
#   "Table 14\nDescription"  (caption number then title on next line)
# Trailing context uses [ \t]* (horizontal whitespace only) so the lookahead
# for newline/end-of-string actually fires; \s* would have eaten the \n itself.
# Body sentences like "Table 14 lists the following" don't match because the
# next character after the optional spaces would be a lowercase letter, which
# is none of [—–\-(\n\r$].
_TABLE_CAPTION_RE   = re.compile(
    r'^(?:Table|TABLE)\s+([A-Z](?:\.\d+)+|\d+(?:\.\d+)*)'
    r'(?=[ \t]*(?:[—–\-]|\(|$|\n|\r))'
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
