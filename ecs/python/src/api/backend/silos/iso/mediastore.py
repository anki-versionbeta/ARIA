"""Naming, writing and reading the extracted media. Ported from backend.py:2154-2238.

Media filenames are derived from captions, which is why `_sanitize_filename` exists —
the real corpus contains files like
`Figure 1 — Relationship of terms used to describe information supplied by the
manufacturer.png`.

The one signature change in this module is the seam: ISO threaded a `session_id` down
purely to derive `<memry>/<session_id>/`, and passed an `s3_rel_key` to opt into a
durable copy. Both become the workspace — `_session_media_dir(ws)` and
`_write_media(..., ws=ws)`. The local write stays exactly as it was, because downstream
code passes file *paths* around and `_media_as_bytes` reads them back; dropping it would
change the values flowing through the figures/tables/formulas structures.

The four region-detection constants at 2176-2179 live here because that is where the
source declares them, immediately before `_sanitize_filename`. `geometry.py` and
`prescan.py` import them rather than keeping their own copies.
"""

from __future__ import annotations

import io
import logging
import os
import re

from PIL import Image as PILImage

logger = logging.getLogger(__name__)


def _vstack_images(img_bytes_list):
    """Vertically concatenate multiple PNG byte-strings into one image."""
    if len(img_bytes_list) == 1:
        return img_bytes_list[0]
    images = [PILImage.open(io.BytesIO(b)).convert('RGB') for b in img_bytes_list]
    max_w  = max(im.width  for im in images)
    total_h = sum(im.height for im in images)
    combined = PILImage.new('RGB', (max_w, total_h), (255, 255, 255))
    y = 0
    for im in images:
        combined.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    combined.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# Region detection (ported from img_ext/extImg.py) — used by normal path
# ─────────────────────────────────────────────────────────────────────────────

_TWO_LINE_GAP = 26
_PROX         = 60
_CAP_PROX     = 80
_FILENAME_MAX = 120


def _sanitize_filename(text, max_len=_FILENAME_MAX):
    """Strip filesystem-unsafe chars, collapse whitespace, cap length."""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', text or '')
    text = re.sub(r'\s+', ' ', text).strip().strip('. ')
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or 'unnamed'


def _session_media_dir(ws):
    """Return (and create) the run's local media directory for extracted images.

    Was `<memry>/<session_id>/`; the workspace now owns that directory and creates it on
    first use. Returns None when there is no workspace, which is the in-memory mode the
    3-tuple return path and the tests use.
    """
    if not ws:
        return None
    return ws.media_dir


def _write_media(media_dir, filename, png_bytes, ws=None):
    """Write *png_bytes* to <media_dir>/<filename>, return absolute path.
    If a workspace is given, also store a durable copy."""
    path = os.path.join(media_dir, filename)
    with open(path, 'wb') as f:
        f.write(png_bytes)
    if ws is not None:
        try:
            ws.put_media(filename, png_bytes)
        except Exception as _e:
            # As in the source: losing the durable copy must not fail the run, because
            # the local copy is what this run reads back.
            logger.info("[prescan] durable copy skipped for %s: %s", filename, _e)
    return path


def _media_as_bytes(v):
    """Accept either bytes (no session) or filepath (persisted); return bytes."""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    with open(v, 'rb') as f:
        return f.read()


def _table_img_list(v):
    """Return a list of PNG bytes for a table entry.

    Handles the new per-page list format (list[bytes | str]) produced by
    prescan_media after Fix B, as well as the legacy single-image format
    (bytes | str) so callers work for both paths.
    """
    if isinstance(v, list):
        return [_media_as_bytes(item) for item in v]
    return [_media_as_bytes(v)]


_CAP_LABEL_RE = re.compile(
    r"^(Fig(?:ure)?\.?|Scheme|Plate|Chart|Diagram|Exhibit|Illustration|Graph)\s*\d",
    re.I,
)
