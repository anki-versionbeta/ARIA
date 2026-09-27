"""Data-only queries behind the cap-colour screens (UI-agnostic, dict-returning).

These two tables — the products using a given palette colour, and the product count per site — were
originally written inline in the Dash screens, with the SQL tangled into the markup, so neither result
was reachable from another front-end. They were lifted out UNCHANGED, which is why the numbers still
match the earlier screenshots exactly; the Dash apps themselves have since been removed.

Follows the convention of the rest of the cap layer: no front-end import, returns plain dicts, and
never raises on a data gap (an empty list instead).
"""
from __future__ import annotations

import os
import sqlite3
import sys

from . import cap_colors as cc
from . import scope

# cap_color.vendor_id → vendor name, used only as a normalisation hint for free-text cap values.
# The ids come from the `vendor` table seeded by the ingest.
VENDOR_BY_ID = {1: "Datwyler", 2: "West", 3: "West"}


def _palette_row(vendor, color_name):
    """The off-the-shelf palette row for one vendor colour name, or None."""
    return next((r for r in cc.palette(vendor, component="pp_disc", off_the_shelf_only=True)
                 if r["vendor_color_name"] == color_name), None)


def products_by_color(con, vendor, color_name):
    """In-scope products whose cap is THIS exact palette colour.

    Matching is by CODE, not by hue: when the palette colour carries a vendor code
    (e.g. 'Blue 6043' → 6043) only products whose stored code or normalised code equals it are
    returned — NOT every shade of the canonical hue. Uncoded palette colours fall back to a canonical
    colour-name match.

    Returns [{product, contact, strength, vial_size, form, mfr_sites, cap_color_name}], de-duplicated
    on (program, vial, form, cap name) as the screen does.
    """
    prow = _palette_row(vendor, color_name)
    palette_code = cc._norm_code(prow["vendor_code"]) if (prow and prow.get("vendor_code")) else ""
    canonical = (prow["canonical_color"] if prow
                 else cc.normalize(color_name).get("canonical_color"))
    try:
        rows = con.execute(
            "SELECT p.program_no, p.program_name, p.contact, p.strength, p.vial_container_size, "
            "p.form, cc.cap_color_name, cc.color_code, cc.vendor_id, "
            "(SELECT GROUP_CONCAT(s.site_code, ', ') FROM product_site ps JOIN site s "
            " ON s.site_id=ps.site_id WHERE ps.product_id=p.product_id AND ps.role='mfr') "
            "AS mfr_sites FROM product p JOIN cap_color cc "
            f"ON cc.product_id=p.product_id WHERE {scope.scope_sql('p')}").fetchall()
    except Exception:
        return []

    out, seen = [], set()
    for prog, name, contact, strength, vial, form, capname, capcode, vid, mfr in rows:
        norm = cc.normalize(capname, vendor_hint=VENDOR_BY_ID.get(vid))
        prod_code = cc._norm_code(capcode) if capcode else ""
        norm_code = cc._norm_code(norm.get("vendor_code")) if norm.get("vendor_code") else ""
        if palette_code:
            if palette_code not in (prod_code, norm_code):
                continue
        elif (norm["canonical_color"] or "").upper() != (canonical or "").upper():
            continue
        key = (prog, vial, form, capname)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "product": f"{prog} ({name})" if (name and prog) else (name or prog or "?"),
            "contact": contact or "",
            "strength": strength or "",
            "vial_size": vial or "",
            "form": form or "",
            "mfr_sites": mfr or "",
            "cap_color_name": capname or "",
        })
    return out


def site_product_counts(con):
    """In-scope product count per MANUFACTURING site, busiest first.

    Returns [{site_code, n_products}] — the numbers behind the bar chart.
    """
    try:
        rows = con.execute(
            "SELECT s.site_code, COUNT(DISTINCT ps.product_id) FROM product_site ps "
            "JOIN site s ON s.site_id=ps.site_id JOIN product p ON p.product_id=ps.product_id "
            f"WHERE ps.role='mfr' AND {scope.scope_sql('p')} GROUP BY s.site_code "
            "ORDER BY 2 DESC").fetchall()
    except Exception:
        return []
    return [{"site_code": code, "n_products": n} for code, n in rows]


def _demo():
    """Print both tables for a spot-check:  python -m da_silos.psa.cap_queries [VENDOR] [COLOUR]"""
    from . import paths
    db = paths.db_path()
    vendor = sys.argv[1] if len(sys.argv) > 1 else "Datwyler"
    colour = sys.argv[2] if len(sys.argv) > 2 else None
    if not (os.path.exists(db) and os.path.getsize(db) > 0):
        # Connecting would CREATE a 0-byte psa.db, and the smoke tests gate on the file merely
        # existing — so they would fail instead of skip.
        print(f"psa.db not built ({db}) — run a PSA job so its fetch stage captures a snapshot")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        print("Products per manufacturing site:")
        for r in site_product_counts(con):
            print(f"  {r['site_code']:30} {r['n_products']}")
        if colour:
            print(f"\nProducts using {vendor} {colour!r}:")
            for r in products_by_color(con, vendor, colour):
                print(f"  {r['product']:34} vial={r['vial_size']:18} cap={r['cap_color_name']}")
    finally:
        con.close()


if __name__ == "__main__":
    _demo()
