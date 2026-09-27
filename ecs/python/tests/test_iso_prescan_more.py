"""The media pre-scan's error, fallback and stitching paths.

`tests/test_iso_prescan.py` drives `prescan_media` through a real synthetic PDF and
covers the happy paths of both branches: arity, the structure contract, the per-page
table list, Textract call counting and determinism. It deliberately stops there, because
a real PDF can only express the geometry that PyMuPDF is willing to lay out. This file
adds ONLY what that leaves untested — and it is the majority of the module:

  * the recovery paths, where `get_pixmap` raises on a degenerate clip. The source
    swallows those and carries on (the rect is still recorded for text suppression), and
    a real PDF cannot be persuaded to fail on demand.
  * the cross-page figure machinery — fragment capture, caption-only trim, PIL stitch and
    all three of its exception handlers — which needs a drawing cluster, a *later*
    figure caption on the same page, and a caption line 100+ points above the region
    bottom, all at once.
  * the vision path's caption arbitration: continuation stitching by x-overlap, the
    "figure caption nearby → don't stitch a table" guard, the LLM-bbox union, the
    highest-overlap caption pick and the below-the-figure caption search.
  * the LLM-bbox fallback's four skip conditions and its y1n clipping against the next
    figure on the page.

TWO SEAMS, both test-only, both because the alternative is untestable:

  1. `_extract_page_regions` is replaced with a function returning hand-built region
     dicts. It is imported into `prescan`'s namespace at import time, so patching the
     name on the module is enough. This is how bbox / caption / caption_y0 combinations
     that a synthetic PDF cannot produce become reachable.
  2. `prescan.fitz` is replaced with a shim whose `Rect` is the real one and whose
     `open` returns a `FakeDoc`. Only then can `get_pixmap` be made to raise for one
     specific clip, and only then can a "PNG" be returned that PIL refuses to open —
     which is exactly what the stitch/trim handlers are written for.

Every fake pixmap's dimensions are the clip's own dimensions, so an assertion about a
stitched image's height is an assertion about which clips the source chose.

Coverage is always measured with both files together; nothing here duplicates a case
already covered there.
"""

from __future__ import annotations

import io
import types

import fitz
import pytest
from PIL import Image as PILImage

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module
from tests.iso_fakes import FakeTextract

ISO_DIR = BACKEND_ROOT / "silos" / "iso"

pytestmark = pytest.mark.skipif(
    not (ISO_DIR / "prescan.py").is_file(), reason="the ISO silo is not present"
)

PAGE_W = 595.0
PAGE_H = 842.0


@pytest.fixture(scope="module")
def prescan():
    _load_module("iso", ISO_DIR / "silo.py")
    return _load_module("iso", ISO_DIR / "prescan.py", name="prescan")


# ── fakes ─────────────────────────────────────────────────────────────────────


def png_bytes(width, height, color=(255, 255, 255)):
    """A real PNG, because the stitch path opens these with PIL."""
    buf = io.BytesIO()
    PILImage.new("RGB", (max(1, int(width)), max(1, int(height))), color).save(buf, "PNG")
    return buf.getvalue()


class FakePixmap:
    def __init__(self, data):
        self._data = data

    def tobytes(self, _fmt="png"):
        return self._data


class FakePage:
    """A page that answers only what `prescan_media` asks of it.

    `fail` receives the clip and decides whether `get_pixmap` raises, which is how a
    single page can succeed for one crop and fail for another — the shape the trim and
    fragment handlers need. `png` forces a fixed payload, including a payload PIL cannot
    decode.
    """

    def __init__(self, *, blocks=(), drawings=(), fail=None, png=None,
                 width=PAGE_W, height=PAGE_H):
        self.rect = fitz.Rect(0, 0, width, height)
        self._blocks = list(blocks)
        self._drawings = list(drawings)
        self._fail = fail or (lambda clip: False)
        self._png = png
        self.clips = []

    def get_pixmap(self, clip=None, dpi=None):
        # clip=None is the vision path rendering the whole page for Textract; that
        # render is not what any failure case here is about, so it never raises.
        if clip is None:
            return FakePixmap(png_bytes(self.rect.width, self.rect.height))
        self.clips.append(clip)
        if self._fail(clip):
            raise RuntimeError("Invalid bandwriter header dimensions")
        if self._png is not None:
            return FakePixmap(self._png)
        return FakePixmap(png_bytes(round(clip.width), round(clip.height)))

    def get_text(self, kind="text", sort=False):
        # The vision path's landscape-table detection asks for the "dict" shape
        # ({"blocks": [{"lines": [{"dir": (dx, dy), ...}]}]}) to count rotated text
        # lines. Real PyMuPDF returns that mapping; these synthetic pages are all
        # portrait, so an empty block list is faithful (no rotated lines → not
        # landscape) and keeps the tuple form for the "blocks"/"text" callers.
        if kind == "dict":
            return {"blocks": []}
        return list(self._blocks)

    def get_drawings(self):
        return list(self._drawings)


class FakeDoc:
    def __init__(self, pages):
        self._pages = list(pages)
        self.closed = False

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, index):
        return self._pages[index]

    def close(self):
        self.closed = True


class FakeWorkspace:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self._media = root / "memry"
        self._media.mkdir(exist_ok=True)
        self.put: dict[str, bytes] = {}

    @property
    def media_dir(self) -> str:
        return str(self._media)

    def put_media(self, filename, data):
        self.put[filename] = data
        return f"key/{filename}"


def block(text, y0=100.0, y1=120.0, btype=0):
    """A `get_text("blocks")` tuple: (x0, y0, x1, y1, text, block_no, type)."""
    return (72.0, y0, 523.0, y1, text, 0, btype)


def drawing(y0, y1, x0=100.0, x1=400.0):
    return {"rect": (x0, y0, x1, y1)}


def region(kind, bbox, caption="", caption_y0=None):
    reg = {"type": kind, "bbox": bbox, "caption": caption}
    if caption_y0 is not None:
        reg["caption_y0"] = caption_y0
    return reg


def run(prescan, monkeypatch, pages, regions=None, **kwargs):
    """Call `prescan_media` against fake pages, with fake region detection.

    `regions` maps page index → region list; a missing page yields none.
    """
    doc = FakeDoc(pages)
    monkeypatch.setattr(
        prescan, "fitz", types.SimpleNamespace(Rect=fitz.Rect, open=lambda _p: doc)
    )
    by_page = dict(regions or {})
    monkeypatch.setattr(
        prescan,
        "_extract_page_regions",
        lambda page: by_page.get(pages.index(page), []),
    )
    result = prescan.prescan_media("ignored.pdf", **kwargs)
    assert doc.closed, "the document must be closed on every path"
    return result


def llm_page(page_idx, text="", images=(), tables=()):
    return {
        "page_idx": page_idx,
        "text": text,
        "images": list(images),
        "tables": list(tables),
    }


# ── the normal path: figure keys when the caption does not match ───────────────


def test_a_loose_figure_reference_in_the_caption_still_keys_the_figure(prescan, monkeypatch):
    """`_FIGURE_CAPTION_RE` demands a dash, so "see Figure 12 for details" fails it; the
    `\\bFigure\\s+(\\d+)\\b` fallback recovers the number rather than filing the image
    under a negative key."""
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        [FakePage()],
        {0: [region("image", (100, 100, 400, 300), caption="see figure 12 for details")]},
    )

    assert sorted(figures) == [12]


def test_captionless_figures_get_distinct_negative_keys(prescan, monkeypatch):
    """Negative keys are the module's promise that no image is silently dropped, and they
    must not collide with each other."""
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        [FakePage()],
        {
            0: [
                region("image", (100, 100, 400, 300)),
                region("image", (100, 400, 400, 600), caption="no reference here"),
            ]
        },
    )

    assert sorted(figures) == [-2, -1]


def test_an_unlabeled_figures_placeholder_caption_names_its_file(prescan, monkeypatch, tmp_path):
    """The caption is only used for the filename, so a one-based page number in it is the
    only externally visible evidence of the placeholder."""
    workspace = FakeWorkspace(tmp_path / "ws")
    run(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        {1: [region("image", (100, 100, 400, 300))]},
        ws=workspace,
    )

    assert sorted(workspace.put) == ["Figure (unlabeled, page 2).png"]


@pytest.mark.parametrize("bbox", [(100, 100, 104, 300), (100, 100, 400, 104)])
def test_a_region_thinner_than_five_points_is_never_cropped(prescan, monkeypatch, bbox):
    """Pinned from both sides: 5 is rejected, and the tests above prove 200+ is kept."""
    figures, tables, _formulas = run(
        prescan,
        monkeypatch,
        [FakePage()],
        {0: [region("image", bbox, caption="Figure 4 - thin"),
             region("table", bbox, caption="Table 4 - thin")]},
    )

    assert figures == {} and tables == {}


def test_a_caption_line_inside_the_bbox_is_cropped_out_of_the_image(prescan, monkeypatch):
    """The caption is already the row description, so baking it into the PNG would print
    it twice. The crop stops at caption_y0 — and the *suppression* rect still covers the
    whole region, which is a different rectangle."""
    page = FakePage()
    result = run(
        prescan,
        monkeypatch,
        [page],
        {0: [region("image", (100, 100, 400, 500), "Figure 3 - x", caption_y0=460.0)]},
        toc=[],
    )

    assert page.clips[0] == fitz.Rect(100, 100, 400, 460)
    assert result[5][0] == [(100.0, 100.0, 400.0, 500.0)]


def test_a_caption_within_ten_points_of_the_top_does_not_trim_the_crop(prescan, monkeypatch):
    """The `caption_y0 > bbox.y0 + 10` guard: a caption that high is above the artwork,
    not inside it, so trimming to it would crop the figure away entirely."""
    page = FakePage()
    run(
        prescan,
        monkeypatch,
        [page],
        {0: [region("image", (100, 100, 400, 500), "Figure 3 - x", caption_y0=109.0)]},
    )

    assert page.clips[0] == fitz.Rect(100, 100, 400, 500)


# ── the normal path: cross-page figures ───────────────────────────────────────


def cross_page_regions(caption_y0=200.0, bottom=700.0):
    """Page 0 holds Figure 6's caption high up with 500 points of artwork below it —
    which is how ISO 11608-4 lays out Figure 7, whose own caption is on the next page."""
    return {
        0: [region("image", (100, 100, 400, bottom), "Figure 6 - first", caption_y0=caption_y0)],
        1: [region("image", (100, 100, 400, 300), "Figure 7 - second", caption_y0=290.0)],
    }


def test_content_below_a_caption_is_stitched_onto_the_next_pages_figure(prescan, monkeypatch):
    """The fragment starts 30 points below the caption line — skipping the caption line
    itself — and is prepended, so the stitched height is fragment + figure."""
    pages = [FakePage(), FakePage()]
    figures, _tables, _formulas = run(prescan, monkeypatch, pages, cross_page_regions())

    stitched = PILImage.open(io.BytesIO(figures[7]))
    # fragment = y 230..700 (470), figure 7's crop = y 100..290 (190).
    assert stitched.height == 470 + 190
    assert stitched.width == 300


def test_a_bottom_less_than_a_hundred_points_below_the_caption_is_not_a_fragment(
    prescan, monkeypatch
):
    """Threshold pinned from both sides: 100 points below the caption is ordinary
    figure trailer, 101 is a cross-page fragment."""
    pages = [FakePage(), FakePage()]
    below = run(prescan, monkeypatch, pages, cross_page_regions(bottom=300.0))[0]
    over = run(prescan, monkeypatch, pages, cross_page_regions(bottom=301.0))[0]

    assert PILImage.open(io.BytesIO(below[7])).height == 190
    assert PILImage.open(io.BytesIO(over[7])).height == 71 + 190


def test_a_failed_fragment_capture_does_not_stop_the_figure_it_belongs_to(prescan, monkeypatch):
    """Both handlers on the same page: the fragment crop raises, then the figure crop
    raises too, and the run still finishes with the next page's figure intact."""
    pages = [FakePage(fail=lambda clip: True), FakePage()]
    figures, _tables, _formulas = run(prescan, monkeypatch, pages, cross_page_regions())

    assert sorted(figures) == [7], "page 0's figure is lost, page 1's is not"
    assert PILImage.open(io.BytesIO(figures[7])).height == 190, "nothing was stitched"


def test_an_undecodable_fragment_leaves_the_next_figure_untouched(prescan, monkeypatch):
    """The stitch is best-effort: a fragment PIL cannot open must not lose the figure."""
    pages = [FakePage(png=b"not a png at all"), FakePage()]
    figures, _tables, _formulas = run(prescan, monkeypatch, pages, cross_page_regions())

    assert PILImage.open(io.BytesIO(figures[7])).height == 190


def trimmable_page():
    """Page 1 of a stitch: Figure 7's caption at the top, then a drawing cluster that
    actually belongs to Figure 8, whose caption sits below the cluster."""
    return FakePage(
        drawings=[drawing(200.0, 400.0)],
        blocks=[block("Figure 8 - the cluster below", y0=430.0)],
    )


def test_a_cluster_claimed_by_a_later_caption_is_trimmed_off_the_stitch(prescan, monkeypatch):
    """Without the trim, Figure 8's circuit is baked into Figure 7 and then emitted again
    under Figure 8. The crop is cut 5 points above the first drawing."""
    pages = [FakePage(), trimmable_page()]
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        pages,
        {
            0: [region("image", (100, 100, 400, 700), "Figure 6 - first", caption_y0=200.0)],
            1: [region("image", (100, 100, 400, 500), "Figure 7 - second", caption_y0=480.0)],
        },
    )

    stitched = PILImage.open(io.BytesIO(figures[7]))
    # caption-only crop is y 100..195, not the full 100..480.
    assert stitched.height == 470 + 95


def test_the_trim_guard_on_a_fifteen_point_caption_strip_can_never_fire(prescan, monkeypatch):
    """Characterization of dead code, not of a behaviour.

    prescan.py:526 guards the trim with `_cap_only.height > 15`, but the cluster that
    triggers the trim was already filtered to `rect.y0 > crop_bbox.y0 + 20` at line 510,
    and the strip is `min(y0) - 5 - crop_bbox.y0`. So the strip is always over 15 and the
    guard's false branch is unreachable. Suspected defect: one of the two constants is
    wrong — most likely the filter should be a smaller offset than the guard, so a
    cluster starting immediately below the caption really does suppress the trim.
    The assertion below is the invariant that makes the branch dead: the smallest
    cluster offset the filter admits still trims.
    """
    pages = [
        FakePage(),
        FakePage(
            drawings=[drawing(120.5, 400.0)],  # the least the y0 > crop.y0 + 20 filter allows
            blocks=[block("Figure 8 - lower", y0=430.0)],
        ),
    ]
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        pages,
        {
            0: [region("image", (100, 100, 400, 700), "Figure 6 - a", caption_y0=200.0)],
            1: [region("image", (100, 100, 400, 500), "Figure 7 - b", caption_y0=480.0)],
        },
    )

    # 470 = the page-0 fragment; 15.5 = the trimmed caption strip, never the full 380.
    assert PILImage.open(io.BytesIO(figures[7])).height == 470 + 16


def test_a_trim_whose_crop_fails_falls_back_to_the_untrimmed_image(prescan, monkeypatch):
    """The trim is an optimisation; losing it must not lose the figure. Only the narrow
    caption-only clip is made to fail, so the first crop of the same page succeeds."""
    pages = [
        FakePage(),
        FakePage(
            drawings=[drawing(200.0, 400.0)],
            blocks=[block("Figure 8 - lower", y0=430.0)],
            fail=lambda clip: clip.y1 == 195.0,
        ),
    ]
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        pages,
        {
            0: [region("image", (100, 100, 400, 700), "Figure 6 - a", caption_y0=200.0)],
            1: [region("image", (100, 100, 400, 500), "Figure 7 - b", caption_y0=480.0)],
        },
    )

    assert PILImage.open(io.BytesIO(figures[7])).height == 470 + 380


def test_a_cluster_with_no_later_caption_is_kept_in_the_stitched_figure(prescan, monkeypatch):
    """The trim is conditional on another figure claiming the cluster; body text below it
    is not a claim."""
    pages = [
        FakePage(),
        FakePage(
            drawings=[drawing(200.0, 400.0)],
            blocks=[block("The values are given in the table below.", y0=430.0)],
        ),
    ]
    figures, _tables, _formulas = run(
        prescan,
        monkeypatch,
        pages,
        {
            0: [region("image", (100, 100, 400, 700), "Figure 6 - a", caption_y0=200.0)],
            1: [region("image", (100, 100, 400, 500), "Figure 7 - b", caption_y0=480.0)],
        },
    )

    assert PILImage.open(io.BytesIO(figures[7])).height == 470 + 380


# ── the normal path: tables ───────────────────────────────────────────────────


def test_an_uncaptioned_table_is_keyed_by_page_and_flushed_at_end_of_page(prescan, monkeypatch):
    """Cross-reference matrices carry no "Table N —" line. They still have to be keyed,
    because the rect suppression means their cell text is no longer emitted as body rows;
    dropping the image would lose the content entirely."""
    result = run(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        {
            1: [
                region("table", (72, 300, 500, 400), caption="Measured values"),
                region("table", (72, 500, 500, 600)),
            ]
        },
        toc=[],
    )

    assert sorted(result[1]) == ["unlabeled_1_0", "unlabeled_1_1"]
    assert result[6] == {1: ["unlabeled_1_0", "unlabeled_1_1"]}


def test_an_uncaptioned_tables_filename_records_the_page_it_came_from(prescan, monkeypatch, tmp_path):
    """An empty caption is replaced by a placeholder; a non-matching one is kept as-is."""
    workspace = FakeWorkspace(tmp_path / "ws")
    run(
        prescan,
        monkeypatch,
        [FakePage()],
        {
            0: [
                region("table", (72, 300, 500, 400), caption="Measured values"),
                region("table", (72, 500, 500, 600)),
            ]
        },
        ws=workspace,
    )

    assert sorted(workspace.put) == ["Measured values.png", "Table (unlabeled, page 1).png"]


def test_a_table_whose_crop_fails_still_suppresses_its_text(prescan, monkeypatch):
    """The defect this guards against is worse than a missing image: with no rect, every
    cell of the table is re-emitted as body rows in the description column. So the rect
    is recorded before the crop is attempted, and survives its failure."""
    result = run(
        prescan,
        monkeypatch,
        [FakePage(fail=lambda clip: True)],
        {0: [region("table", (72, 300, 500, 400), caption="Table 7 - broken")]},
        toc=[],
    )

    assert result[1] == {}, "no image"
    assert result[4] == {0: [(72.0, 300.0, 500.0, 400.0)]}


# ── the normal path: numbered formulas ────────────────────────────────────────


@pytest.mark.parametrize(
    "blk, why",
    [
        (block("Copyrighted material licensed to ABBVIE  x = y (4)"), "watermark"),
        (block("x = y (4)", btype=1), "image block, not text"),
        (block("   ", btype=0), "whitespace only"),
        (block("see 6.2 (4)"), "no maths character"),
        (block("x = y  (4) mm"), "the number does not trail the block"),
    ],
)
def test_a_formula_candidate_is_rejected_when(prescan, monkeypatch, blk, why):
    formulas = run(prescan, monkeypatch, [FakePage(blocks=[blk])])[2]
    assert formulas == {}, why


def test_a_numbered_formula_is_cropped_with_a_twenty_point_side_margin(prescan, monkeypatch):
    """The margin exists because the trailing "(N)" sits outside the text block's own
    bbox on some ISO layouts; without it the number is clipped off."""
    page = FakePage(blocks=[(72.0, 300.0, 523.0, 330.0, "P = F / A (4)", 0, 0)])
    formulas = run(prescan, monkeypatch, [page])[2]

    assert sorted(formulas) == [4]
    assert page.clips[0] == fitz.Rect(52.0, 295.0, 543.0, 335.0)


def test_the_first_appearance_of_a_formula_number_wins(prescan, monkeypatch):
    """Formula numbers repeat on continuation pages of the same clause."""
    pages = [
        FakePage(blocks=[(72.0, 300.0, 523.0, 330.0, "x = 1 (4)", 0, 0)]),
        FakePage(blocks=[(72.0, 600.0, 523.0, 660.0, "x = 2 (4)", 0, 0)]),
    ]
    formulas = run(prescan, monkeypatch, pages)[2]

    assert PILImage.open(io.BytesIO(formulas[4])).height == 40, "page 0's crop, not page 1's"


def test_a_formula_block_shorter_than_five_points_is_not_cropped(prescan, monkeypatch):
    """y1 + 5 against max(y0 - 5, 0): a block at the very top of the page collapses."""
    page = FakePage(blocks=[(72.0, 0.0, 523.0, -1.0, "x = y (4)", 0, 0)])
    assert run(prescan, monkeypatch, [page])[2] == {}


# ── the vision path: table stitching and its guards ───────────────────────────


def vision(prescan, monkeypatch, pages, textract, llm_pages, **kwargs):
    return run(prescan, monkeypatch, pages, None,
               llm_pages=llm_pages, textract=textract, **kwargs)


def test_a_null_page_entry_is_skipped_on_both_vision_passes(prescan, monkeypatch):
    """`llm_pages` comes from a per-page thread pool that yields None for a page whose
    model call failed, so a hole in the list is normal input, not corruption."""
    textract = FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]])
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        textract,
        [None, llm_page(1, text="Table 4 - real\n\nrows")],
    )

    assert textract.calls == 1, "the None page was never rendered or charged"
    assert sorted(result[1]) == ["4"]


def test_an_uncaptioned_table_continues_the_table_from_the_previous_page(prescan, monkeypatch):
    """A continuation page has no caption and no column headers; the only evidence that
    it is the same table is that its x-extent matches. Two entries under one ref is the
    per-page list the emitter inserts in order."""
    text = "Table 4 - Test methods\n\nrows and rows"
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]]),
        [llm_page(0, text=text), llm_page(1, text="continued rows")],
        toc=[],
    )

    assert sorted(result[1]) == ["4"]
    assert len(result[1]["4"]) == 2
    assert result[7]["table:4"] == 0, "first capture, not last"


def test_a_continuation_page_uses_a_taller_top_padding(prescan, monkeypatch):
    """55 points instead of 20, so the repeated "Table 4 (continued)" header row and the
    column labels are inside the crop."""
    pages = [FakePage(), FakePage()]
    vision(
        prescan,
        monkeypatch,
        pages,
        FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]]),
        [llm_page(0, text="Table 4 - Test methods\n\nrows"), llm_page(1, text="rows")],
    )

    assert pages[0].clips[-1].y0 == pytest.approx(0.30 * PAGE_H - 20)
    assert pages[1].clips[-1].y0 == pytest.approx(0.30 * PAGE_H - 55)


@pytest.mark.parametrize(
    "x_range, why",
    [
        ((0.86, 0.98), "x-overlap is only a third of the candidate's width"),
        ((0.10, 0.10), "zero width — the ratio is undefined so nothing can match"),
    ],
)
def test_an_uncaptioned_table_that_does_not_line_up_is_dropped(prescan, monkeypatch, x_range, why):
    """Overlap must be at least half the candidate's width. Under that, the region is
    more likely a contents page or a stray detection than a continuation."""
    x0n, x1n = x_range
    textract = FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]])
    doc_pages = [FakePage(), FakePage()]

    # Page 1's detection differs, so it must not join page 0's table.
    def per_page(png_bytes_, feature_types=None):
        textract.calls += 1
        boxes = [[0.10, 0.30, 0.90, 0.50]] if textract.calls == 1 else [[x0n, 0.30, x1n, 0.50]]
        return {"Blocks": FakeTextract(tables=boxes).blocks()}

    result = vision(
        prescan,
        monkeypatch,
        doc_pages,
        per_page,
        [llm_page(0, text="Table 4 - Test methods\n\nrows"), llm_page(1, text="rows")],
    )

    assert len(result[1]["4"]) == 1, why


def test_a_gap_of_more_than_two_pages_breaks_the_continuation(prescan, monkeypatch):
    """Bounded at 2 so one intervening text-only page still stitches while a table
    forty pages later does not."""
    text = "Table 4 - Test methods\n\nrows"
    boxes = [[0.10, 0.30, 0.90, 0.50]]

    def result_for(second_page):
        return vision(
            prescan,
            monkeypatch,
            [FakePage() for _ in range(6)],
            FakeTextract(tables=boxes),
            [llm_page(0, text=text), llm_page(second_page, text="rows")],
        )

    assert len(result_for(2)[1]["4"]) == 2, "gap 2 stitches"
    assert len(result_for(3)[1]["4"]) == 1, "gap 3 does not"


def test_a_table_detection_beside_a_figure_caption_is_never_stitched(prescan, monkeypatch):
    """The measurement-value boxes inside a circuit diagram look like tables to Textract.
    Stitching one as a continuation drags the figure's page into the preceding table's
    capture, so the figure image is emitted inside a table row and again at its own
    section. The guard drops it instead."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]]),
        [
            llm_page(0, text="Table 4 - Test methods\n\nrows"),
            llm_page(1, text="Figure 9 - Circuit diagram\n\nCF 1,2\n\nBF 3,4"),
        ],
    )

    assert len(result[1]["4"]) == 1


def test_a_narrow_textract_table_box_is_width_extended_not_dropped(prescan, monkeypatch):
    """Textract occasionally returns a degenerate box (here ~3pt wide). The vision path
    now widens it with the border/right-margin extension before cropping, so it clears the
    >5pt crop guard and Table 4 is captured rather than discarded. The ≤5pt guard still
    exists — it catches a clip that stays degenerate after extension and keeps the PyMuPDF
    crop from raising on a zero-width rect. The earlier port dropped the box before any
    extension; the deployed server extends first, which is the behaviour matched here."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(tables=[[0.50, 0.30, 0.505, 0.50]]),
        [llm_page(0, text="Table 4 - Test methods\n\nrows")],
    )

    assert sorted(result[1]) == ["4"]


# ── the vision path: figures ──────────────────────────────────────────────────


def test_a_captioned_model_bbox_expands_a_short_textract_figure(prescan, monkeypatch):
    """Textract reports the dense part of a diagram and misses the sparse remainder. The
    union is guarded on an explicit "Figure N —" caption because a captionless model
    detection can span two adjacent figures, and unioning that pulls the next figure's
    artwork into this one."""
    page = FakePage()
    result = vision(
        prescan,
        monkeypatch,
        [page],
        FakeTextract(figures=[[0.20, 0.20, 0.60, 0.40]]),
        [
            llm_page(
                0,
                images=[
                    {"caption": "no caption shape", "bbox_norm": [0.0, 0.0, 1.0, 1.0]},
                    {"caption": "Figure 3 - no bbox at all"},
                    {"caption": "Figure 3 - Circuit", "bbox_norm": [0.05, 0.05, 0.65, 0.55]},
                ],
            )
        ],
        toc=[],
    )

    assert sorted(result[0]) == [3]
    # Union of the two boxes, then a 15-point pad on every side.
    assert result[5][0] == [
        pytest.approx((0.05 * PAGE_W - 15, 0.05 * PAGE_H - 15,
                       0.65 * PAGE_W + 15, 0.55 * PAGE_H + 15))
    ]


@pytest.mark.parametrize(
    "bbox, why",
    [
        ([0.55, 0.05, 0.99, 0.55], "x-overlap is 11% of the model box's width, under 30%"),
        ([0.05, 0.38, 0.65, 0.98], "y-overlap is 3% of the model box's height, under 20%"),
    ],
)
def test_a_barely_overlapping_model_bbox_does_not_expand_the_figure(
    prescan, monkeypatch, bbox, why
):
    """Both thresholds pinned from the failing side; the test above pins the passing one.
    Each box still overlaps enough to *name* the figure, which is a weaker test than
    expanding it — the two decisions use different thresholds on purpose."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.20, 0.60, 0.40]]),
        [llm_page(0, images=[{"caption": "Figure 3 - Circuit", "bbox_norm": bbox}])],
        toc=[],
    )

    assert sorted(result[0]) == [3]
    assert result[5][0] == [
        pytest.approx((0.20 * PAGE_W - 15, 0.20 * PAGE_H - 15,
                       0.60 * PAGE_W + 15, 0.40 * PAGE_H + 15))
    ], why


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_the_caption_of_the_most_overlapping_model_image_wins(prescan, monkeypatch, order):
    """A page can carry two figures. The caption for the Textract-detected box is taken
    from whichever model image covers more of it (Figure 4 here), and that pick must not
    depend on list order.

    The LLM-figure supplement (_VISION_USE_LLM_BOXES) additionally emits Figure 3 as a
    figure in its own right: its bbox runs to y=0.99, well past the Textract box's y1=0.40,
    so it is a distinct detection rather than the same one. Both figures are therefore
    captured — Figure 4 cropped to the wide, short Textract rectangle and Figure 3 to its
    own narrow, tall bbox. This matches the deployed server; the earlier port lacked the
    supplement and emitted only Figure 4.
    """
    images = [
        {"caption": "Figure 3 - sliver", "bbox_norm": [0.30, 0.39, 0.50, 0.99]},
        {"caption": "Figure 4 - wider", "bbox_norm": [0.25, 0.38, 0.75, 0.98]},
    ]
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.20, 0.80, 0.40]]),
        [llm_page(0, images=[images[index] for index in order])],
    )

    assert sorted(result[0]) == [3, 4]
    # Figure 4 is the one matched to the Textract box, so its crop is that box (wide and
    # short); Figure 3's own bbox is narrow and tall. The shapes prove the most-overlapping
    # image (4, not 3) won the Textract box, regardless of the input order.
    w4, h4 = PILImage.open(io.BytesIO(result[0][4])).size
    w3, h3 = PILImage.open(io.BytesIO(result[0][3])).size
    assert w4 > h4 and h3 > w3


def test_a_captioned_model_image_without_a_bbox_still_names_the_figure(prescan, monkeypatch):
    """Fallback when the model gave a caption but no coordinates: the Textract box is
    used for the crop and the caption only for the number."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.20, 0.80, 0.40]]),
        [llm_page(0, images=[{"caption": "Figure 9 - Assembly", "bbox_norm": [1, 2]}])],
    )

    assert sorted(result[0]) == [9]


def test_a_caption_below_the_figure_is_found_when_nothing_above_matches(prescan, monkeypatch):
    """ISO puts figure captions below the artwork, so the below-search is the normal
    case; the above-search runs first only because a continuation page can repeat it."""
    text = "\n\n".join(
        ["intro", "clause 5", "more prose", "yet more", "Figure 6 - Below the artwork", "notes"]
    )
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.05, 0.80, 0.55]]),
        [llm_page(0, text=text)],
    )

    assert sorted(result[0]) == [6]


def test_an_uncaptioned_figure_takes_a_negative_key_on_the_vision_path(prescan, monkeypatch):
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.20, 0.80, 0.40], [0.20, 0.60, 0.80, 0.80]]),
        [llm_page(0, text="nothing captioned here")],
    )

    assert sorted(result[0]) == [-2, -1]


def test_a_figure_clip_collapsed_by_the_page_edge_is_dropped(prescan, monkeypatch):
    """Textract can report a box below the page bottom; clamping it to the page leaves
    an inverted rect, and cropping that raises."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[1.2, 1.2, 1.3, 1.3]]),
        [llm_page(0, text="Figure 6 - somewhere")],
    )

    assert result[0] == {}


# ── the vision path: the model-bbox fallback ──────────────────────────────────


@pytest.mark.parametrize(
    "entry, why",
    [
        ({"caption": "Methods and results", "bbox_norm": [0.1, 0.3, 0.9, 0.5]}, "no ref"),
        ({"caption": "Table 4 - Methods"}, "no bbox at all"),
        ({"caption": "Table 4 - Methods", "bbox_norm": [0.1, 0.3, 0.9]}, "bbox too short"),
        ({"caption": "Table 4 - Methods", "bbox_norm": [0.90, 0.30, 0.10, 0.50]}, "inverted"),
    ],
)
def test_the_table_fallback_skips_a_model_entry_when(prescan, monkeypatch, entry, why):
    result = vision(
        prescan, monkeypatch, [FakePage()], FakeTextract(), [llm_page(0, tables=[entry])]
    )
    assert result[1] == {}, why


def test_the_table_fallback_does_not_overwrite_a_textract_capture(prescan, monkeypatch):
    """Textract's cell geometry beats the model's estimate, so the fallback only fills
    gaps — and it would flatten a multi-page list to one entry if it did not."""
    text = "Table 4 - Test methods\n\nrows"
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(), FakePage()],
        FakeTextract(tables=[[0.10, 0.30, 0.90, 0.50]]),
        [
            llm_page(0, text=text,
                     tables=[{"caption": "Table 4 - Test methods",
                              "bbox_norm": [0.0, 0.0, 1.0, 1.0]}]),
            llm_page(1, text="rows"),
        ],
    )

    assert len(result[1]["4"]) == 2, "the two Textract page crops survive"


def test_a_fallback_table_whose_crop_fails_is_simply_absent(prescan, monkeypatch):
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(fail=lambda clip: True)],
        FakeTextract(),
        [llm_page(0, tables=[{"caption": "Table 4 - m", "bbox_norm": [0.1, 0.3, 0.9, 0.5]}])],
    )

    assert result[1] == {}


@pytest.mark.parametrize(
    "entry, why",
    [
        ({"caption": "Assembly diagram", "bbox_norm": [0.1, 0.3, 0.9, 0.5]}, "no ref"),
        ({"caption": "Figure 4 - a"}, "no bbox at all"),
        ({"caption": "Figure 4 - a", "bbox_norm": [0.1, 0.3]}, "bbox too short"),
        ({"caption": "Figure 4 - a", "bbox_norm": [0.90, 0.30, 0.10, 0.50]}, "inverted"),
    ],
)
def test_the_figure_fallback_skips_a_model_entry_when(prescan, monkeypatch, entry, why):
    result = vision(
        prescan, monkeypatch, [FakePage()], FakeTextract(), [llm_page(0, images=[entry])]
    )
    assert result[0] == {}, why


def test_the_figure_fallback_does_not_overwrite_a_textract_capture(prescan, monkeypatch):
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(figures=[[0.20, 0.20, 0.80, 0.40]]),
        [llm_page(0, images=[{"caption": "Figure 3 - Circuit",
                              "bbox_norm": [0.05, 0.38, 0.65, 0.98]}])],
        toc=[],
    )

    # The Textract crop, padded by 15 — not the model box, and only recorded once.
    assert result[5][0] == [
        pytest.approx((0.20 * PAGE_W - 15, 0.20 * PAGE_H - 15,
                       0.80 * PAGE_W + 15, 0.40 * PAGE_H + 15))
    ]


def test_a_fallback_figures_bottom_is_clipped_at_the_next_figure_on_the_page(prescan, monkeypatch):
    """An over-generous model bbox for Figure 3 otherwise swallows Figure 4's artwork,
    which then appears inside Figure 3's row and never in its own. Only later-numbered
    figures clip: an earlier one is above, and a bbox-less one gives nothing to clip to."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage()],
        FakeTextract(),
        [
            llm_page(
                0,
                images=[
                    {"caption": "Figure 3 - too tall", "bbox_norm": [0.10, 0.10, 0.90, 0.90]},
                    {"caption": "not a caption", "bbox_norm": [0.10, 0.20, 0.90, 0.30]},
                    {"caption": "Figure 2 - earlier", "bbox_norm": [0.10, 0.02, 0.90, 0.08]},
                    {"caption": "Figure 9 - no bbox"},
                    {"caption": "Figure 4 - next", "bbox_norm": [0.10, 0.50, 0.90, 0.90]},
                ],
            )
        ],
        toc=[],
    )

    assert sorted(result[0]) == [2, 3, 4]
    x0, y0, x1, y1 = result[5][0][0]   # figure 3's, recorded first
    assert y1 == pytest.approx(0.50 * PAGE_H + 6), "clipped to Figure 4's top, plus the pad"
    assert y0 == pytest.approx(0.10 * PAGE_H - 6)


def test_a_fallback_figure_whose_crop_fails_records_no_suppression_rect(prescan, monkeypatch):
    """Unlike the table branch, the figure rect is appended *after* a successful crop —
    so a failed figure crop suppresses nothing and its embedded labels stay in the body."""
    result = vision(
        prescan,
        monkeypatch,
        [FakePage(fail=lambda clip: True)],
        FakeTextract(),
        [llm_page(0, images=[{"caption": "Figure 4 - a",
                              "bbox_norm": [0.1, 0.3, 0.9, 0.5]}])],
        toc=[],
    )

    assert result[0] == {} and result[5] == {}


# ── unnumbered formulas ───────────────────────────────────────────────────────


def test_the_unnumbered_formula_detector_runs_only_with_a_non_empty_toc(prescan, monkeypatch):
    """It needs the TOC slice to know which clauses to scan, and it is the one part of
    the pre-scan that costs model calls — so a falsy toc must not reach it."""
    seen = []

    def detector(pdf_path, toc=None, toc_start_idx=0, toc_end_idx=None, ws=None, llm=None):
        seen.append((toc, toc_start_idx, toc_end_idx))
        return [{"number": None, "image": "/tmp/f.png"}]

    monkeypatch.setattr(prescan, "find_unnumbered_formulas", detector)
    toc = [{"level": 1, "title": "5 Requirements", "page": 4}]
    result = run(prescan, monkeypatch, [FakePage()], None,
                 toc=toc, toc_start_idx=1, toc_end_idx=9)

    assert result[3] == [{"number": None, "image": "/tmp/f.png"}]
    assert seen == [(toc, 1, 9)]


def test_a_failing_unnumbered_formula_detector_costs_only_the_formulas(prescan, monkeypatch):
    """It is the last and least reliable stage — an exception there must not throw away
    the figures and tables already extracted."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("frml_ext blew up")

    monkeypatch.setattr(prescan, "find_unnumbered_formulas", boom)
    result = run(
        prescan,
        monkeypatch,
        [FakePage()],
        {0: [region("image", (100, 100, 400, 300), "Figure 3 - kept")]},
        toc=[{"level": 1, "title": "5 Requirements", "page": 4}],
    )

    assert result[3] == []
    assert sorted(result[0]) == [3], "the figure survived the failure"
