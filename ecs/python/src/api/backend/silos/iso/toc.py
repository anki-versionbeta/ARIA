"""Finding the document's section structure. Ported from backend.py:915-2080.

This is the largest module in the silo and the one the user sees most directly: its
output is the list the range picker renders, and `start_idx`/`end_idx` are indices into
it. A TOC entry is:

    {'level': int, 'title': str, 'page': int}     # page is 1-INDEXED

`title` carries the section number as part of the text ("4.1.2 General requirements"),
because the emitter parses the number back out of it. `page` is 1-indexed everywhere in
this module's *output*, while the internal `page_idx` values are 0-indexed — the `+ 1`
conversions at the boundaries are deliberate and load-bearing.

Two entry points, chosen by whether the text layer was trusted:

* **Vision path** — `build_toc_from_llm_pages(llm_pages)`. `VirtualPdfReader` makes the
  model's per-page text look like a pypdf reader, so the same dot-leader pipeline runs
  over it. It parses *only* the real contents page(s), never body text, which is what
  keeps duplicates, dot-leader artefacts and table-row noise out of the result.
* **Text path** — `extract_toc(pdf_path, llm=...)`, a five-level fallback chain:
  bookmarks, then the dot-leader pipeline over pypdf, then a bold-font scan, then a
  section-number pattern scan, then the vision model. Each level is tried only if the
  previous returned nothing (bookmarks additionally need at least three entries to be
  trusted).

Both paths then pass through `deduplicate_toc`, which exists because some standards PDFs
contain the whole document twice — a redline copy followed by the clean one — so every
section, figure and annex is bookmarked twice.

Seam changes only: the direct `requests` call in `_toc_from_llm` becomes
`llm.chat_vision_multi` (it sends up to ten page images in a *single* request, and the
model is asked to pick the contents page out of them, so it cannot be split into one call
per image), and the `print` trail becomes `logger.info` with identical text.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from concurrent.futures import ThreadPoolExecutor

import fitz
import pypdf

from .patterns import _STD_PREFIX_RE, _WATERMARK_RE
from .vision import _ILIAD_VISION_MODEL, VISION_TOC_PROMPT, _render_page_to_base64

logger = logging.getLogger(__name__)


class VirtualPage:
    """Mimics a pypdf page backed by LLM-extracted text.

    Only implements the interface used by the TOC pipeline:
        page.extract_text() -> str
    """
    def __init__(self, text: str):
        self._text = text

    def extract_text(self) -> str:
        return self._text


class VirtualPdfReader:
    """Mimics pypdf.PdfReader using LLM-extracted page data.

    Satisfies all calls made by the stable TOC pipeline:
        len(reader.pages)
        reader.pages[i].extract_text()
    """
    def __init__(self, llm_pages):
        self.pages = [VirtualPage(p.get("text", "") if p else "") for p in llm_pages]
        self._llm_pages = llm_pages

    def __len__(self):
        return len(self.pages)


# ─────────────────────────────────────────────────────────────────────────────
# VISION PATH TOC PARSING  (ported from stable/main.py)
# Correctly identifies the TOC page(s) via dot-leader heuristic and parses
# only those lines — never scans section body text for headings.
# ─────────────────────────────────────────────────────────────────────────────

_V_DISCARD_PREFIXES = (
    "Copyrighted material licensed to",
    "No further reproduction or distribution",
    "©",
    "Â©",
    "Copyright by",
    "BS EN ISO",
)
_V_TOC_INDEX_RE  = re.compile(r"^\s*(?P<idx>\d+(?:\.\d+)*)\s+(?P<title>.+?)\s*(?P<page>\d+)?\s*$")
_V_DOT_RUN_RE    = re.compile(r"\.{6,}")          # ≥ 6 consecutive dots
_V_MIN_LEADER    = 1                               # pages with ≥ this many dot-run lines = TOC page
_V_TOC_DOTLINE_RE = re.compile(
    r"^\s*(?P<title>.+?)\s*\.{7,}\s*(?P<page>(?:\d+|[ivxlcdmIVXLCDM]+))\s*$"
)


def _v_should_discard(line):
    s = (line or "").strip()
    if not s:
        return True
    for pref in _V_DISCARD_PREFIXES:
        if s.startswith(pref):
            return True
    ls = s.lower()
    if ls.endswith('(en)') or ls.endswith('en)'):
        return True
    return False


def _v_normalize_spaces(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _v_clean_title(title):
    t = re.sub(r"\.{2,}", " ", title or "")
    return _v_normalize_spaces(t)


def _v_remove_rep_labels(text):
    pat = r"^\b(Table|Figure|Annex)s?\s+\b(Table|Figure|Annex)\b"
    return re.sub(pat, r"\2", text.strip(), flags=re.IGNORECASE)


def _v_page_lines(reader):
    """Return list-of-lines per page using reader.pages[i].extract_text()."""
    pages = []
    for p in reader.pages:
        text = p.extract_text() or ""
        pages.append(text.splitlines())
    return pages


def _v_is_leader_heavy(lines):
    count = 0
    for raw in lines:
        if _v_should_discard(raw):
            continue
        if _V_DOT_RUN_RE.search(raw):
            count += 1
            if count >= _V_MIN_LEADER:
                return True
    return False


def _v_has_trailing_page_token(s):
    if not s:
        return False
    s = s.strip()
    if re.search(r"\b\d+\s*$", s):
        return True
    if re.search(r"\b[ivxlcdmIVXLCDM]+\s*$", s):
        return True
    return False


def _v_is_isolated_page_token(s):
    s = (s or "").strip()
    return bool(re.fullmatch(r"(?:\d+|[ivxlcdmIVXLCDM]+)", s))


def _v_reflow_lines(lines):
    """Merge wrapped TOC lines into single logical entries."""
    kept = [
        _v_normalize_spaces(ln)
        for ln in lines
        if not _v_should_discard(ln) and not (ln or "").strip().lower().startswith('contents')
    ]
    out = []
    i = 0
    while i < len(kept):
        merged = kept[i]
        last_j = i

        def _complete(s):
            if _V_TOC_DOTLINE_RE.match(s):
                return True
            if _V_TOC_INDEX_RE.match(s) and _v_has_trailing_page_token(s):
                return True
            if _V_DOT_RUN_RE.search(s) and _v_has_trailing_page_token(s):
                return True
            return False

        def _incomplete(s):
            if _v_has_trailing_page_token(s):
                return False
            if re.match(r"^\s*\d+(?:\.\d+)*\b", s):
                return True
            if _V_DOT_RUN_RE.search(s):
                return True
            return len(s) > 0 and not s.endswith(('.', ':', ';', ','))

        k = i + 1
        while k < len(kept) and (k - i) <= 2:
            if _complete(merged):
                break
            nxt  = kept[k]
            trial = _v_normalize_spaces(merged + ' ' + nxt)
            if _complete(trial):
                merged = trial
                last_j = k
                break
            cont = (
                nxt.startswith('.') or
                _V_DOT_RUN_RE.search(nxt) is not None or
                _v_is_isolated_page_token(nxt)
            )
            if _incomplete(merged) or cont:
                merged = trial
                last_j = k
                k += 1
                continue
            else:
                break

        out.append(merged)
        i = last_j + 1
    return out


def _v_find_contents_blocks(pages):
    """Find TOC lines from pages that contain the TOC section.

    A page qualifies as a TOC page if it has a 'Contents' header line OR is
    dot-leader heavy (≥ _V_MIN_LEADER lines with long dot runs).
    Only those pages are parsed for section entries — body pages are ignored.

    Returns (toc_lines, toc_start_page, toc_end_page).
    """
    toc_lines = []
    toc_started = False
    toc_start_page = -1
    toc_end_page = -1

    for pi, lines in enumerate(pages):
        contents_idx = None
        for li, raw in enumerate(lines):
            if _v_should_discard(raw):
                continue
            if raw.strip().lower() == 'contents':
                contents_idx = li
                break

        leader_heavy = _v_is_leader_heavy(lines)
        start_collect = None
        if contents_idx is not None:
            start_collect = contents_idx + 1
        elif toc_started:
            start_collect = 0
        elif leader_heavy:
            start_collect = 0

        if start_collect is None:
            if toc_started:
                break
            continue

        reflowed = _v_reflow_lines(lines[start_collect:])
        indexed_here = []
        for raw in reflowed:
            if _v_should_discard(raw):
                continue
            s = _v_normalize_spaces(raw)
            if _V_TOC_INDEX_RE.match(s) and re.match(r"^\s*\d+(?:\.\d+)*\b", s):
                indexed_here.append(s)
                continue
            if _V_TOC_DOTLINE_RE.match(s):
                indexed_here.append(s)

        if indexed_here:
            if not toc_started:
                toc_started = True
                toc_start_page = pi
            toc_end_page = pi
            for s in indexed_here:
                toc_lines.append((pi, s))
        else:
            if toc_started:
                break

    return toc_lines, toc_start_page, (toc_end_page if toc_end_page != -1 else toc_start_page)


def _v_parse_toc_entries(toc_lines):
    entries = []
    for _, line in toc_lines:
        s = line
        m = _V_TOC_INDEX_RE.match(s)
        if m and re.match(r"^\s*\d+(?:\.\d+)*\b", s):
            idx   = m.group('idx')
            title = _v_clean_title(m.group('title') or '')
            title = _v_remove_rep_labels(title)
            toc_page_str = (m.group('page') or '').strip()
            entries.append({
                'index':    idx,
                'level':    str(len(idx.split('.'))),
                'title':    title,
                'toc_page': int(toc_page_str) if toc_page_str.isdigit() else 0,
            })
            continue
        m2 = _V_TOC_DOTLINE_RE.match(s)
        if m2:
            title = _v_clean_title(m2.group('title') or '')
            toc_page_str = (m2.group('page') or '').strip()
            entries.append({
                'index':    '',
                'level':    '0',
                'title':    title,
                'toc_page': int(toc_page_str) if toc_page_str.isdigit() else 0,
            })
    return entries


def _v_resolve_outlines(reader):
    """Top-level TOC extraction using VirtualPdfReader.

    Returns {'entries': [...], 'toc_start': int, 'toc_end': int}.
    """
    pages = _v_page_lines(reader)
    toc_lines, toc_start, toc_end = _v_find_contents_blocks(pages)
    if not toc_lines:
        return {'entries': [], 'toc_start': -1, 'toc_end': -1}
    entries = _v_parse_toc_entries(toc_lines)
    return {'entries': entries, 'toc_start': toc_start, 'toc_end': toc_end}


def _v_extract_page_text(reader, page_idx):
    """Extract text for one page from a VirtualPdfReader (or any pypdf-compatible reader)."""
    try:
        return reader.pages[page_idx].extract_text() or ""
    except Exception:
        return ""


def _v_build_title_patterns(title):
    """Build ordered regex patterns for matching a section heading on a page."""
    t = (title or "").strip()
    if not t:
        return []
    escaped  = re.escape(t)
    flex_ws  = re.sub(r"\\\s+", r"\\s+", escaped)
    pat1     = re.compile(rf"(?m)^\s*{flex_ws}\s*$",             re.IGNORECASE)
    pat2     = re.compile(flex_ws,                                re.IGNORECASE)
    pat3     = re.compile(rf"(?m)^\s*\d+(?:\.\d+)*\s+{flex_ws}\s*$", re.IGNORECASE)
    return [pat1, pat3, pat2]


def _v_find_page_by_title(reader, title, pages_text_cache=None):
    """Return the 0-based page index where *title* is found, or None."""
    if not title:
        return None
    patterns = _v_build_title_patterns(title)
    if not patterns:
        return None
    n = len(reader.pages)
    if pages_text_cache is None:
        pages_text_cache = [_v_extract_page_text(reader, p) for p in range(n)]

    best_page  = None
    best_score = None

    for p in range(n):
        text = pages_text_cache[p] or ""
        if not text:
            continue
        lines = text.splitlines()
        head  = "\n".join(lines[:60]) if lines else text
        for pat in patterns:
            m = pat.search(head)
            if m:
                score = m.start()
                if best_score is None or score < best_score:
                    best_score = score
                    best_page  = p
                break

    # Full-page fallback
    if best_page is None:
        for p in range(n):
            text = pages_text_cache[p] or ""
            for pat in patterns:
                if pat.search(text):
                    best_page = p
                    break
            if best_page is not None:
                break

    return best_page


def _v_flatten_outlines(entries, reader, toc_start=-1, toc_end=-1):
    """Resolve each TOC entry to a page index and return an ordered flat list.

    Strategy (matching stable/main.py):
    1. Search every page for the title text → page_idx
    2. Fallback: use TOC-printed page number + median offset from successful matches
    3. Monotonicity pass: section page indices must be non-decreasing
    Returns list of {level, title, page_idx}.
    """
    n_pages = len(reader.pages)
    pages_text_all    = [_v_extract_page_text(reader, p) for p in range(n_pages)]
    pages_text_search = pages_text_all[:]
    # Blank out TOC pages so heading search can't match TOC lines
    if toc_start != -1 and toc_end != -1:
        for p in range(toc_start, toc_end + 1):
            pages_text_search[p] = ""

    def _process(e):
        idx_str = (e.get('index') or '').strip()
        ttl     = (e.get('title') or '').strip()
        query   = f"{idx_str} {ttl}".strip() if idx_str else ttl
        page_idx = _v_find_page_by_title(reader, query,
                                         pages_text_cache=pages_text_search)
        return {
            'level':    int(e.get('level') or 0),
            'title':    query,
            'page_idx': page_idx,
            'toc_page': e.get('toc_page', 0),
        }

    # `ex.map` keeps submission order, which is what makes the monotonicity pass below
    # meaningful. `as_completed` would reorder the sections.
    with ThreadPoolExecutor(max_workers=8) as ex:
        flat_list = list(ex.map(_process, entries))

    # Offset from resolved entries (median — robust to outliers)
    samples = [
        item['page_idx'] - (item['toc_page'] - 1)
        for item in flat_list
        if item['page_idx'] is not None and item['toc_page'] > 0
    ]
    if samples:
        samples.sort()
        offset = samples[len(samples) // 2]
    else:
        offset = 0
    logger.info("[toc-vision] page_offset=%s from %d samples", offset, len(samples))

    for item in flat_list:
        if item['page_idx'] is None:
            if item['toc_page'] > 0:
                item['page_idx'] = max(0, min(item['toc_page'] - 1 + offset, n_pages - 1))
            else:
                item['page_idx'] = 0

    # Monotonicity: page indices must be non-decreasing
    for i in range(1, len(flat_list)):
        if flat_list[i]['page_idx'] < flat_list[i - 1]['page_idx']:
            flat_list[i]['page_idx'] = flat_list[i - 1]['page_idx']

    return [
        {'level': item['level'], 'title': item['title'], 'page_idx': item['page_idx']}
        for item in flat_list
    ]


def _build_toc_from_llm_pages_stable(llm_pages):
    """Build accurate TOC from vision-LLM pages using the stable TOC pipeline.

    Uses VirtualPdfReader so _v_resolve_outlines works on LLM-extracted text.
    _v_find_contents_blocks identifies only the TOC page(s) — body pages are
    never scanned for headings, preventing the duplicates / noise seen with
    the old _toc_from_llm_pages approach.

    Returns list of {level, title, page} matching new_test_app's toc format.
    """
    virtual_reader = VirtualPdfReader(llm_pages or [])
    info    = _v_resolve_outlines(virtual_reader)
    entries = info['entries']

    if not entries:
        logger.info("[toc-vision] _v_resolve_outlines found no TOC entries")
        return []

    flat = _v_flatten_outlines(entries, virtual_reader,
                               toc_start=info['toc_start'],
                               toc_end=info['toc_end'])

    # Convert to new_test_app toc format: page is 1-indexed
    toc = [
        {'level': e['level'], 'title': e['title'], 'page': e['page_idx'] + 1}
        for e in flat
    ]

    # Strip leading standard-number running-header prefix that the vision LLM
    # sometimes concatenates with the real section title when the TOC page has
    # the running header on the same line as the entry
    # (e.g. "ISO 11608-4:2022(E) 8.10.6 NIS-E …" → "8.10.6 NIS-E …").
    # _deduplicate_toc will then collapse any phantom entry that becomes
    # identical to a real entry after stripping.
    stripped_count = 0
    for entry in toc:
        cleaned = _STD_PREFIX_RE.sub('', entry['title'], count=1).strip()
        if cleaned and cleaned != entry['title']:
            logger.info("[toc-vision] stripped prefix: %r → %r", entry['title'], cleaned)
            entry['title'] = cleaned
            stripped_count += 1
    if stripped_count:
        logger.info(
            "[toc-vision] stripped running-header prefix from %d entries", stripped_count
        )

    logger.info("[toc-vision] built %d entries via stable pipeline", len(toc))
    if toc:
        logger.info("[toc-vision] first 5: %s", [e['title'] for e in toc[:5]])
    return toc


# ─────────────────────────────────────────────────────────────────────────────
# TOC DEDUPLICATION GUARDRAIL
# ─────────────────────────────────────────────────────────────────────────────
# Some standards PDFs contain the full document twice (e.g. a redline version
# followed by the clean international standard).  The bookmark tree therefore
# lists every section, figure, table, and annex twice.  This guardrail runs
# between extraction and display to keep only the first occurrence of each
# topic (by ascending page number) and ensures the result is continuous.

# Characters that are noise in ISO bookmark titles — non-breaking spaces,
# soft hyphens, zero-width chars, bullet-like markers, asterisks, etc.
_TITLE_NOISE_RE = re.compile(
    r'[\u00a0\u00ad\u200b\u200c\u200d\ufeff\u2022\u2023\u25aa\u25cf'
    r'\u00b7\u2013\u2014\u2012*\u0000-\u001f]'
)


def _normalize_toc_title(title):
    """Produce a canonical key from a TOC title for duplicate detection.

    Handles the common noise found in PDF bookmarks:
    - Non-ASCII punctuation / special chars (non-breaking spaces, soft hyphens,
      zero-width chars, bullet markers, asterisks) — replaced with space so
      adjacent words are not accidentally joined (e.g. "Annex A" → "Annex A")
    - Inconsistent whitespace (multiple spaces, tabs, leading/trailing)
    - Trailing / leading punctuation differences
    - Case differences
    - Missing / extra spaces around parentheses like "(informative)"
    """
    # Replace noise chars with a space (not empty) to avoid joining words
    t = _TITLE_NOISE_RE.sub(' ', title or '')
    t = re.sub(r'\s+', ' ', t).strip().lower()
    # Normalise spaces around parentheses: "(informative)General" → "(informative) general"
    t = re.sub(r'\)\s*', ') ', t)
    t = re.sub(r'\s*\(', ' (', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Strip leading/trailing punctuation that varies between copies
    t = t.strip('.,;:!? ')
    return t


# Regex to extract the structural identifier from a TOC title.
# Matches: "4.3", "4.3.1", "Annex A", "Figure 9", "Figure A.8",
#           "Table 11", "Table A.6", etc.
# The figure/table number part uses (?:[a-z]\.?)? to handle both "9" and "A.8".
_TOC_ID_RE = re.compile(
    r'^\s*(?:'
    r'(?P<sec>\d+(?:\.\d+)*)'                                  # 4.3.1
    r'|(?P<annex>annex\s+[a-z])'                                # Annex A
    r'|(?P<fig>figure\s+(?:[a-z]\.?)?\d+(?:\.\d+)*)'            # Figure 9 / Figure A.8
    r'|(?P<tbl>table\s+(?:[a-z]\.?)?\d+(?:\.\d+)*)'             # Table 11 / Table A.6
    r')\b',
    re.IGNORECASE,
)


def _extract_toc_id(title):
    """Extract the structural identifier from a normalised TOC title.

    Returns a lowercase string like "sec:4.3.1", "annex:a", "figure:9",
    "table:a.6", or None if no identifier is found.
    """
    m = _TOC_ID_RE.match(title.strip())
    if not m:
        return None
    if m.group('sec'):
        return 'sec:' + m.group('sec').strip()
    if m.group('annex'):
        return 'annex:' + m.group('annex').split()[-1].lower()
    if m.group('fig'):
        return 'figure:' + re.sub(r'\s+', '', m.group('fig').lower().replace('figure', ''))
    if m.group('tbl'):
        return 'table:' + re.sub(r'\s+', '', m.group('tbl').lower().replace('table', ''))
    return None


def _deduplicate_toc(toc):
    """Remove duplicate TOC entries, using the last occurrence's page number.

    Two-pass algorithm:

    Pass 1 — **Full normalised title**: handles entries whose titles are
    identical or differ only in whitespace / special characters.

    Pass 2 — **Structural identifier**: catches entries whose descriptive
    text changed between editions (e.g. "Table 11 — Minimum creepage…" vs
    "Table 11 — Not used") but whose section/figure/table number is the same.

    In both passes the first occurrence's title/level is kept but the page
    number is replaced with the *last* (highest-page) occurrence so that the
    final report points to the clean / latest copy of the section.
    """
    if not toc:
        return toc

    # ── Pass 1: deduplicate by full normalised title ─────────────────────
    # Build a map: normalised title → max page number across all occurrences
    title_max_page = {}
    for entry in toc:
        key = _normalize_toc_title(entry['title'])
        if not key:
            continue
        if key not in title_max_page or entry['page'] > title_max_page[key]:
            title_max_page[key] = entry['page']

    indexed = list(enumerate(toc))
    indexed.sort(key=lambda pair: (pair[1]['page'], pair[0]))

    seen_titles = set()
    after_pass1 = []
    for _orig_idx, entry in indexed:
        key = _normalize_toc_title(entry['title'])
        if not key:
            continue
        if key in seen_titles:
            continue
        seen_titles.add(key)
        # Keep first occurrence's title/level but use last occurrence's page
        after_pass1.append({**entry, 'page': title_max_page[key]})

    # ── Pass 2: deduplicate by structural identifier ─────────────────────
    # Build a map: struct_id → max page number across all Pass-1 survivors
    id_max_page = {}
    for entry in after_pass1:
        norm = _normalize_toc_title(entry['title'])
        struct_id = _extract_toc_id(norm)
        if struct_id:
            if struct_id not in id_max_page or entry['page'] > id_max_page[struct_id]:
                id_max_page[struct_id] = entry['page']

    after_pass1.sort(key=lambda e: e['page'])
    seen_ids = set()
    kept = []
    for entry in after_pass1:
        norm = _normalize_toc_title(entry['title'])
        struct_id = _extract_toc_id(norm)
        if struct_id and struct_id in seen_ids:
            continue
        if struct_id:
            seen_ids.add(struct_id)
            # Use last occurrence's page for this structural ID
            entry = {**entry, 'page': id_max_page[struct_id]}
        kept.append(entry)

    # Re-sort by page to ensure the final list is in reading order
    kept.sort(key=lambda e: e['page'])

    # ── Pass 3: remove orphan entries with abnormal page gaps ────────────
    # After the last-occurrence page swap, entries that existed only in the
    # earlier copy (e.g. "Redline version" at page 5) remain at their
    # original low page while everything else moved to the later copy's
    # pages.  These orphans show up as entries with a huge gap (> threshold)
    # to the next entry.  Removing them cleans up the final TOC.
    _GAP_THRESHOLD = 70
    if len(kept) >= 2:
        filtered = []
        for i, entry in enumerate(kept):
            if i + 1 < len(kept):
                gap = kept[i + 1]['page'] - entry['page']
                if gap > _GAP_THRESHOLD:
                    logger.info(
                        "[toc-dedup] pass 3: removing orphan p%s (gap %s to next) \"%s\"",
                        entry['page'],
                        gap,
                        entry['title'][:60],
                    )
                    continue
            filtered.append(entry)
        kept = filtered

    # ── Pass 4: remove Figure / Table / category bookmark entries ────────
    # Some PDFs include individual Figure and Table bookmarks alongside the
    # real section bookmarks.  These point to pages already covered by the
    # numbered section entries, causing content to be extracted twice.
    # Filter them out — they are index entries, not real sections.
    _NON_SECTION_RE = re.compile(
        r'^(Figures?|Tables?|Bibliography|INDEX|Contents)\b', re.IGNORECASE
    )
    before_pass4 = len(kept)
    kept = [e for e in kept if not _NON_SECTION_RE.match(e['title'].strip())]
    pass4_removed = before_pass4 - len(kept)
    if pass4_removed:
        logger.info(
            "[toc-dedup] pass 4: removed %d Figure/Table/index entries", pass4_removed
        )

    removed = len(toc) - len(kept)
    if removed:
        logger.info(
            "[toc-dedup] removed %d duplicate/orphan entries (%d → %d)",
            removed,
            len(toc),
            len(kept),
        )
    else:
        logger.info("[toc-dedup] no duplicates found (%d entries)", len(toc))
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# TABLE OF CONTENTS EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_toc(pdf_path, *, llm=None):
    """Extract ToC from PDF using a five-level fallback chain.

    1. PDF bookmarks (fitz.get_toc)
    2. pypdf dot-leader TOC page detection
    3. Font-based bold heading scan
    4. Section-number pattern scan (no formatting needed)
    5. LLM-based TOC construction (last resort)

    `llm` is keyword-only and may be None, in which case level 5 is skipped and an
    otherwise-unreadable document yields an empty TOC rather than raising.
    """
    logger.info("[toc] extracting ToC from: %s", pdf_path)
    doc = fitz.open(pdf_path)
    raw_toc = doc.get_toc()          # [[level, title, page], ...]
    total_pages = len(doc)
    doc.close()

    logger.info("[toc] fitz.get_toc() returned %d raw bookmark entries", len(raw_toc))

    # ── Fallback 1: PDF bookmarks ────────────────────────────────────────
    if raw_toc:
        result = []
        for level, title, page in raw_toc:
            title = title.strip()
            if title and page > 0:
                result.append({'level': level, 'title': title, 'page': page})
        logger.info("[toc] after filtering: %d valid bookmark entries", len(result))
        # Fewer than three is not a table of contents; it is usually a cover-page
        # bookmark and a stray, so the later fallbacks do better.
        if len(result) >= 3:
            logger.info("[toc] using bookmarks. first 5: %s", [e['title'] for e in result[:5]])
            return result
        logger.info("[toc] too few bookmarks (<3), falling back")

    # ── Fallback 2: pypdf dot-leader TOC page detection ──────────────────
    logger.info("[toc] running pypdf dot-leader TOC detection (_build_toc_from_pypdf)")
    result = _build_toc_from_pypdf(pdf_path)
    logger.info("[toc] pypdf TOC found %d entries", len(result))
    if result:
        logger.info("[toc] first 5: %s", [e['title'] for e in result[:5]])
        return result

    # ── Fallback 3: font-based bold heading scan ─────────────────────────
    logger.info("[toc] running font-based heading scan (_toc_from_text)")
    result = _toc_from_text(pdf_path)
    logger.info("[toc] font scan found %d entries", len(result))
    if result:
        logger.info("[toc] first 5: %s", [e['title'] for e in result[:5]])
        return result

    # ── Fallback 4: section-number pattern scan (no formatting needed) ───
    logger.info("[toc] running section-number pattern scan (_toc_from_section_patterns)")
    result = _toc_from_section_patterns(pdf_path)
    logger.info("[toc] pattern scan found %d entries", len(result))
    if result:
        logger.info("[toc] first 5: %s", [e['title'] for e in result[:5]])
        return result

    # ── Fallback 5: LLM-based TOC construction (last resort) ────────────
    logger.info("[toc] all local methods exhausted, trying LLM-based TOC construction")
    result = _toc_from_llm(pdf_path, llm=llm) if llm is not None else []
    logger.info("[toc] LLM TOC found %d entries", len(result))
    if result:
        logger.info("[toc] first 5: %s", [e['title'] for e in result[:5]])
        return result

    logger.info("[toc] all fallbacks exhausted, returning empty TOC")
    return []


def _toc_from_text(pdf_path):
    """Detect headings from font analysis when bookmarks are absent.

    Two-pass approach:
    Pass A — Bold text with section-number patterns (original logic).
    Pass B — If Pass A finds nothing, fall back to font-size analysis:
             compute the median body font size and treat any span whose size
             exceeds the median by ≥ 1.5 pt as a heading candidate, provided
             it matches a section-number or Annex pattern or is a known
             front-matter title (Foreword, Introduction, Scope, etc.).
    """
    doc = fitz.open(pdf_path)
    entries = []
    seen_headings = set()

    section_re = re.compile(r'^(\d+(?:\.\d+)*)\s+\S')
    annex_re   = re.compile(r'^(Annex\s+[A-Z])\b', re.IGNORECASE)

    # ── Pass A: bold text ────────────────────────────────────────────────
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_h = page.rect.height

        for block in page.get_text("dict", flags=0)["blocks"]:
            if block["type"] != 0:
                continue
            by0, by1 = block["bbox"][1], block["bbox"][3]
            if by1 < 65 or by0 > page_h - 65:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if len(text) < 3:
                        continue
                    is_bold = bool(span["flags"] & 16)
                    size = span["size"]

                    if is_bold and size >= 8:
                        m = section_re.match(text) or annex_re.match(text)
                        if m and text not in seen_headings:
                            seen_headings.add(text)
                            num = m.group(1)
                            level = 1 if annex_re.match(text) else len(num.split('.'))
                            entries.append({'level': level, 'title': text, 'page': page_num + 1})

    if entries:
        doc.close()
        return entries

    # ── Pass B: font-size analysis (no bold required) ────────────────────
    # Accept ANY short text at heading font size as a heading candidate.
    # Filter out cover-page noise and document references.
    logger.info("[toc] bold scan found nothing, trying font-size analysis")

    # Patterns to SKIP — generic boilerplate that can appear at heading sizes.
    # Document-specific title text is handled by page skipping (first 3 + last 1),
    # so no hardcoded document words are needed here.
    _SKIP_PATTERNS = [
        re.compile(r'^(BS\s+)?ISO[\s/]', re.IGNORECASE),           # "BS ISO 11040-2:2011"
        re.compile(r'^IEC\s', re.IGNORECASE),                       # "IEC 60601..."
        re.compile(r'^(INTERNATIONAL|EUROPEAN)\s', re.IGNORECASE),   # "INTERNATIONAL STANDARD"
        re.compile(r'^(BSI|ANSI|AAMI|DIN|EN)\b', re.IGNORECASE),    # standards body names
        re.compile(r'^(British Standards Institution)', re.IGNORECASE),
        re.compile(r'^\d{4,}'),                                       # bare large numbers
        re.compile(r'^Copyrighted|^No further|^All rights', re.IGNORECASE),
        re.compile(r'^\(normative\)|^\(informative\)', re.IGNORECASE),  # orphan annex type labels
    ]
    # Skip first 3 pages (cover/title) and last page (back cover/BSI info)
    _skip_first = 3
    _skip_last  = 1

    # Collect all font sizes to compute median body size
    all_sizes = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_h = page.rect.height
        for block in page.get_text("dict", flags=0)["blocks"]:
            if block["type"] != 0:
                continue
            by0, by1 = block["bbox"][1], block["bbox"][3]
            if by1 < 65 or by0 > page_h - 65:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if len(text) >= 3 and span["size"] >= 6:
                        all_sizes.append(span["size"])

    if not all_sizes:
        doc.close()
        return []

    all_sizes.sort()
    median_size = all_sizes[len(all_sizes) // 2]
    heading_threshold = median_size + 1.5
    logger.info(
        "[toc] median body size=%.1fpt, heading threshold=%.1fpt",
        median_size,
        heading_threshold,
    )

    seen_headings = set()
    total_pages = len(doc)
    for page_num in range(_skip_first, max(total_pages - _skip_last, _skip_first)):
        page = doc[page_num]
        page_h = page.rect.height

        for block in page.get_text("dict", flags=0)["blocks"]:
            if block["type"] != 0:
                continue
            by0, by1 = block["bbox"][1], block["bbox"][3]
            if by1 < 65 or by0 > page_h - 65:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if len(text) < 3 or span["size"] < heading_threshold:
                        continue

                    # Skip very long text (headings are short)
                    if len(text) > 80:
                        continue

                    # Skip cover-page / boilerplate noise
                    if any(p.match(text) for p in _SKIP_PATTERNS):
                        continue

                    # Skip watermarks
                    if _WATERMARK_RE.search(text[:120]):
                        continue

                    # Must start with an uppercase letter or digit
                    if not text[0].isupper() and not text[0].isdigit():
                        continue

                    if text in seen_headings:
                        continue
                    seen_headings.add(text)

                    # Determine level
                    m = section_re.match(text)
                    if m:
                        num = m.group(1)
                        level = 1 if annex_re.match(text) else len(num.split('.'))
                    elif annex_re.match(text):
                        level = 1
                    else:
                        level = 1   # unnumbered heading

                    entries.append({'level': level, 'title': text, 'page': page_num + 1})

    doc.close()
    logger.info("[toc] font-size analysis found %d entries", len(entries))
    return entries


def _toc_from_section_patterns(pdf_path):
    """Detect section headings by ISO numbering patterns in raw text blocks.

    Scans all pages using fitz text blocks (no font analysis needed).
    Matches lines starting with ISO section numbers (e.g. '4.1.2 Title')
    or 'Annex A' patterns.  Validates hierarchical consistency before returning.

    Filters applied to reduce noise:
    - Skip dot-leader lines (belong to the printed TOC page, handled by fallback 2)
    - Cap top-level section numbers at 30 (filters addresses like "901 N. Glebe Road")
    - For single-level numbers (no dots), require title starts with uppercase and
      line length ≤ 80 chars (top-level headings are short and capitalized)
    - Skip lines where text after the number is purely numeric (table rows)

    Returns list of {'level': int, 'title': str, 'page': int} (1-indexed).
    Returns [] if too few headings found or validation fails.
    """
    logger.info("[toc-pattern] scanning for section-number patterns: %s", pdf_path)
    doc = fitz.open(pdf_path)
    entries = []
    seen_nums = set()

    section_re = re.compile(r'^(\d+(?:\.\d+)*)\s+(\S.*)$')
    annex_re   = re.compile(r'^(Annex\s+[A-Z])\b(.*)', re.IGNORECASE)
    pure_numeric_tail_re = re.compile(r'^[\d\s.,;%()+-]+$')
    dot_leader_re = re.compile(r'\.{6,}')

    for page_num in range(len(doc)):
        page   = doc[page_num]
        page_h = page.rect.height

        for blk in page.get_text("blocks", sort=True):
            x0, y0, x1, y1, text, _, btype = blk

            if btype != 0:
                continue
            if y1 < 65 or y0 > page_h - 65:
                continue
            if _WATERMARK_RE.search(text[:120]):
                continue

            for raw_line in text.split('\n'):
                line = raw_line.strip()
                if len(line) < 3 or len(line) > 120:
                    continue

                # Skip dot-leader lines (TOC page artifacts)
                if dot_leader_re.search(line):
                    continue

                # Try section number match
                m = section_re.match(line)
                is_annex = False
                if not m:
                    m = annex_re.match(line)
                    if m:
                        is_annex = True
                if not m:
                    continue

                num_part  = m.group(1)
                tail_part = m.group(2).strip() if m.lastindex >= 2 else ""

                # Filter: text after the number should not be purely numeric
                if tail_part and pure_numeric_tail_re.match(tail_part):
                    continue

                if not is_annex:
                    top_level = int(num_part.split('.')[0])
                    # Cap top-level numbers at 30 (ISO standards never exceed ~20)
                    if top_level > 30:
                        continue
                    # For single-level numbers (e.g. "4 General requirements"):
                    # require uppercase start and short line to filter body text
                    if '.' not in num_part:
                        if not tail_part or not tail_part[0].isupper():
                            continue
                        if len(line) > 80:
                            continue

                # Deduplication by section number
                dedup_key = num_part.lower()
                if dedup_key in seen_nums:
                    continue
                seen_nums.add(dedup_key)

                level = 1 if is_annex else len(num_part.split('.'))
                entries.append({
                    'level': level,
                    'title': line,
                    'page':  page_num + 1,
                })

    doc.close()

    if not _validate_section_hierarchy(entries):
        logger.info("[toc-pattern] hierarchy validation failed, returning []")
        return []

    logger.info(
        "[toc-pattern] found %d entries via section-number patterns", len(entries)
    )
    return entries


def _validate_section_hierarchy(entries):
    """Validate that extracted section numbers form a plausible hierarchy.

    Rules:
    - Must have at least 3 entries.
    - For numbered sections: if '4.1' exists, parent '4' should too.
      Allows up to 30 % orphans (some standards skip intermediate levels).
    - Top-level numbers should be roughly sequential — rejects if any
      consecutive gap > 10 (catches false positives from reference numbers).

    Returns True if the entries look like a valid TOC, False otherwise.
    """
    if len(entries) < 3:
        return False

    section_re = re.compile(r'^(\d+(?:\.\d+)*)')
    all_nums = set()
    numbered = []
    for e in entries:
        m = section_re.match(e['title'])
        if m:
            all_nums.add(m.group(1))
            numbered.append(m.group(1))

    if not numbered:
        return len(entries) >= 3   # All Annex entries — still valid

    # Check parent existence
    orphan_count = 0
    for num in numbered:
        parts = num.split('.')
        if len(parts) > 1:
            parent = '.'.join(parts[:-1])
            if parent not in all_nums:
                orphan_count += 1

    orphan_ratio = orphan_count / len(numbered)
    # For small TOCs (< 15 entries), be lenient — the document may only
    # contain subsections of a single parent (e.g. 7.1, 7.2, 7.2.1).
    max_orphan = 0.50 if len(entries) < 15 else 0.30
    if orphan_ratio > max_orphan:
        logger.info(
            "[toc-pattern] too many orphan sections: %d/%d (%.0f%% > %.0f%%)",
            orphan_count,
            len(numbered),
            orphan_ratio * 100,
            max_orphan * 100,
        )
        return False

    # Check top-level sequential-ness
    unique_tops = sorted(set(int(n.split('.')[0]) for n in numbered))
    if len(unique_tops) >= 2:
        max_gap = max(unique_tops[i + 1] - unique_tops[i]
                      for i in range(len(unique_tops) - 1))
        if max_gap > 10:
            logger.info("[toc-pattern] top-level gap too large: %d", max_gap)
            return False

    return True


def _toc_from_llm(pdf_path, *, llm):
    """Construct TOC via the vision model as a last resort.

    Renders the first ~10 pages of the PDF and sends them to the LLM
    with a dedicated prompt asking for structured section identification.

    Returns list of {'level': int, 'title': str, 'page': int} (1-indexed).
    Returns [] on any failure.

    All ten images go in ONE request. The prompt asks the model to find the printed
    contents page among them and to fall back to inferring structure from body headings,
    which it can only do by seeing them together — so this cannot become one call per
    page without changing the question.
    """
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        max_pages = min(10, total_pages)

        # Build the image list: the client puts the text prompt before them
        images = []
        for page_idx in range(max_pages):
            b64, pw, ph = _render_page_to_base64(doc, page_idx, dpi=150)
            images.append(b64)
            logger.info("[toc-llm] rendered page %d (%.0fx%.0f pts)", page_idx, pw, ph)
        doc.close()

        logger.info("[toc-llm] sending %d page images in one request", max_pages)
        raw = llm.chat_vision_multi(
            images=images,
            prompt=VISION_TOC_PROMPT,
            model=_ILIAD_VISION_MODEL,
            max_tokens=REDACTED
            timeouts=(180,),
        ).strip()
        logger.info("[toc-llm] response len=%d  preview=%r", len(raw), raw[:150])

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.rstrip())

        llm_entries = json.loads(raw)
        if not isinstance(llm_entries, list):
            logger.info("[toc-llm] response is not a list, returning []")
            return []

        # Convert to standard TOC format
        sec_re = re.compile(r'^(\d+(?:\.\d+)*)')
        ann_re = re.compile(r'^Annex\s+[A-Z]', re.IGNORECASE)

        toc = []
        for item in llm_entries:
            sec_num = str(item.get("section_number", "")).strip()
            title   = str(item.get("title", "")).strip()
            page    = item.get("page_number", 0)

            if not sec_num and not title:
                continue

            # Build full title: "4.1.2 General requirements"
            if sec_num and title:
                full_title = f"{sec_num} {title}"
            elif sec_num:
                full_title = sec_num
            else:
                full_title = title

            # Calculate level
            m_sec = sec_re.match(sec_num)
            m_ann = ann_re.match(sec_num)
            if m_sec:
                level = len(m_sec.group(1).split('.'))
            elif m_ann:
                level = 1
            else:
                level = 1   # Foreword, Introduction, Bibliography, etc.

            try:
                page = int(page)
            except (TypeError, ValueError):
                page = 0

            toc.append({
                'level': level,
                'title': full_title,
                'page':  max(page, 1),
            })

        # Filter out page-1 (unknown) entries if we have enough page-known entries
        known_page = [e for e in toc if e['page'] > 1]
        if len(known_page) >= 3:
            toc = known_page

        logger.info("[toc-llm] parsed %d TOC entries from LLM response", len(toc))
        return toc

    except Exception as exc:
        logger.info("[toc-llm] error: %s", exc)
        logger.info("[toc-llm] %s", traceback.format_exc())
        return []


def _build_toc_from_pypdf(pdf_path):
    """Extract TOC from a PDF using pypdf + the stable dot-leader pipeline.

    Works for PDFs without bookmarks and without bold text by detecting the
    TOC page via dot-leader density, parsing its entries, then resolving
    actual page numbers via title search + monotonicity correction.
    Falls back gracefully to [] when no TOC page is found.
    """
    try:
        reader = pypdf.PdfReader(pdf_path)
        info = _v_resolve_outlines(reader)
        entries = info.get('entries', [])
        if not entries:
            logger.info("[toc-pypdf] _v_resolve_outlines found no TOC entries")
            return []
        flat = _v_flatten_outlines(entries, reader,
                                   toc_start=info['toc_start'],
                                   toc_end=info['toc_end'])
        toc = [{'level': e['level'], 'title': e['title'], 'page': e['page_idx'] + 1}
               for e in flat]
        logger.info("[toc-pypdf] built %d entries via dot-leader pipeline", len(toc))
        return toc
    except Exception:
        logger.info("[toc-pypdf] error: %s", traceback.format_exc())
        return []


# _toc_from_llm_pages removed — replaced by _build_toc_from_llm_pages_stable
# which uses the stable TOC pipeline (dot-leader page detection + flatten_outlines)


# Names the stage calls. Aliases rather than renames, so the ported bodies above stay
# line-for-line comparable against backend.py during validation.
build_toc_from_llm_pages = _build_toc_from_llm_pages_stable
deduplicate_toc = _deduplicate_toc
