"""Rectangle maths and page-region detection. Ported from backend.py:2241-2662.

`_extract_page_regions` is the workhorse of the text path: given a page, it decides which
rectangles are figures and which are tables, and attaches the caption belonging to each.
Everything downstream — what gets cropped to a PNG, which text is suppressed so it is not
emitted twice, which caption names the file — follows from its output.

Its return value is a list of dicts:

    {"type": "image" | "table", "bbox": [x0, y0, x1, y1], "caption": str | None}

with one extra key, `"caption_y0"`, present **only** on merged figure entries and absent
from tables and from orphan-caption images. Read it with `.get()`; `prescan_media` does.

The hard part this solves is that a figure is not a single object in a PDF. It is a
scatter of vector strokes, sometimes with rasters, sometimes with sub-diagrams that
cluster separately, sitting near body text that must not be swallowed. Hence the seed →
grow → merge → second-caption-sweep → orphan-fallback sequence, and hence the comments,
which record which real documents each rule was tuned against.

No seam changes: this module is pure geometry over a `fitz` page. It imports its
constants from `mediastore` because that is where the source declares them.
"""

from __future__ import annotations

import re
from collections import Counter

import fitz

from .mediastore import _CAP_LABEL_RE, _CAP_PROX, _PROX, _TWO_LINE_GAP


def _rect_area(r):
    return max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)


def _intersect_area(r1, r2):
    return (max(0.0, min(r1.x1, r2.x1) - max(r1.x0, r2.x0)) *
            max(0.0, min(r1.y1, r2.y1) - max(r1.y0, r2.y0)))


def _overlaps_any(rect, regions, threshold=0.05):
    area = _rect_area(rect)
    if area <= 0:
        return True
    return any(_intersect_area(rect, fitz.Rect(r["bbox"])) / area > threshold
               for r in regions)


def _cluster_drawings(page_drawings, gap=30):
    items = []
    for d in page_drawings:
        r = fitz.Rect(d["rect"])
        if r.width > 3 and r.height > 3:
            items.append([r, [d]])
    if not items:
        return []
    changed = True
    while changed:
        changed = False
        out = []
        used = [False] * len(items)
        for i in range(len(items)):
            if used[i]:
                continue
            ri, di = items[i]
            for j in range(i + 1, len(items)):
                if used[j]:
                    continue
                rj = items[j][0]
                pad = fitz.Rect(ri.x0 - gap, ri.y0 - gap,
                                ri.x1 + gap, ri.y1 + gap)
                if pad.intersects(rj):
                    ri = ri | rj
                    di = di + items[j][1]
                    used[j] = True
                    changed = True
            out.append([ri, di])
        items = out
    return [(r, d) for r, d in items]


def _is_real_figure(drawings, bbox, free_text, page_w):
    if bbox.x0 > page_w * 0.82:
        return False
    substantial = [d for d in drawings
                   if fitz.Rect(d["rect"]).width > 3 and fitz.Rect(d["rect"]).height > 3]
    has_curves = any(item[0] == "c"
                     for d in substantial for item in d.get("items", []))
    has_fill   = any(d.get("fill") not in (None, (1, 1, 1), [1, 1, 1])
                     for d in substantial)
    many_paths = len(substantial) >= 6
    # Many drawing paths → definitely a figure; skip text-coverage check.
    # Circuit diagrams often sit alongside body text, inflating the coverage
    # ratio and causing false negatives when the text-overlap gate runs first.
    if many_paths:
        return True
    area = _rect_area(bbox)
    if area > 0:
        cov = sum(_intersect_area(fitz.Rect(t["bbox"]), bbox) for t in free_text)
        if cov / area > 0.10:
            return False
    return has_curves or has_fill


def _group_text_blocks(blocks, gap=_TWO_LINE_GAP):
    if not blocks:
        return []
    blocks = sorted(blocks, key=lambda b: b["bbox"][1])
    groups = [[blocks[0]]]
    for b in blocks[1:]:
        if b["bbox"][1] - groups[-1][-1]["bbox"][3] <= gap:
            groups[-1].append(b)
        else:
            groups.append([b])
    result = []
    for g in groups:
        result.append({"type": "text", "bbox": [
            min(b["bbox"][0] for b in g),
            min(b["bbox"][1] for b in g),
            max(b["bbox"][2] for b in g),
            max(b["bbox"][3] for b in g),
        ]})
    return result


def _extract_page_regions(page):
    """Return list of {type, bbox, caption} for image/table regions on *page*.

    Ported from img_ext/extImg.py `get_regions`, with two changes:
      - Tables capture the "Table N" title text as `caption`.
      - Plain text regions are not returned (not needed by main.py).
    """
    ph    = page.rect.height
    pw    = page.rect.width
    parea = pw * ph
    HY    = ph * 0.08
    FY    = ph * 0.92

    raw = []
    page_size_counts = Counter()

    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        blk_style  = Counter()
        text_parts = []
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                text_parts.append(sp["text"])
                stripped = sp["text"].strip()
                if not stripped:
                    continue
                key = (round(sp["size"], 1),
                       bool(sp["flags"] & 2),    # italic
                       bool(sp["flags"] & 16))   # bold
                blk_style[key] += len(stripped)
                page_size_counts[key[0]] += len(stripped)
        text = " ".join(text_parts).strip()
        if not text:
            continue
        dom_size, dom_ital, dom_bold = (blk_style.most_common(1)[0][0]
                                        if blk_style else (0.0, False, False))
        raw.append({
            "bbox":       list(blk["bbox"]),
            "text":       text,
            "size":       dom_size,
            "italic":     dom_ital,
            "bold":       dom_bold,
            "is_ttl":     bool(re.match(r"^Table\s+\S+", text, re.I)),
            "is_fig_cap": bool(_CAP_LABEL_RE.match(text)),
        })

    body_size = (page_size_counts.most_common(1)[0][0]
                 if page_size_counts else 10.0)
    for r in raw:
        r["is_cap_style"] = (
            not r["is_fig_cap"]
            and r["size"] > 0
            and (r["size"] < body_size - 0.3 or r["italic"])
        )

    claimed  = []
    tbl_used = set()
    fig_used = set()

    # ── Tables: find_tables + claim "Table N" title above ────────────────
    try:
        for tab in page.find_tables():
            tr = fitz.Rect(tab.bbox)
            tbl_caption = None
            for i, b in enumerate(raw):
                if i in tbl_used:
                    continue
                br = fitz.Rect(b["bbox"])
                if b["is_ttl"] and br.y1 <= tr.y0 + 5 and (tr.y0 - br.y1) <= 40:
                    tr |= br
                    tbl_used.add(i)
                    if tbl_caption is None:
                        tbl_caption = b["text"]
            if tr.width > 20 and tr.height > 20 and not _overlaps_any(tr, claimed, 0.30):
                claimed.append({"type": "table", "bbox": list(tr),
                                "caption": tbl_caption})
    except Exception:
        pass

    free_text = [b for i, b in enumerate(raw)
                 if i not in tbl_used
                 and not _overlaps_any(fitz.Rect(b["bbox"]), claimed, 0.30)]

    # ── Figure seeds: rasters + inline images + clustered drawings ───────
    seeds = []
    seen  = set()

    for info in page.get_images(full=True):
        try:
            for r in page.get_image_rects(info[0]):
                if r.width < 10 or r.height < 10:
                    continue
                k = tuple(round(v) for v in r)
                if k not in seen:
                    seeds.append((r, "raster"))
                    seen.add(k)
        except Exception:
            pass

    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] == 1:
            r = fitz.Rect(blk["bbox"])
            k = tuple(round(v) for v in r)
            if k not in seen and r.width > 10 and r.height > 10:
                seeds.append((r, "inline"))
                seen.add(k)

    for u, ds in _cluster_drawings(page.get_drawings(), gap=30):
        if u.y1 < HY or u.y0 > FY:
            continue
        # Clip to visible page area before area/validity checks — some drawing
        # paths extend above/below the page boundary (negative y0, y1 > ph),
        # which inflates the cluster bbox and makes real figures look like noise.
        u_vis = u & fitz.Rect(0, HY, pw, FY)
        if u_vis.is_empty or u_vis.height < 8:
            continue
        af = _rect_area(u_vis) / parea
        if not (0.01 < af < 0.85):
            continue
        if _overlaps_any(u_vis, claimed, 0.05):
            continue
        if not _is_real_figure(ds, u_vis, free_text, pw):
            continue
        k = tuple(round(v) for v in u_vis)
        if k not in seen:
            seeds.append((u_vis, "vector"))
            seen.add(k)

    # ── Grow each seed to absorb nearby captions ─────────────────────────
    fig_cands = []
    for seed_rect, _kind in seeds:
        fig             = fitz.Rect(seed_rect)
        seed_y0         = seed_rect.y0   # original seed top — non-caption text must not pull bbox above this
        caption_text    = None
        style_caption   = None
        caption_bottom  = None
        caption_y0_crop = None   # y0 of caption block below figure — used to exclude caption from PNG crop
        changed         = True
        while changed:
            changed = False
            for raw_i, b in enumerate(raw):
                if raw_i in tbl_used or raw_i in fig_used:
                    continue
                br = fitz.Rect(b["bbox"])
                if br.y0 < HY:
                    continue
                if _overlaps_any(br, claimed, 0.30):
                    continue
                if caption_bottom is not None and br.y0 > caption_bottom + 5:
                    continue
                is_cap = b.get("is_fig_cap") or b.get("is_cap_style")
                prox = _CAP_PROX if b.get("is_fig_cap") else _PROX
                # Prevent non-caption body text above the figure seed from being
                # absorbed (which would cascade and merge separate figures).
                # Rule: non-caption blocks whose TOP edge is above the seed top
                # are rejected outright; only caption blocks above the seed
                # (e.g. "Figure N —" that appears above the circuit diagram)
                # are allowed to pull the bbox upward.
                if not is_cap and br.y0 < seed_y0:
                    continue
                br_y1_above_seed = br.y1 <= seed_y0
                if is_cap and br_y1_above_seed:
                    top_limit = fig.y0 - prox    # caption above seed: allow upward
                else:
                    top_limit = seed_y0           # everything else: clamp at seed top
                pad = fitz.Rect(fig.x0 - prox, top_limit,
                                fig.x1 + prox, fig.y1 + prox)
                if pad.intersects(br):
                    if b.get("is_fig_cap") and caption_bottom is None:
                        caption_bottom  = br.y1
                        caption_text    = b["text"]
                        # Track where caption starts (only when it's below the figure seed)
                        # so prescan can crop the PNG above the caption line.
                        if br.y0 > seed_y0:
                            caption_y0_crop = br.y0
                    elif b.get("is_cap_style") and style_caption is None:
                        style_caption = b["text"]
                    fig |= br
                    fig_used.add(raw_i)
                    changed = True
        fig_cands.append({"r": fig, "caption": caption_text or style_caption,
                          "caption_y0": caption_y0_crop})

    # ── Merge overlapping figures + second caption sweep ─────────────────
    if fig_cands:
        merged = []
        for fc in sorted(fig_cands, key=lambda x: _rect_area(x["r"]), reverse=True):
            placed = False
            for m in merged:
                pad = fitz.Rect(m["r"].x0 - 50, m["r"].y0 - 50,
                                m["r"].x1 + 50, m["r"].y1 + 50)
                if pad.intersects(fc["r"]):
                    m["r"] = m["r"] | fc["r"]
                    if not m.get("caption") and fc.get("caption"):
                        m["caption"] = fc["caption"]
                    if not m.get("caption_y0") and fc.get("caption_y0"):
                        m["caption_y0"] = fc["caption_y0"]
                    placed = True
                    break
            if not placed:
                merged.append({"r": fitz.Rect(fc["r"]), "caption": fc.get("caption"),
                               "caption_y0": fc.get("caption_y0")})

        for m in merged:
            for raw_i, b in enumerate(raw):
                if raw_i in tbl_used or raw_i in fig_used:
                    continue
                if not b.get("is_fig_cap"):
                    continue
                br  = fitz.Rect(b["bbox"])
                pad = fitz.Rect(m["r"].x0 - _CAP_PROX, m["r"].y0 - _CAP_PROX,
                                m["r"].x1 + _CAP_PROX, m["r"].y1 + _CAP_PROX)
                if pad.intersects(br):
                    m["r"] = m["r"] | br
                    if not m.get("caption"):
                        m["caption"] = b["text"]
                    fig_used.add(raw_i)

        # ── Union uncaptioned sub-figures into a captioned figure below them ──
        # When a page has sub-diagrams (a, b), each clusters separately. The
        # main "Figure N" caption belongs to the lower sub-figure or appears
        # below both. The upper sub-figure gets no caption. Without this step
        # the upper sub-figure is emitted as a separate orphan image or dropped.
        # Strategy: for each uncaptioned entry, if there is a captioned entry
        # whose top edge is within 400 px below the uncaptioned entry's bottom,
        # and their x-ranges overlap, absorb the uncaptioned entry into the
        # captioned one.
        _fig_num_re = re.compile(r'\bfig(?:ure)?\s*\d', re.I)
        for _i, mc in enumerate(merged):
            if mc.get("caption"):
                continue
            for _j, mp in enumerate(merged):
                if _i == _j:
                    continue
                if not mp.get("caption"):
                    continue
                if not _fig_num_re.search(mp["caption"]):
                    continue
                # mc is above mp and x-ranges overlap
                if mc["r"].y1 <= mp["r"].y0 + 400:
                    x_overlap = min(mc["r"].x1, mp["r"].x1) - max(mc["r"].x0, mp["r"].x0)
                    if x_overlap > 0:
                        mp["r"] = mp["r"] | mc["r"]
                        mc["_merged_into"] = _j
                        break

        merged = [m for m in merged if not m.get("_merged_into")]

        for m in merged:
            if not _overlaps_any(m["r"], claimed, 0.20):
                claimed.append({"type": "image", "bbox": list(m["r"]),
                                "caption": m.get("caption"),
                                "caption_y0": m.get("caption_y0")})

    # ── Orphaned-caption fallback ─────────────────────────────────────────
    # If a "Figure N" caption exists in page text but no claimed image has
    # that caption, capture the page strip between the previous image's
    # bottom and the caption as a new image region.  This recovers figure
    # sub-areas whose vector paths are too scattered to form a large cluster
    # (e.g. circuit diagrams with many thin isolated strokes).
    claimed_images = [r for r in claimed if r["type"] == "image"]
    for b in raw:
        if not b.get("is_fig_cap"):
            continue
        cap_text = b["text"]
        cap_rect = fitz.Rect(b["bbox"])
        # Check if any existing claimed image already covers this caption
        already_covered = any(
            fitz.Rect(r["bbox"]).y1 >= cap_rect.y0 - _CAP_PROX
            for r in claimed_images
        )
        if already_covered:
            continue
        # Find the previous claimed image above this caption
        above = [r for r in claimed_images if fitz.Rect(r["bbox"]).y1 < cap_rect.y0]
        if above:
            _prev_y1 = fitz.Rect(above[-1]["bbox"]).y1
            # Extend upward to include drawing clusters sitting just above the
            # previous image's bottom boundary (shared circuit area between two
            # consecutive figures, e.g. Figure 7 and Figure 8 on the same page).
            _look_rect = fitz.Rect(0, max(HY, _prev_y1 - 250), pw, _prev_y1)
            _near_draws = [fitz.Rect(d["rect"]) for d in page.get_drawings()
                           if _look_rect.intersects(fitz.Rect(d["rect"]))
                           and fitz.Rect(d["rect"]).height > 50]
            if _near_draws:
                strip_top = max(HY, min(r.y0 for r in _near_draws) - 5)
            else:
                strip_top = max(HY, _prev_y1 + 2)
        else:
            strip_top = HY
        strip_bot = cap_rect.y0 - 2
        if strip_bot - strip_top < 30:
            continue
        # Check if there are any vector drawings in this strip
        strip = fitz.Rect(0, strip_top, pw, strip_bot)
        has_drawings = any(
            strip.intersects(fitz.Rect(d["rect"]))
            for d in page.get_drawings()
        )
        if not has_drawings:
            continue
        # Use the x-range of the nearest above-image as a hint, else full width
        if above:
            prev_r = fitz.Rect(above[-1]["bbox"])
            x0 = prev_r.x0
            x1 = min(prev_r.x1, pw * 0.75)
        else:
            x0, x1 = 0, pw * 0.75
        orphan_bbox = [x0, strip_top, x1, strip_bot]
        claimed.append({"type": "image", "bbox": orphan_bbox, "caption": cap_text})
        claimed_images.append({"type": "image", "bbox": orphan_bbox, "caption": cap_text})

    # Remove tables whose bbox is substantially contained within a claimed figure.
    # Such tables are already visible inside the figure PNG — emitting them as
    # separate table rows creates visual duplicates in the DOCX.
    claimed_figs = [r for r in claimed if r["type"] == "image"]
    if claimed_figs:
        def _inside_figure(tbl_r):
            tr = fitz.Rect(tbl_r["bbox"])
            ta = max(_rect_area(tr), 1)
            return any(
                _intersect_area(tr, fitz.Rect(f["bbox"])) / ta > 0.60
                for f in claimed_figs
            )
        claimed = [r for r in claimed if r["type"] != "table" or not _inside_figure(r)]

    return [r for r in claimed if r["type"] in ("image", "table")]
