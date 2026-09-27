"""Section structure extraction (`silos/iso/toc.py`).

The output of this module is what the range picker renders, and `start_idx`/`end_idx`
are indices into it, so the entry shape is asserted explicitly.

Offline throughout: synthetic PDFs built with `fitz`, and a stub for the model. Whether
the fallbacks pick the right structure out of a real 200-page standard is a golden-file
question, not a unit-test one; what is pinned here is the entry contract, the precedence
of the five fallbacks, and the deduplication passes that exist because some standards
ship the whole document twice.
"""

from __future__ import annotations

import json

import pytest

from da_platform.settings import REPO_ROOT
from da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, StubLlmError

ISO_DIR = REPO_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "toc.py").is_file(), reason="the ISO silo is not present"
)

NBSP = chr(0x00A0)


@pytest.fixture(scope="module")
def toc():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "toc.py", name="toc")


def build_pdf(path, *, pages=6, bookmarks=None, contents_page=True, heading_size=11):
    """A synthetic ISO-shaped PDF: cover, a dot-leader contents page, then body pages.

    Six pages minimum: the font scan's second pass skips the first three and the last
    one, so a shorter document leaves it nothing to look at. Headings sit well inside the
    page because blocks within 65pt of either edge are treated as header/footer.
    """
    import fitz

    document = fitz.open()

    cover = document.new_page(width=595, height=842)
    cover.insert_text((72, 120), "INTERNATIONAL STANDARD", fontsize=14)
    cover.insert_text((72, 150), "ISO 99999-1:2026", fontsize=14)

    second = document.new_page(width=595, height=842)
    if contents_page:
        second.insert_text((72, 100), "Contents", fontsize=13)
        y = 140
        for line in (
            "1 Scope ................................................ 3",
            "2 Normative references ................................ 3",
            "3 Terms and definitions ............................... 4",
            "4 General requirements ............................... 5",
            "4.1 Materials ......................................... 5",
        ):
            second.insert_text((72, y), line, fontsize=11)
            y += 20
    else:
        second.insert_text((72, 100), "Foreword", fontsize=13)

    body = [
        ["1 Scope", "2 Normative references"],
        ["3 Terms and definitions"],
        ["4 General requirements", "4.1 Materials"],
        ["Annex A (informative) Test methods"],
    ]
    for index in range(pages - 2):
        page = document.new_page(width=595, height=842)
        y = 100
        for heading in body[index % len(body)]:
            page.insert_text((72, y), heading, fontsize=heading_size)
            y += 26
            page.insert_text(
                (72, y),
                "This International Standard specifies requirements for the device "
                "and the test shall be performed as given in Table 3.",
                fontsize=10,
            )
            y += 46

    if bookmarks:
        document.set_toc(bookmarks)
    document.save(str(path))
    document.close()
    return str(path)


# ── the entry contract ────────────────────────────────────────────────────────


def test_an_entry_has_level_title_and_a_one_indexed_page(toc, tmp_path):
    """The range picker and the emitter both depend on this shape; the emitter parses
    the section number back out of `title`."""
    path = build_pdf(
        tmp_path / "bm.pdf",
        bookmarks=[[1, "1 Scope", 3], [1, "2 Normative references", 3], [2, "4.1 Materials", 5]],
    )

    entries = toc.extract_toc(path)

    assert entries
    for entry in entries:
        assert set(entry) == {"level", "title", "page"}
        assert isinstance(entry["level"], int)
        assert isinstance(entry["title"], str)
        assert entry["page"] >= 1, "pages are 1-indexed"


# ── the fallback chain ────────────────────────────────────────────────────────


def test_bookmarks_win_when_there_are_at_least_three(toc, tmp_path):
    path = build_pdf(
        tmp_path / "bm.pdf",
        bookmarks=[
            [1, "1 Scope", 3],
            [1, "2 Normative references", 3],
            [1, "4 General requirements", 5],
            [2, "4.1 Materials", 5],
        ],
    )

    entries = toc.extract_toc(path)

    assert [entry["title"] for entry in entries] == [
        "1 Scope",
        "2 Normative references",
        "4 General requirements",
        "4.1 Materials",
    ]
    assert [entry["level"] for entry in entries] == [1, 1, 1, 2]
    assert entries[0]["page"] == 3


def test_two_bookmarks_are_too_few_and_fall_through(toc, tmp_path):
    """Fewer than three is usually a cover bookmark and a stray, not a contents tree."""
    path = build_pdf(
        tmp_path / "few.pdf",
        bookmarks=[[1, "1 Scope", 3], [1, "2 Normative references", 3]],
    )

    entries = toc.extract_toc(path)

    assert len(entries) > 2
    assert any("Materials" in entry["title"] for entry in entries)


def test_the_dot_leader_pipeline_reads_the_contents_page(toc, tmp_path):
    path = build_pdf(tmp_path / "dots.pdf")

    entries = toc._build_toc_from_pypdf(path)

    assert entries, "the dot-leader contents page should have been detected"
    assert all(set(entry) == {"level", "title", "page"} for entry in entries)
    # The monotonicity pass guarantees non-decreasing pages.
    assert [entry["page"] for entry in entries] == sorted(
        entry["page"] for entry in entries
    )


def test_the_dot_leader_pipeline_returns_empty_on_an_unreadable_file(toc, tmp_path):
    assert toc._build_toc_from_pypdf(str(tmp_path / "missing.pdf")) == []


def test_the_pattern_scan_finds_numbered_sections_and_skips_dot_leaders(toc, tmp_path):
    path = build_pdf(tmp_path / "pat.pdf", pages=8)

    entries = toc._toc_from_section_patterns(path)

    assert any(entry["title"].startswith("4.1 Materials") for entry in entries)
    assert not any("....." in entry["title"] for entry in entries)
    # Deduplicated by section number, so a heading repeated on later pages appears once.
    numbers = [entry["title"].split()[0] for entry in entries]
    assert len(numbers) == len(set(numbers))


def test_extract_toc_without_a_model_degrades_to_empty(toc, tmp_path):
    """`llm=None` must skip level 5 rather than raise, so an unreadable document yields
    an empty outline the picker can report."""
    import fitz

    document = fitz.open()
    for _ in range(4):
        document.new_page(width=595, height=842)
    path = tmp_path / "blank.pdf"
    document.save(str(path))
    document.close()

    assert toc.extract_toc(str(path)) == []


# ── hierarchy validation, which gates the pattern scan ────────────────────────


@pytest.mark.parametrize(
    "titles,valid",
    [
        ([], False),
        (["1 Scope", "2 Refs"], False),
        (["1 Scope", "2 Refs", "3 Terms"], True),
        (["Annex A", "Annex B", "Annex C"], True),
        (["1 Scope", "2 Refs", "40 Nope"], False),
    ],
    ids=["empty", "two-entries", "three-sections", "all-annexes", "huge-gap"],
)
def test_hierarchy_validation(toc, titles, valid):
    entries = [{"level": 1, "title": title, "page": 1} for title in titles]
    assert toc._validate_section_hierarchy(entries) is valid


def test_hierarchy_validation_rejects_mostly_orphaned_subsections(toc):
    """Reference numbers scraped out of body text look like this."""
    entries = [
        {"level": 3, "title": f"{n}.1.1 Orphan", "page": n} for n in range(1, 20)
    ]
    assert toc._validate_section_hierarchy(entries) is False


# ── the model fallback ────────────────────────────────────────────────────────


def test_the_model_fallback_sends_every_page_in_one_request(toc, tmp_path):
    """The prompt asks the model to find the contents page among the images, so they
    must arrive together — one call per page would be a different question."""
    reply = json.dumps(
        [
            {"section_number": "1", "title": "Scope", "page_number": 3},
            {"section_number": "4.1.2", "title": "Materials", "page_number": 5},
            {"section_number": "Annex A", "title": "Test methods", "page_number": 12},
        ]
    )
    llm = FakeLlm(chat_vision_multi=[f"```json\n{reply}\n```"])

    entries = toc._toc_from_llm(build_pdf(tmp_path / "llm.pdf", pages=4), llm=llm)

    assert [(e["level"], e["title"], e["page"]) for e in entries] == [
        (1, "1 Scope", 3),
        (3, "4.1.2 Materials", 5),
        (1, "Annex A Test methods", 12),
    ]
    call = llm.calls_of("chat_vision_multi")[0]
    assert len(llm.calls_of("chat_vision_multi")) == 1
    assert call["images"] == 4
    assert call["model"] == "gpt-5.2"
    assert call["max_tokens"] == 4096
    assert call["timeouts"] == (180,)


def test_the_model_fallback_caps_the_render_at_ten_pages(toc, tmp_path):
    llm = FakeLlm(chat_vision_multi=["[]"])
    toc._toc_from_llm(build_pdf(tmp_path / "big.pdf", pages=14), llm=llm)
    assert llm.calls_of("chat_vision_multi")[0]["images"] == 10


def test_the_model_fallback_drops_unknown_pages_when_enough_are_known(toc, tmp_path):
    reply = json.dumps(
        [
            {"section_number": "", "title": "Foreword", "page_number": 0},
            {"section_number": "1", "title": "Scope", "page_number": 3},
            {"section_number": "2", "title": "Refs", "page_number": 3},
            {"section_number": "3", "title": "Terms", "page_number": 4},
        ]
    )
    entries = toc._toc_from_llm(
        build_pdf(tmp_path / "llm2.pdf", pages=4), llm=FakeLlm(chat_vision_multi=[reply])
    )
    assert [entry["title"] for entry in entries] == ["1 Scope", "2 Refs", "3 Terms"]


@pytest.mark.parametrize(
    "reply",
    [StubLlmError("504"), "not json", '{"a": 1}'],
    ids=["transport-failure", "unparseable", "not-a-list"],
)
def test_the_model_fallback_never_raises(toc, tmp_path, reply):
    path = build_pdf(tmp_path / "llm3.pdf", pages=4)
    assert toc._toc_from_llm(path, llm=FakeLlm(chat_vision_multi=[reply])) == []


# ── the vision path ───────────────────────────────────────────────────────────


def llm_page(page_idx, text):
    return {"page_idx": page_idx, "text": text, "images": [], "tables": []}


def test_the_vision_path_reads_only_the_contents_page(toc):
    """The reason this pipeline exists: body pages are never scanned for headings, so a
    heading repeated in the body cannot produce a duplicate entry."""
    contents = (
        "Contents\n"
        "1 Scope ................................................ 3\n"
        "2 Normative references ................................ 3\n"
        "4.1 Materials ......................................... 5\n"
    )
    pages = [
        llm_page(0, "INTERNATIONAL STANDARD\nISO 99999-1:2026"),
        llm_page(1, contents),
        llm_page(2, "1 Scope\nThis document specifies requirements."),
        llm_page(3, "4.1 Materials\nThe materials shall be inert."),
    ]

    entries = toc.build_toc_from_llm_pages(pages)

    assert entries
    titles = [entry["title"] for entry in entries]
    assert len(titles) == len(set(titles)), f"duplicates leaked in: {titles}"
    assert all(entry["page"] >= 1 for entry in entries)


def test_the_vision_path_returns_empty_without_a_contents_page(toc):
    pages = [llm_page(0, "Just some body prose with no contents page at all.")]
    assert toc.build_toc_from_llm_pages(pages) == []


def test_the_vision_path_tolerates_no_pages(toc):
    assert toc.build_toc_from_llm_pages([]) == []
    assert toc.build_toc_from_llm_pages(None) == []


def test_a_running_header_prefix_is_stripped_from_a_title(toc):
    """The vision model sometimes glues the page's running header onto the entry."""
    contents = (
        "Contents\n"
        "ISO 11608-4:2022(E) 8.10.6 NIS-E requirements ................ 12\n"
        "9 Marking ..................................................... 15\n"
    )
    entries = toc.build_toc_from_llm_pages([llm_page(0, contents), llm_page(1, "body")])

    assert entries
    assert not any(entry["title"].startswith("ISO 11608") for entry in entries)


# ── deduplication ─────────────────────────────────────────────────────────────


def test_a_doubled_document_collapses_to_one_copy(toc):
    """Some standards ship a redline copy followed by the clean one, so every section is
    bookmarked twice. The later (clean) page wins."""
    entries = [
        {"level": 1, "title": "1 Scope", "page": 3},
        {"level": 1, "title": "2 Normative references", "page": 4},
        {"level": 1, "title": "1 Scope", "page": 40},
        {"level": 1, "title": "2 Normative references", "page": 41},
    ]

    kept = toc.deduplicate_toc(entries)

    assert [entry["title"] for entry in kept] == ["1 Scope", "2 Normative references"]
    assert [entry["page"] for entry in kept] == [40, 41]


def test_titles_differing_only_in_noise_are_one_entry(toc):
    entries = [
        {"level": 1, "title": "Annex" + NBSP + "A (informative)", "page": 30},
        {"level": 1, "title": "Annex A  (informative)", "page": 60},
        {"level": 1, "title": "1 Scope", "page": 3},
    ]

    kept = toc.deduplicate_toc(entries)

    assert len(kept) == 2


def test_a_renamed_section_is_still_a_duplicate_by_its_number(toc):
    """Pass 2: the descriptive text changed between editions but "4.3" did not.

    Pass 2 also builds `table:` and `figure:` identifiers, but those turn out to be
    unreachable in the final output: pass 4 deletes every entry whose title *starts*
    with "Table" or "Figure" outright, and `_extract_toc_id` only matches at the start
    of a title. So in practice pass 2 dedupes sections and annexes only. That is current
    behaviour and is ported as-is — see the companion test below, which pins the pass-4
    removal that makes it so.
    """
    entries = [
        {"level": 2, "title": "4.3 Minimum creepage distances", "page": 20},
        {"level": 2, "title": "4.3 Not used", "page": 55},
        {"level": 1, "title": "1 Scope", "page": 3},
    ]

    kept = toc.deduplicate_toc(entries)

    matching = [entry for entry in kept if entry["title"].startswith("4.3")]
    assert len(matching) == 1
    assert matching[0]["page"] == 55


def test_a_table_bookmark_is_removed_outright_rather_than_deduplicated(toc):
    """Pass 4 runs after pass 2 and drops the whole entry, which is why the `table:`
    branch of `_extract_toc_id` never reaches the output."""
    entries = [
        {"level": 1, "title": "1 Scope", "page": 3},
        {"level": 2, "title": "Table 11 - Minimum creepage distances", "page": 20},
        {"level": 2, "title": "Table 11 - Not used", "page": 55},
        {"level": 1, "title": "2 Normative references", "page": 60},
    ]

    kept = toc.deduplicate_toc(entries)

    assert not any("Table 11" in entry["title"] for entry in kept)
    assert [entry["title"] for entry in kept] == ["1 Scope", "2 Normative references"]


def test_an_orphan_left_far_behind_is_dropped(toc):
    """Pass 3: an entry that existed only in the earlier copy stays at its low page
    while everything else moved forward, leaving an implausible gap."""
    entries = [
        {"level": 1, "title": "Redline version marker", "page": 5},
        {"level": 1, "title": "1 Scope", "page": 100},
        {"level": 1, "title": "2 Normative references", "page": 101},
    ]

    kept = toc.deduplicate_toc(entries)

    assert [entry["title"] for entry in kept] == ["1 Scope", "2 Normative references"]


@pytest.mark.parametrize(
    "title", ["Figures", "Tables", "Bibliography", "INDEX", "Contents"]
)
def test_index_style_bookmarks_are_removed(toc, title):
    """Pass 4: these point at pages the numbered sections already cover, so keeping them
    would extract the same content twice."""
    entries = [
        {"level": 1, "title": "1 Scope", "page": 3},
        {"level": 1, "title": title, "page": 4},
        {"level": 1, "title": "2 Normative references", "page": 5},
    ]

    kept = toc.deduplicate_toc(entries)

    assert [entry["title"] for entry in kept] == ["1 Scope", "2 Normative references"]


def test_deduplication_leaves_a_clean_outline_alone(toc):
    entries = [
        {"level": 1, "title": "1 Scope", "page": 3},
        {"level": 1, "title": "2 Normative references", "page": 4},
        {"level": 2, "title": "4.1 Materials", "page": 5},
    ]
    assert toc.deduplicate_toc(entries) == entries


def test_deduplication_of_an_empty_outline(toc):
    assert toc.deduplicate_toc([]) == []
