"""The media pre-scan (`silos/iso/prescan.py`) and its supporting geometry.

Everything here is offline. Textract is a stub, so no page is ever sent to AWS — which
matters because it is charged per page and a real run over a 200-page document is 200
analyses.

The `toc=[]` trick is what makes the full 8-tuple testable without a model: the arity is
gated on `toc is not None`, while the unnumbered-formula detector is gated on
`toc and not llm_pages`, so an empty list satisfies the first and fails the second.
"""

from __future__ import annotations

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeLlm, FakeTextract

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "prescan.py").is_file(), reason="the ISO silo is not present"
)

FIGURE_CAPTION = "Figure 1 - Relationship of terms"
TABLE_CAPTION = "Table E.1 - Correspondence between documents"


@pytest.fixture(scope="module")
def iso():
    _load_module("iso", ISO_DIR / "silo.py")
    modules = {}
    for name in (
        "patterns",
        "sections",
        "mediastore",
        "geometry",
        "vision",
        "textract",
        "equations",
        "prescan",
    ):
        modules[name] = _load_module("iso", ISO_DIR / f"{name}.py", name=name)
    return modules


class FakeWorkspace:
    """Stands in for the run workspace: a local media dir plus a durable sink."""

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self._media = root / "memry"
        self._media.mkdir(exist_ok=True)
        self.put: dict[str, bytes] = {}

    @property
    def media_dir(self) -> str:
        return str(self._media)

    def put_media(self, filename: str, data: bytes) -> str:
        self.put[filename] = data
        return f"key/{filename}"


def write_pdf(tmp_path, pages=1, name="doc.pdf"):
    """A synthetic page carrying a figure, a ruled table and an equation.

    Three properties are designed in rather than hoped for:
      * the figure is a filled rect plus eight strokes and a circle, which clears
        `_is_real_figure`'s "six or more substantial paths" gate regardless of how much
        text sits near it;
      * the table's rules are axis-aligned, so each has zero width or zero height and
        `_cluster_drawings` discards them — the table can never masquerade as a figure;
      * captions are inserted at size 8 against body text at size 10, because
        `_extract_page_regions` decides caption style by comparing against the most
        common span size on the page.
    """
    import fitz

    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)

        page.insert_textbox(
            fitz.Rect(72, 90, 523, 150),
            "This International Standard specifies requirements for information "
            "supplied by the manufacturer of a medical device.",
            fontsize=10,
        )

        # A figure: filled rectangle, diagonals, and a circle.
        page.draw_rect(fitz.Rect(120, 300, 400, 430), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        for offset in range(8):
            page.draw_line(
                fitz.Point(130 + offset * 30, 310), fitz.Point(150 + offset * 30, 420)
            )
        page.draw_circle(fitz.Point(260, 365), 40)
        page.insert_text((120, 450), FIGURE_CAPTION, fontsize=8)

        # A table: caption above, then axis-aligned rules.
        page.insert_text((72, 520), TABLE_CAPTION, fontsize=8)
        for row in range(4):
            y = 540 + row * 20
            page.draw_line(fitz.Point(72, y), fitz.Point(402, y))
        for column in range(3):
            x = 72 + column * 165
            page.draw_line(fitz.Point(x, 540), fitz.Point(x, 600))
        page.insert_text((80, 555), "Clause", fontsize=9)
        page.insert_text((250, 555), "Requirement", fontsize=9)

        # An equation with a trailing number, so it also lands in `formulas`.
        page.insert_text((150, 680), "P = F / A  where  a <= b  (1)", fontsize=10)

    path = tmp_path / name
    document.save(str(path))
    document.close()
    return str(path)


def llm_page(page_idx, text="", images=(), tables=()):
    return {
        "page_idx": page_idx,
        "text": text,
        "images": list(images),
        "tables": list(tables),
    }


def as_bytes(iso, value):
    return iso["mediastore"]._media_as_bytes(value)


# ── geometry ──────────────────────────────────────────────────────────────────


def test_page_regions_finds_a_figure_and_a_table(iso, tmp_path):
    import fitz

    document = fitz.open(write_pdf(tmp_path))
    try:
        regions = iso["geometry"]._extract_page_regions(document[0])
    finally:
        document.close()

    kinds = [region["type"] for region in regions]
    assert "image" in kinds, f"no figure detected, got {kinds}"
    assert "table" in kinds, f"no table detected, got {kinds}"
    # caption_y0 exists only on merged figure entries, so it must be read with .get()
    assert all(set(region) >= {"type", "bbox", "caption"} for region in regions)


def test_axis_aligned_rules_are_not_mistaken_for_a_figure(iso):
    """Zero-height and zero-width rects are discarded, which is what stops a ruled table
    clustering into a vector figure."""
    flat = [{"rect": (72, 540, 402, 540)}, {"rect": (72, 540, 72, 600)}]
    assert iso["geometry"]._cluster_drawings(flat) == []


def test_many_paths_alone_make_a_figure(iso):
    """The `many_paths >= 6` shortcut exists because circuit diagrams sit alongside body
    text, which would otherwise fail the text-coverage check."""
    import fitz

    drawings = [{"rect": (10, 10 + n * 20, 100, 100 + n * 20), "items": []} for n in range(6)]
    bbox = fitz.Rect(0, 0, 200, 200)
    dense_text = [{"bbox": (0, 0, 200, 200)}]
    assert iso["geometry"]._is_real_figure(drawings, bbox, dense_text, 595) is True


# ── return arity ──────────────────────────────────────────────────────────────


def test_no_toc_and_no_workspace_gives_three_tuple_of_bytes(iso, tmp_path):
    result = iso["prescan"].prescan_media(write_pdf(tmp_path))

    assert len(result) == 3
    figures, tables, formulas = result
    assert all(isinstance(value, bytes) for value in figures.values())
    assert all(isinstance(item, bytes) for pages in tables.values() for item in pages)


def test_an_empty_toc_still_gives_the_eight_tuple(iso, tmp_path):
    """`toc=[]` is not None, so the full contract is reachable with no model at all."""
    result = iso["prescan"].prescan_media(write_pdf(tmp_path), toc=[])

    assert len(result) == 8
    unnumbered = result[3]
    assert unnumbered == [], "no toc entries means the detector is skipped"


def test_a_workspace_switches_values_from_bytes_to_paths(iso, tmp_path):
    workspace = FakeWorkspace(tmp_path / "ws")
    figures, tables, formulas = iso["prescan"].prescan_media(
        write_pdf(tmp_path), ws=workspace
    )

    assert figures, "expected at least one figure"
    assert all(isinstance(value, str) for value in figures.values())
    assert as_bytes(iso, next(iter(figures.values())))[:8] == b"\x89PNG\r\n\x1a\n"
    # The durable copies reached the store as well as local scratch.
    assert workspace.put


# ── the structure contract ────────────────────────────────────────────────────


def test_a_captioned_figure_is_keyed_by_its_number(iso, tmp_path):
    figures, _tables, _formulas = iso["prescan"].prescan_media(write_pdf(tmp_path))
    assert 1 in figures, f"expected figure 1, got keys {sorted(figures)}"


def test_table_values_are_one_entry_per_captured_page(iso, tmp_path):
    """Fix B: a list per page, never concatenated — vstacking then slicing cut across
    table rows."""
    _figures, tables, _formulas = iso["prescan"].prescan_media(write_pdf(tmp_path, pages=3))

    assert "E.1" in tables, f"expected table E.1, got {sorted(tables)}"
    assert isinstance(tables["E.1"], list)
    assert len(tables["E.1"]) == 3, "the same caption on three pages means three images"


def test_a_multi_page_table_gets_page_suffixed_filenames(iso, tmp_path):
    workspace = FakeWorkspace(tmp_path / "ws")
    iso["prescan"].prescan_media(write_pdf(tmp_path, pages=2), ws=workspace)

    names = sorted(workspace.put)
    assert any(name.endswith("_p1.png") for name in names), names
    assert any(name.endswith("_p2.png") for name in names), names
    # Filenames are caption-derived, which is why _sanitize_filename exists.
    assert any(name.startswith("Table E.1") for name in names), names
    assert any(name.startswith("Figure 1") for name in names), names


def test_a_numbered_formula_is_keyed_by_its_number(iso, tmp_path):
    _figures, _tables, formulas = iso["prescan"].prescan_media(write_pdf(tmp_path))
    assert 1 in formulas, f"expected formula 1, got {sorted(formulas)}"


def test_table_suppression_rects_are_recorded_for_every_page(iso, tmp_path):
    """`_extract_body` uses these to avoid emitting table cell text twice.

    Tables record their rect before any deduplication, so a table repeated on a second
    page is still suppressed there.
    """
    result = iso["prescan"].prescan_media(write_pdf(tmp_path, pages=2), toc=[])
    table_rects = result[4]

    assert set(table_rects) == {0, 1}
    for rects in table_rects.values():
        for rect in rects:
            assert len(rect) == 4
            assert all(isinstance(value, float) for value in rect), "plain floats, not Rect"


def test_a_repeated_figure_records_no_suppression_rect_on_the_later_page(iso, tmp_path):
    """A quirk of the source, pinned so it is documented rather than rediscovered.

    The normal path skips a figure number it has already captured (`if fig_num in
    figures: continue`) *before* it records the suppression rect. So when the same figure
    number appears again on a later page, that page gets no `fig_rects` entry and the
    text inside the repeated figure is NOT suppressed — unlike tables, which record their
    rect first. Current behaviour, ported unchanged.
    """
    result = iso["prescan"].prescan_media(write_pdf(tmp_path, pages=2), toc=[])
    fig_rects = result[5]

    assert set(fig_rects) == {0}, "only the first capture records a figure rect"
    for rect in fig_rects[0]:
        assert len(rect) == 4
        assert all(isinstance(value, float) for value in rect)


def test_media_first_page_uses_prefixed_keys(iso, tmp_path):
    first_page = iso["prescan"].prescan_media(write_pdf(tmp_path, pages=2), toc=[])[7]

    assert first_page["figure:1"] == 0
    assert first_page["table:E.1"] == 0
    assert all(key.startswith(("figure:", "table:")) for key in first_page)


def test_a_captioned_table_is_not_listed_as_unlabeled(iso, tmp_path):
    assert iso["prescan"].prescan_media(write_pdf(tmp_path), toc=[])[6] == {}


# ── the vision path ───────────────────────────────────────────────────────────


def test_textract_is_called_exactly_once_per_page(iso, tmp_path):
    """This count is the bill."""
    textract = FakeTextract()
    pages = [llm_page(index) for index in range(3)]

    iso["prescan"].prescan_media(
        write_pdf(tmp_path, pages=3), llm_pages=pages, textract=textract
    )

    assert textract.calls == 3


def test_a_repeated_page_is_not_charged_twice(iso, tmp_path):
    textract = FakeTextract()
    pages = [llm_page(0), llm_page(1), llm_page(0), llm_page(1)]

    iso["prescan"].prescan_media(
        write_pdf(tmp_path, pages=2), llm_pages=pages, textract=textract
    )

    assert textract.calls == 2


def found_media() -> FakeTextract:
    """Textract having located the synthetic page's table and figure."""
    return FakeTextract(
        tables=[[0.10, 0.60, 0.70, 0.72]], figures=[[0.15, 0.33, 0.70, 0.52]]
    )


def test_the_vision_path_does_not_depend_on_page_order(iso, tmp_path):
    """Keys are derived from how many items have been seen, so the pages are walked in
    sorted order rather than whatever order they arrived in."""
    path = write_pdf(tmp_path, pages=3)
    text = f"{TABLE_CAPTION}\n\nsome rows\n\n{FIGURE_CAPTION}\n\nmore prose"

    forwards = iso["prescan"].prescan_media(
        path,
        llm_pages=[llm_page(index, text=text) for index in (0, 1, 2)],
        textract=found_media(),
        toc=[],
    )
    backwards = iso["prescan"].prescan_media(
        path,
        llm_pages=[llm_page(index, text=text) for index in (2, 1, 0)],
        textract=found_media(),
        toc=[],
    )

    assert forwards[0], "the fixture should have produced at least one figure"
    assert sorted(forwards[0]) == sorted(backwards[0])
    assert sorted(forwards[1]) == sorted(backwards[1])
    assert forwards[7] == backwards[7]


def test_the_vision_path_takes_captions_from_the_model_text(iso, tmp_path):
    text = f"{TABLE_CAPTION}\n\nsome rows\n\n{FIGURE_CAPTION}\n\nmore prose"

    result = iso["prescan"].prescan_media(
        write_pdf(tmp_path),
        llm_pages=[llm_page(0, text=text)],
        textract=found_media(),
        toc=[],
    )

    assert "E.1" in result[1], sorted(result[1])
    assert 1 in result[0], sorted(result[0])


def test_an_uncaptioned_table_with_no_continuation_is_skipped(iso, tmp_path):
    """Otherwise a contents page would be captured as a table."""
    result = iso["prescan"].prescan_media(
        write_pdf(tmp_path),
        llm_pages=[llm_page(0, text="no captions at all here")],
        textract=FakeTextract(tables=[[0.12, 0.60, 0.68, 0.70]]),
    )

    assert result[1] == {}


def test_the_vision_path_leaves_numbered_formulas_empty(iso, tmp_path):
    """The numbered-formula scan lives in the normal-path branch only."""
    result = iso["prescan"].prescan_media(
        write_pdf(tmp_path),
        llm_pages=[llm_page(0)],
        textract=FakeTextract(),
        toc=[{"level": 1, "title": "1 Scope", "page": 1}],
        toc_end_idx=0,
        llm=FakeLlm(),
    )

    assert result[2] == {}
    assert result[3] == [], "the detector requires the normal path"


def test_a_model_bbox_recovers_a_table_textract_missed(iso, tmp_path):
    pages = [
        llm_page(
            0,
            tables=[{"caption": "Table 9 - missed", "bbox_norm": [0.10, 0.55, 0.70, 0.72]}],
            images=[{"caption": "Figure 5 - missed", "bbox_norm": [0.10, 0.30, 0.60, 0.50]}],
        )
    ]

    figures, tables = iso["prescan"].prescan_media(
        write_pdf(tmp_path), llm_pages=pages, textract=FakeTextract()
    )[:2]

    assert "9" in tables
    assert 5 in figures


def test_the_vision_path_survives_a_textract_that_finds_nothing(iso, tmp_path):
    empty = FakeTextract()

    result = iso["prescan"].prescan_media(
        write_pdf(tmp_path), llm_pages=[llm_page(0)], textract=empty, toc=[]
    )

    assert result[0] == {} and result[1] == {}
    assert empty.calls == 1


# ── determinism, which the golden-file comparison depends on ──────────────────


def fingerprint(iso, result):
    """Keys and PNG payloads, ignoring paths — those vary per workspace."""
    figures, tables, formulas = result[0], result[1], result[2]
    return {
        "figures": {key: as_bytes(iso, value) for key, value in figures.items()},
        "tables": {
            key: [as_bytes(iso, item) for item in iso["mediastore"]._table_img_list(value)]
            for key, value in tables.items()
        },
        "formulas": {key: as_bytes(iso, value) for key, value in formulas.items()},
        "table_rects": result[4],
        "fig_rects": result[5],
        "unlabeled": result[6],
        "first_page": result[7],
    }


def test_the_normal_path_is_deterministic(iso, tmp_path):
    path = write_pdf(tmp_path, pages=3)
    first = iso["prescan"].prescan_media(path, toc=[])
    second = iso["prescan"].prescan_media(path, toc=[])
    assert fingerprint(iso, first) == fingerprint(iso, second)


def test_the_result_does_not_depend_on_the_workspace(iso, tmp_path):
    path = write_pdf(tmp_path, pages=3)
    one = iso["prescan"].prescan_media(path, ws=FakeWorkspace(tmp_path / "a"), toc=[])
    two = iso["prescan"].prescan_media(path, ws=FakeWorkspace(tmp_path / "b"), toc=[])
    assert fingerprint(iso, one) == fingerprint(iso, two)


def test_the_vision_path_is_deterministic(iso, tmp_path):
    path = write_pdf(tmp_path, pages=3)
    text = f"{TABLE_CAPTION}\n\nrows\n\n{FIGURE_CAPTION}\n\nprose"
    pages = [llm_page(index, text=text) for index in range(3)]

    first = iso["prescan"].prescan_media(
        path, llm_pages=pages, textract=found_media(), toc=[]
    )
    second = iso["prescan"].prescan_media(
        path, llm_pages=pages, textract=found_media(), toc=[]
    )

    assert first[0], "the fixture should have produced media to compare"
    assert fingerprint(iso, first) == fingerprint(iso, second)


def test_the_module_docstring_records_the_contract(iso):
    """The emitter is written against this; an undocumented contract is the failure this
    whole task exists to prevent."""
    text = iso["prescan"].__doc__ or ""
    for token in (
        "figures",
        "tables",
        "formulas",
        "unnumbered_formulas",
        "table_rects_by_page",
        "fig_rects_by_page",
        "unlabeled_tables_by_page",
        "media_first_page",
        "ONE ENTRY PER CAPTURED PDF PAGE",
        "negative",
    ):
        assert token in text, token
