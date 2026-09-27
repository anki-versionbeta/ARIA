"""Two small helpers for reading a TOC entry. Ported from backend.py:4065-4082.

Declared in the document-assembly section of the source, but `equations.py` needs both
of them — one to locate a heading on the page, the other to derive the page range the
formula detector scans — so they live here and both consumers import them rather than
carrying a copy each.
"""

from __future__ import annotations

import re


def _section_num(title):
    """Extract the bare number/id from a section title, e.g. '6.1.3 Foo' → '6.1.3'."""
    m = re.match(r'^(Annex\s+[A-Z]|\d+(?:\.\d+)*)\s*', title, re.IGNORECASE)
    return m.group(1).strip() if m else (title.split()[0] if title else '')


def _section_end_page_0(toc, sec_idx, doc_last_page_0):
    """Return the last page (0-indexed) of the section at toc[sec_idx].

    Skips subsequent ToC entries that share the same start page so that
    child bookmarks on the same page don't cause a negative page range.
    """
    sec_start_page = toc[sec_idx]['page']
    for i in range(sec_idx + 1, len(toc)):
        if toc[i]['page'] > sec_start_page:
            return toc[i]['page'] - 1   # 0-indexed: include next section's start page so
                                          # _extract_body can capture content before its heading
    return doc_last_page_0
