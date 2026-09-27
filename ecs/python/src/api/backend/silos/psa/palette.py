"""The cap-colour palette: a shipped default asset plus an override object.

This is ARIA's `silos/bop/prompts.py` pattern applied to the palette. The shipped
`assets/cap_palette.csv` — 65 rows transcribed by hand from the Datwyler and West catalogues — is the
default, lives in git, and is genuinely read-only. Edits made from the UI are recorded as an override
object in the object store, read back on top of the default, and can be discarded in one call.

What that replaces, and why:

  The palette used to be an editable CSV whose path was chosen at import time by `cap_colors`, with a
  runtime fallback to `~/.psa/cap_palette.csv` when the file could not be written. Three problems, all
  of them real:
    * the path global was REBOUND on a failed write, so a single locked file silently diverted reads to
      a stale copy in the user profile (the palette-edit bug fixed on 2026-08-20);
    * `~/.psa/...` is exactly the local-disk persistence that `storage/base.py` warns about — it
      survives on a laptop and vanishes when a container is replaced;
    * a deployment where the code directory is read-only had no correct place to put an edit.
  A shipped default plus an override object has none of them, and it makes "what did we change?" and
  "put it back" first-class operations instead of a diff against a file nobody can see.

The override is a DELTA, not a snapshot — a deliberate divergence from `bop`, which replaces a whole
prompt. A prompt is one opaque blob; a palette is a set of rows with a stable key
(`vendor`, `vendor_color_name`). Storing a snapshot would freeze the catalogue for anyone who ever added
a single colour, and the shipped rows are still expected to improve: the hex values are explicitly
approximations pending measured swatches, and West's per-design availability matrix is still outstanding.
With a delta, those upstream improvements keep reaching every user, and only the rows they actually
touched are pinned.

    {"version": 1,
     "added":   [{"vendor": "Datwyler", "vendor_color_name": "Sky 6099", ...}],
     "removed": [["DATWYLER", "GREEN 6007"]]}

The override is GLOBAL: one person's edit changes what every assessor is recommended. That is `bop`'s
behaviour too, deliberately ("the override is global — one person's edit changes everyone's output"),
and ARIA keeps a per-user override table ready if it is ever wanted. Whether a palette edit should be
per-user is a governance question for the business, not a technical one — flagged, not decided here.

Reads are split the way ARIA splits them: the default comes through `asset_store` (the port ARIA fills
with `ctx.assets`) and the override through the object store. In ARIA both collapse to `ctx.assets`,
because bundled assets are deployed to the same asset prefix the override is written to.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
from dataclasses import dataclass, field

from . import asset_store
from .location import STORAGE_PREFIX
from .storage import StorageError, asset_key, get_object_store

logger = logging.getLogger(__name__)

DEFAULT_ASSET = "cap_palette.csv"
"""The shipped palette, in `psa/assets/`. Never written to."""

OVERRIDE_RELATIVE = "palette/cap_palette.override.json"
OVERRIDE_VERSION = 1

FIELDS = ["vendor", "vendor_color_name", "vendor_code", "canonical_color", "hue_group",
          "hex", "sizes_mm", "finish", "off_the_shelf", "component", "notes"]
"""CSV column order. Must match the shipped header exactly, `notes` included."""


def override_key() -> str:
    return asset_key(STORAGE_PREFIX, OVERRIDE_RELATIVE)


def _key_of(row: dict) -> tuple[str, str]:
    """The stable identity of a palette row: (vendor, colour name), case-insensitive."""
    return ((row.get("vendor") or "").strip().upper(),
            (row.get("vendor_color_name") or "").strip().upper())


@dataclass(frozen=True)
class PaletteState:
    """The effective palette and where it came from."""

    rows: list[dict] = field(default_factory=list)
    is_override: bool = False
    added: int = 0
    removed: int = 0
    override_invalid: bool = False
    """True when an override object exists but could not be used, so the default is in force."""


# ------------------------------------------------------------------ default
def default_rows() -> list[dict]:
    """The shipped palette as raw CSV-shaped dicts. Cached by `asset_store`."""
    text = asset_store.read_text(DEFAULT_ASSET)
    return list(csv.DictReader(io.StringIO(text)))


# ------------------------------------------------------------------ override
def _read_override_bytes() -> bytes | None:
    """The raw override object, or None when there isn't one.

    Read on every call rather than cached — same reason `bop/prompts.py` passes `cache=False` for the
    override: an edit must be visible immediately, and the object is a few hundred bytes.
    """
    store = get_object_store()
    key = override_key()
    try:
        if not store.exists(key):
            return None
        with store.open(key) as fh:
            return fh.read()
    except StorageError as exc:
        logger.warning("cap palette: override unreadable (%s) — using the shipped default", exc)
        return None


def _parse_override(raw: bytes) -> dict | None:
    """Validate an override document. Returns None when it is unusable.

    A malformed override must never stop a recommendation — the same rule `bop` states for its prompts.
    The caller falls back to the shipped palette and says so.
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("cap palette: override is not valid JSON (%s) — using the shipped default", exc)
        return None
    if not isinstance(doc, dict):
        logger.warning("cap palette: override is not an object — using the shipped default")
        return None
    added = doc.get("added") or []
    removed = doc.get("removed") or []
    if not isinstance(added, list) or not isinstance(removed, list):
        logger.warning("cap palette: override 'added'/'removed' are not lists — using the default")
        return None
    clean_added = [r for r in added if isinstance(r, dict) and _key_of(r) != ("", "")]
    clean_removed = []
    for entry in removed:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            clean_removed.append(((entry[0] or "").strip().upper(), (entry[1] or "").strip().upper()))
    return {"version": doc.get("version", OVERRIDE_VERSION),
            "added": clean_added, "removed": clean_removed}


def _write_override(doc: dict) -> None:
    body = json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")
    get_object_store().put(override_key(), body)


# ------------------------------------------------------------------ effective palette
_LOCK = threading.Lock()
_CACHE: tuple[bytes | None, PaletteState] | None = None


def load() -> PaletteState:
    """The effective palette. Never raises.

    Cached on the override's exact bytes, so an edit (from this process or another) invalidates it
    without any explicit invalidation call, while a run of read requests costs one small file read each.
    """
    global _CACHE
    raw = _read_override_bytes()
    with _LOCK:
        if _CACHE is not None and _CACHE[0] == raw:
            return _CACHE[1]

    rows = [dict(r) for r in default_rows()]
    state = PaletteState(rows=rows)
    if raw is not None:
        doc = _parse_override(raw)
        if doc is None:
            state = PaletteState(rows=rows, override_invalid=True)
        else:
            state = PaletteState(rows=_merge(rows, doc), is_override=bool(doc["added"] or doc["removed"]),
                                 added=len(doc["added"]), removed=len(doc["removed"]))

    with _LOCK:
        _CACHE = (raw, state)
    return state


def _merge(rows: list[dict], doc: dict) -> list[dict]:
    """Apply removals, then upsert additions.

    An addition that matches a shipped row replaces it IN PLACE. Position matters: when no comparator
    cap is on file every ΔE is None, and the recommendation list is then presented in palette order.
    """
    removed = set(doc["removed"])
    out = [r for r in rows if _key_of(r) not in removed]
    by_key = {_key_of(r): i for i, r in enumerate(out)}
    for extra in doc["added"]:
        row = {k: (extra.get(k) or "") for k in FIELDS}
        key = _key_of(row)
        if key in by_key:
            out[by_key[key]] = row
        else:
            by_key[key] = len(out)
            out.append(row)
    return out


# ------------------------------------------------------------------ edits
def record_add(row: dict) -> None:
    """Add or replace one colour. Keyed on (vendor, colour name), so a re-add updates.

    Lives here rather than in the router (where `bop` keeps its two-line prompt write) because an edit
    is a read-modify-write of the delta, and that belongs next to the merge rule it has to agree with.
    """
    key = _key_of(row)
    doc = _current_doc()
    doc["added"] = [r for r in doc["added"] if _key_of(r) != key]
    doc["added"].append({k: (row.get(k) or "") for k in FIELDS})
    # An explicit re-add cancels a previous removal of the same colour.
    doc["removed"] = [k for k in doc["removed"] if tuple(k) != key]
    _write_override(doc)


def record_remove(vendor: str, vendor_color_name: str) -> None:
    """Remove one colour: drop it from the additions and record it as removed."""
    key = _key_of({"vendor": vendor, "vendor_color_name": vendor_color_name})
    doc = _current_doc()
    doc["added"] = [r for r in doc["added"] if _key_of(r) != key]
    if key not in {tuple(k) for k in doc["removed"]}:
        doc["removed"].append(list(key))
    _write_override(doc)


def _current_doc() -> dict:
    """The override document to modify — the stored one, or an empty delta."""
    raw = _read_override_bytes()
    doc = _parse_override(raw) if raw is not None else None
    if doc is None:
        # An unusable override is replaced rather than extended: appending to something we could not
        # parse would silently keep whatever was wrong with it.
        return {"version": OVERRIDE_VERSION, "added": [], "removed": []}
    doc["removed"] = [list(k) for k in doc["removed"]]
    return doc


def contains(vendor: str, vendor_color_name: str) -> bool:
    """True when the effective palette holds that colour (any component)."""
    key = _key_of({"vendor": vendor, "vendor_color_name": vendor_color_name})
    return any(_key_of(r) == key for r in load().rows)
