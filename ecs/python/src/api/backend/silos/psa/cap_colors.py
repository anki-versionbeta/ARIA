"""Cap-colour palette + normalization (UI-agnostic, importable — see docs/Cap_Color_Interface_Design.md).

Two jobs:
  1. Load the SUPPLY-side palette (`cap_palette.csv`: Datwyler 40 + West colours) — the colours a
     product CAN choose from. `palette()` / `available_for()`.
  2. NORMALISE the DEMAND-side free-text cap-colour strings that live in the Smartsheet-fed
     `cap_color` table (e.g. "Blue 6016", "Magenta 2063C (L9320)", "SCS - Grey", "TBD") to a canonical
     colour + representative hex, so "already-utilised" colours can be compared against the palette and
     ranked by perceptual distance. `normalize()`.

Design choices (from the two supplier catalogs):
  - Datwyler (`6xxx`) and West (`L####` / Pantone) coding schemes are INDEPENDENT, and West's stored
    codes (`L3773`) differ from the catalog wheel codes (`3773`). So matching is: CODE first (with `L`
    prefix / Pantone `C` suffix stripped), CANONICAL COLOUR NAME second. Never key logic on raw code.
  - hex values in the CSV are APPROXIMATE representative sRGB (pending measured swatch values); good
    enough for relative ΔE ranking, not for print matching.

This module deliberately has NO database dependency so it survives a front-end migration and is trivially
reusable. WHERE the palette rows come from — a shipped CSV asset plus an override object — is
`palette.py`'s business, not this module's. Never raises on bad input; returns an "unknown" result.
"""
import re

from . import palette as palette_source

# Canonical colour keywords → (canonical_color, hue_group, fallback_hex). Ordered MOST-SPECIFIC first
# (multi-word before single-word) so "dark blue" wins over "blue". Fallback hex is used only when the
# text names a colour we can't tie to a palette row (e.g. a custom colour like magenta).
_COLOR_WORDS = [
    ("light blue", "Blue", "blue", "#4FA9DD"),
    ("dark blue", "Blue", "blue", "#1B2A5B"),
    ("light green", "Lime", "green", "#8CC63F"),
    ("dark green", "Green", "green", "#1C5C4E"),
    ("dark grey", "Grey", "neutral", "#4B4F54"),
    ("dark gray", "Grey", "neutral", "#4B4F54"),
    ("mist grey", "Grey", "neutral", "#C4CBD0"),
    ("dark red", "Maroon", "red", "#8E1B2A"),
    ("off-white", "White", "neutral", "#F2F0EA"),
    ("transparent", "Transparent", "neutral", ""),
    ("colorless", "Transparent", "neutral", ""),
    ("colourless", "Transparent", "neutral", ""),
    ("clear", "Clear", "neutral", "#D9DCDF"),
    ("natural", "Natural", "neutral", "#E8E4D8"),
    ("magenta", "Magenta", "pink", "#C63A78"),
    ("burgundy", "Burgundy", "red", "#7A1E33"),
    ("maroon", "Maroon", "red", "#8C1D2C"),
    ("crimson", "Red", "red", "#D81E27"),
    ("turquoise", "Turquoise", "teal", "#3FC5B7"),
    ("teal", "Teal", "teal", "#158E9B"),
    ("aqua", "Aqua", "teal", "#3FC5C0"),
    ("lavender", "Lavender", "purple", "#B7A9D6"),
    ("lilac", "Lilac", "purple", "#C9AEDA"),
    ("violet", "Violet", "purple", "#6E4A9E"),
    ("purple", "Purple", "purple", "#7B4EA6"),
    ("plum", "Plum", "purple", "#5E2544"),
    ("pink", "Pink", "pink", "#EE7BA0"),
    ("orange", "Orange", "orange", "#F0A020"),
    ("yellow", "Yellow", "yellow", "#F7D417"),
    ("gold", "Gold", "neutral", "#C9B37E"),
    ("ivory", "Ivory", "neutral", "#EDE6D0"),
    ("lime", "Lime", "green", "#8CC63F"),
    ("olive", "Olive", "green", "#6B7A2E"),
    ("green", "Green", "green", "#159A55"),
    ("blue", "Blue", "blue", "#1560A8"),
    ("brown", "Brown", "brown", "#7A4A22"),
    ("grey", "Grey", "neutral", "#808A90"),
    ("gray", "Grey", "neutral", "#808A90"),
    ("silver", "Grey", "neutral", "#C4CBD0"),
    ("white", "White", "neutral", "#FFFFFF"),
    ("black", "Black", "neutral", "#1A1A1A"),
    ("red", "Red", "red", "#D81E27"),
]

# Colour codes: Datwyler 6xxx / alu 00x, West 4-digit / L#### / Pantone ####C.
_CODE_RE = re.compile(r"\bL?\d{4}C?\b|\b00[1-5]\b", re.IGNORECASE)
_UNKNOWN_TOKENS = REDACTED

_PALETTE_CACHE = None


def _norm_code(code):
    """Canonicalise a colour code for cross-scheme matching: upper, strip a leading L and trailing C."""
    c = (code or "").strip().upper()
    if c.startswith("L"):
        c = c[1:]
    if c.endswith("C"):
        c = c[:-1]
    return c


def _enrich(row):
    """Add the derived fields every caller expects on a palette row."""
    r = dict(row)
    r["off_the_shelf"] = str(r.get("off_the_shelf", "1")).strip() == "1"
    r["sizes"] = [s for s in re.split(r"[;,]", r.get("sizes_mm") or "") if s.strip()]
    r["_code_norm"] = _norm_code(r.get("vendor_code"))
    return r


def palette(vendor=None, component="pp_disc", off_the_shelf_only=False):
    """Palette rows (list of dicts), optionally filtered by vendor / component / off-the-shelf.

    The effective palette is the shipped default with any override applied. `palette.load()` caches on
    the override's bytes and the enriched rows are cached on that result, so this stays cheap in a loop
    (`normalize()` calls it per comparator).
    """
    global _PALETTE_CACHE
    state = palette_source.load()
    if _PALETTE_CACHE is None or _PALETTE_CACHE[0] is not state.rows:
        _PALETTE_CACHE = (state.rows, [_enrich(r) for r in state.rows])
    rows = _PALETTE_CACHE[1]
    if vendor:
        rows = [r for r in rows if r["vendor"].upper() == vendor.upper()]
    if component:
        rows = [r for r in rows if r.get("component") == component]
    if off_the_shelf_only:
        rows = [r for r in rows if r["off_the_shelf"]]
    return rows


def available_for(vendor, diameter_mm, component="pp_disc", off_the_shelf_only=True):
    """Palette rows available in a given vial-seal diameter (e.g. '20').

    `vendor=None` spans EVERY supplier, which is what the recommendation engine asks for: the best cap
    colour is chosen across both catalogues and the supplier is an attribute of the answer, not an input
    to it (decided 2026-08-21).

    `off_the_shelf_only=False` keeps non-off-the-shelf rows in the result so a caller can rank them
    BELOW the off-the-shelf ones rather than hiding them. Every shipped row is currently off-the-shelf,
    so today this changes nothing — it starts to matter when West's per-design availability matrix is
    transcribed, or when someone adds a custom colour through the palette editor.
    """
    d = re.sub(r"[^\d]", "", str(diameter_mm or ""))
    out = []
    for r in palette(vendor=vendor, component=component, off_the_shelf_only=off_the_shelf_only):
        if not d or not r["sizes"] or d in r["sizes"]:
            out.append(r)
    return out


# --------------------------------------------------- palette editing (the "cap colour database")
# add_color / remove_color record an entry in the palette OVERRIDE (see palette.py). They change only
# the supply-side list a product may choose FROM — never the `cap_color` table — so a product's
# currently-assigned cap is untouched, and the shipped assets/cap_palette.csv is never written to.
def add_color(vendor, vendor_color_name, canonical_color, hex="", vendor_code="",
              hue_group="", sizes_mm="13;20", finish="", off_the_shelf=1, component="pp_disc", notes=""):
    """Add (or replace) one colour. Keyed on (vendor, vendor_color_name) — a re-add updates it.

    Returns (ok, message); never raises on bad input.
    """
    try:
        vendor = (vendor or "").strip()
        name = (vendor_color_name or "").strip()
        if not vendor or not name:
            return False, "Vendor and colour name are required."
        if not (canonical_color or "").strip():
            return False, "Canonical colour is required (e.g. Blue, Green, Grey)."
        palette_source.record_add({
            "vendor": vendor, "vendor_color_name": name, "vendor_code": (vendor_code or "").strip(),
            "canonical_color": canonical_color.strip(), "hue_group": (hue_group or "").strip(),
            "hex": (hex or "").strip(), "sizes_mm": (sizes_mm or "").strip(),
            "finish": (finish or "").strip(),
            "off_the_shelf": "1" if str(off_the_shelf).strip() in ("1", "True", "true", "yes", "on") else "0",
            "component": (component or "pp_disc").strip(), "notes": (notes or "").strip()})
        return True, f"Added {vendor} '{name}' to the palette."
    except Exception as e:
        return False, f"Add failed: {e}"


def remove_color(vendor, vendor_color_name):
    """Remove a colour from the palette by (vendor, vendor_color_name). Returns (ok, message)."""
    try:
        vendor = (vendor or "").strip()
        name = (vendor_color_name or "").strip()
        if not vendor or not name:
            return False, "Pick a colour to remove."
        if not palette_source.contains(vendor, name):
            return False, f"No palette colour '{name}' found for {vendor}."
        palette_source.record_remove(vendor, name)
        return True, f"Removed {vendor} '{name}' from the palette."
    except Exception as e:
        return False, f"Remove failed: {e}"


def _match_color_word(text_lc):
    for kw, canon, hue, hexv in _COLOR_WORDS:
        if kw in text_lc:
            return canon, hue, hexv
    return None, None, None


def normalize(text, vendor_hint=None):
    """Normalise one free-text cap-colour string to a canonical colour + representative hex.

    Returns a dict (never raises):
      raw            the input string
      canonical_color  e.g. 'Blue', 'Magenta', or None if unknown
      hue_group      coarse perceptual bucket, or None
      hex            representative sRGB hex (from a matched palette row, else a fallback), or ''
      vendor         matched palette vendor, or vendor_hint, or None
      vendor_code    the code token found in the text (raw), or None
      matched_code   True if the code tied to a palette row
      off_the_shelf  True/False if determinable (palette hit or known-custom), else None
      is_custom      True when it's a recognised colour that is NOT an off-the-shelf palette colour
                     (e.g. the BoNT/E 'Magenta 2063C' custom cap)
      is_unknown     True for TBD/TBC/blank/uninterpretable
      confidence     'code' | 'name' | 'none'
    """
    raw = (text or "").strip()
    res = {"raw": raw, "canonical_color": None, "hue_group": None, "hex": "",
           "vendor": vendor_hint, "vendor_code": None, "matched_code": False,
           "off_the_shelf": None, "is_custom": False, "is_unknown": False, "confidence": "none"}
    if raw.upper() in _UNKNOWN_TOKENS:
        res["is_unknown"] = True
        return res

    text_lc = raw.lower()

    # 1) code match (authoritative) — try every code token in the string against the palette.
    # Prefer a row of the hinted vendor; else accept a cross-vendor code hit (a Datwyler 6xxx code is
    # authoritative even if the Smartsheet mis-tagged the vendor as West — a real data inconsistency).
    codes = _CODE_RE.findall(raw)
    if codes:
        res["vendor_code"] = codes[0]
        wanted = {_norm_code(c) for c in codes}
        hits = [r for r in palette(component=None) if r["_code_norm"] and r["_code_norm"] in wanted]
        if hits:
            hinted = [r for r in hits if vendor_hint and r["vendor"].upper() == vendor_hint.upper()]
            r = (hinted or hits)[0]
            res.update({"canonical_color": r["canonical_color"], "hue_group": r["hue_group"],
                        "hex": r["hex"], "vendor": r["vendor"], "matched_code": True,
                        "off_the_shelf": r["off_the_shelf"],
                        "confidence": "code" if hinted or not vendor_hint else "code-xvendor"})
            return res

    # 2) colour-word match (canonical name; hex from fallback table)
    canon, hue, hexv = _match_color_word(text_lc)
    if canon:
        res.update({"canonical_color": canon, "hue_group": hue, "hex": hexv, "confidence": "name"})
        # off-the-shelf if this canonical colour exists in the standard palette; else treat as custom
        palette_canon = {r["canonical_color"].upper() for r in palette(component="pp_disc", off_the_shelf_only=True)}
        in_palette = canon.upper() in palette_canon
        res["off_the_shelf"] = in_palette
        # a named colour with a code that did NOT tie to the palette (e.g. Pantone 2063C) => custom
        res["is_custom"] = (not in_palette) or bool(codes and not res["matched_code"])
        return res

    # 3) uninterpretable
    res["is_unknown"] = True
    return res


# --------------------------------------------------- perceptual distance (CIE76 ΔE) for ranking
def _hex_to_rgb(hexv):
    h = (hexv or "").lstrip("#")
    if len(h) != 6:
        return None
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _srgb_to_lin(c):
    c /= 255.0
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def _rgb_to_lab(rgb):
    r, g, b = (_srgb_to_lin(v) for v in rgb)
    # linear sRGB -> XYZ (D65)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    # normalise by D65 white
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116.0
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(hex1, hex2):
    """CIE76 ΔE between two hex colours (rough perceptual distance; ~2.3 = just-noticeable).
    Returns None if either colour has no solid hex (e.g. transparent). CIEDE2000 is a later refinement."""
    rgb1, rgb2 = _hex_to_rgb(hex1), _hex_to_rgb(hex2)
    if not rgb1 or not rgb2:
        return None
    l1, a1, b1 = _rgb_to_lab(rgb1)
    l2, a2, b2 = _rgb_to_lab(rgb2)
    return ((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2) ** 0.5


def _demo():
    """Print the palette summary + normalise a sample of real DB free-text values."""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # avoid cp1252 crash on the ΔE glyph (Windows)
    except Exception:
        pass
    print(f"Palette: {len(palette(component=None))} rows total; "
          f"Datwyler pp_disc={len(palette('Datwyler'))}, West pp_disc={len(palette('West'))}")
    samples = ["Blue 6016", "Blue (1280)", "Blue", "Magenta 2063C (L9320)", "Orange L3773",
               "Light Blue/Teal 6066", "Crimp cap only (Matte Turquoise)", "Dark Grey",
               "SCS - Grey", "SR - Red", "White to off-white", "TBD", ""]
    print("\nNormalisation of sample free-text values:")
    for s in samples:
        r = normalize(s)
        tag = ("code" if r["matched_code"] else r["confidence"])
        flag = " [CUSTOM]" if r["is_custom"] else (" [unknown]" if r["is_unknown"] else "")
        print(f"  {s!r:42} -> {str(r['canonical_color']):12} {r['hex']:8} "
              f"vendor={str(r['vendor']):9} via={tag}{flag}")
    # a quick ΔE sanity check
    print(f"\nΔE Blue6018 vs Blue6034 (close): {delta_e('#1560A8', '#0E4C9A'):.1f}")
    print(f"ΔE Blue6018 vs Orange6063 (far): {delta_e('#1560A8', '#F0A020'):.1f}")


if __name__ == "__main__":
    _demo()
