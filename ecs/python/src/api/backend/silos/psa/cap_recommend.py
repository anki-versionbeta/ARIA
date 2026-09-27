"""Cap-colour recommendation engine (UI-agnostic — see docs/Cap_Color_Interface_Design.md).

The INVERSE of risk.py: instead of detecting look-alike collisions between existing products, it
recommends the off-the-shelf cap colours that AVOID one for a new presentation.

Governing rule (strategy deck slide 20, POC = Evan White):
    "Avoid using the same Cap Colour & Vial Size [among co-located products]. Prefer off-the-shelf
     colours; avoid custom colours (cf. the BoNT/E magenta cap)."

Algorithm:
  1. Resolve the subject's MANUFACTURING site(s) (risk._subject_sites — role='mfr'; packaging sites are
     out of scope for the report and for cap co-location, per the 2026-07-23 decision).
  2. Gather the IN-SCOPE (scope.py) caps of OTHER products manufactured at those sites AND in the SAME
     STATE (product.modality: Biologic / Liquid / Frozen Liquid / Lyo Powder / Lyo-Cake — different states
     are visually distinguishable, so only same-state products can clash; COMPARE_BY_STATE / STATE_GROUPS
     tune this) — the "already-utilised" set — and normalise each to a canonical colour + hex, then GRADE
     each one for mix-up risk against the subject (same vial size + similar shade ⇒ High; see
     _grade_taken) so a front-end can show the highest similarity risks first.
  3. Candidate set = the palette of EVERY supplier (cap_colors.available_for(vendor=None)) — see
     "Suppliers" below.
  4. HARD-EXCLUDE any candidate whose canonical colour is already used AT THE SUBJECT'S VIAL SIZE at a
     shared mfr site (the colour×vial-size uniqueness rule). Colours used only at a different vial size
     are still allowed, but ranked lower if perceptually close.
  5. RANK survivors OFF-THE-SHELF FIRST, then by perceptual distinctness = min ΔE (CIELAB) to ANY
     co-located cap (larger ⇒ more distinct ⇒ safer); tiebreak keeps a stable order (palette order, which
     `palette.py` preserves precisely so it can serve as the no-ΔE-signal fallback). Weights/threshold are
     pluggable for the SME model.

**Suppliers (decided 2026-08-21).** The recommendation spans **both** catalogues and names the supplier of
each suggestion, because at assessment time the cap is often not yet tooled or contracted — so "which
supplier" is an OUTPUT of the colour decision, not a precondition for it. `vendor=None` (the default)
therefore means *every* supplier; passing a vendor still restricts, which the CLI uses and a future
"I know I'm buying Datwyler" control could. The screen's Datwyler/West toggle now only chooses which
catalogue the swatch GRID displays; it no longer constrains the recommendation.

Two consequences worth knowing, both accepted deliberately:

* Candidates roughly grow 40 → 60, and **12 canonical colours exist in both catalogues** (7 "Blue" rows
  across the two, 5 "Orange", 5 "Brown"). Ranking is strictly per supplier row, so the top of the list can
  legitimately show one hue twice — Datwyler's blue and West's blue — which is what lets an assessor
  compare two suppliers' takes on the same colour. It also means top-N covers fewer DISTINCT colours than
  it used to; `DEFAULT_TOP_N` was raised to compensate.
* Cross-supplier ΔE is weaker evidence than within-supplier ΔE, because the palette hex values are
  transcribed approximations pending measured swatches. Within one catalogue the errors are at least
  consistent; across two, a 2-3 ΔE margin between a Datwyler and a West colour may be measuring our
  transcription rather than the colours. The engine therefore reports the margin and never hides it.

Dict-returning and exception-tolerant, mirroring risk.assess(), so any front-end can consume it
unchanged. This is a PROVISIONAL, explainable recommendation — a human (SME) approves the final colour
(per the MOM).
"""
import re
import sqlite3
import sys

from . import cap_colors as cc
from . import paths, risk, scope

# Ranking knobs — pluggable for the SME model (cf. risk.GRADE_THRESHOLDS).
# ΔE below this to an existing co-located cap ⇒ "too close" (a soft warning, not a hard block unless the
# colour×size combo is taken). CIE76 ΔE < ~10 reads as the same colour family to the eye.
CLOSE_DELTA_E = 12.0

ALL_VENDORS = None
"""What `vendor=` means when the recommendation should span every supplier — the default since 2026-08-21."""

GRID_DEFAULT_VENDOR = "Datwyler"
"""Deck slide 14: which catalogue the swatch GRID shows first. No longer constrains the recommendation."""

DEFAULT_TOP_N = 10
"""Was 6, when candidates came from one catalogue. Both catalogues together offer 12 colours twice over, so
strict per-supplier ranking spends rows on repeated hues; 6 could show as few as 3 distinct colours. Raised
so the list still surfaces a usable spread of genuinely different options. A display knob — change freely."""

# Similarity-risk grading of the ALREADY-UTILISED caps (the "highest similarity risks" the UI ranks).
# Two things make a co-located presentation confusable with the subject: the container looks the same
# (SAME VIAL SIZE) and the cap looks the same (SIMILAR SHADE — ΔE to the subject's own cap below
# SAME_SHADE_DELTA_E). Both ⇒ High, either one ⇒ Medium, neither ⇒ Low. Provisional and pluggable, exactly
# like risk.GRADE_THRESHOLDS: the SME Risk Index is expected to replace the RULE, not these fields.
SAME_SHADE_DELTA_E = CLOSE_DELTA_E
RISK_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# Compare cap colours only among products in the SAME STATE (the Smartsheet "State" column = product.modality:
# Biologic / Liquid / Frozen Liquid / Lyo Powder / Lyo-Cake). A liquid, a lyophilised powder/cake and a
# biologic are visually distinguishable, so a cap-colour clash only matters within the same state. Set
# COMPARE_BY_STATE=False to compare across all states (the previous behaviour). STATE_GROUPS optionally
# MERGES modality values into one comparison group (e.g. lump 'Lyo Powder' + 'Lyo-Cake'); empty = every
# distinct modality is its own group. SME-tunable.
COMPARE_BY_STATE = True
STATE_GROUPS = {}


def _state_key(modality):
    """Comparison key for a product's state ('modality'). Same key ⇒ the two are compared for a cap clash.
    None when the state is blank (a blank-state product only compares against other blank-state products)."""
    m = (modality or "").strip()
    if not m:
        return None
    for group, members in STATE_GROUPS.items():
        if m.upper() in {x.upper() for x in members}:
            return group.upper()
    return m.upper()


def _vial_key(vial_size):
    """Normalise a vial-size string to a comparison key: prefer an 'NR' token (2R/10R/20R), else an
    'N ML' token, else the whole string normalised. So '2R (2.00 mL) vial' and '2R' match; '10 mL vial'
    and '10mL Botox' match on '10ML'."""
    s = (vial_size or "").upper()
    m = re.search(r"(\d+)\s*R\b", s)
    if m:
        return m.group(1) + "R"
    m = re.search(r"(\d+)\s*ML\b", s)
    if m:
        return m.group(1) + "ML"
    return re.sub(r"[^A-Z0-9]", "", s)


def _colocated_mfr_products(con, subject):
    """In-scope (product_id, modality) MANUFACTURED at any site the subject is manufactured at (diff program)."""
    return [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT p2.product_id, p2.modality "
        "FROM product_site ps1 "
        "JOIN product_site ps2 ON ps2.site_id=ps1.site_id AND ps2.role='mfr' "
        "JOIN product p2 ON p2.product_id=ps2.product_id "
        f"WHERE ps1.product_id=? AND ps1.role='mfr' AND p2.product_id<>? AND {scope.scope_sql('p2')} "
        "AND (p2.program_no IS NULL OR p2.program_no<>?)",
        (subject["product_id"], subject["product_id"], subject["program_no"]))]


def _cap_rows(con, pid):
    """Raw (cap_color_name, color_code, vendor_id) rows for a presentation."""
    return con.execute(
        "SELECT cap_color_name, color_code, vendor_id FROM cap_color WHERE product_id=?", (pid,)).fetchall()


_VENDOR_BY_ID = {1: "Datwyler", 2: "West", 3: "West"}


def _taken(con, subject, comparator_ids):
    """Build the 'already-utilised' cap list at the subject's mfr sites.
    Each entry: {product, program_no, source_row, vial_size, vial_key, state, color, hex, is_custom, raw};
    _grade_taken() then adds same_vial / delta_e_to_subject / close_shade / risk / risk_reason.

    `source_row` is the Smartsheet row behind the comparator — carried because duplicate Smartsheet rows
    produce entries that are otherwise identical (same product, cap and vial size), and a front-end
    listing them needs a stable identity for each. It is the stable key across rebuilds; product_id is not.
    """
    taken = []
    for cid in comparator_ids:
        p = risk._presentation(con, cid)
        if p is None:
            continue
        sr = con.execute("SELECT source_row FROM product WHERE product_id=?", (cid,)).fetchone()
        vk = _vial_key(p.get("vial_container_size"))
        for name, code, vid in _cap_rows(con, cid):
            norm = cc.normalize(name, vendor_hint=_VENDOR_BY_ID.get(vid))
            if norm["is_unknown"]:
                continue
            taken.append({"product": risk._label(p), "program_no": p.get("program_no"),
                          "source_row": sr[0] if sr else None,
                          "vial_size": p.get("vial_container_size"), "vial_key": vk,
                          "state": p.get("modality"),
                          "color": norm["canonical_color"], "hex": norm["hex"],
                          "is_custom": norm["is_custom"], "raw": norm["raw"]})
    return taken


def _grade_taken(taken, subj_vk, subject_cap):
    """Grade each already-utilised cap for mix-up risk against the subject, IN PLACE.

    Adds to every entry: same_vial, delta_e_to_subject, close_shade, risk ('High'/'Medium'/'Low') and an
    explainable risk_reason. Returns the list sorted highest-risk first (ties: closest shade first).

    The subject may have no cap yet, or one that does not resolve to a palette hex ('TBD', a custom text).
    There is then NO shade to compare: delta_e_to_subject stays None, close_shade is False, and the grade
    rests on the vial size alone — said so in risk_reason, so the screen never implies a colour comparison
    that did not happen (the same honesty rule as the min_delta_e ranking).
    """
    subj_hex = ((subject_cap or {}).get("hex") or "").strip()
    for t in taken:
        same_vial = bool(subj_vk) and t.get("vial_key") == subj_vk
        de = cc.delta_e(subj_hex, t.get("hex")) if subj_hex else None
        close = de is not None and de < SAME_SHADE_DELTA_E
        t["same_vial"] = same_vial
        t["delta_e_to_subject"] = round(de, 1) if de is not None else None
        t["close_shade"] = close
        vial = t.get("vial_key") or t.get("vial_size") or "?"
        if de is None:
            shade = ("shade not compared — no cap colour with a known swatch is selected for this "
                     "presentation yet" if not subj_hex else
                     "shade not compared — this cap has no swatch colour on file")
        elif close:
            shade = f"a near-identical shade to the current cap (ΔE {t['delta_e_to_subject']})"
        else:
            shade = f"a clearly different shade from the current cap (ΔE {t['delta_e_to_subject']})"
        if same_vial and close:
            t["risk"] = "High"
            t["risk_reason"] = f"Same vial size ({vial}) and {shade} — highest mix-up risk."
        elif same_vial:
            t["risk"] = "Medium"
            t["risk_reason"] = (f"Same vial size ({vial}), so the container alone does not distinguish the "
                                f"two — {shade}. This colour is blocked at {vial}.")
        elif close:
            t["risk"] = "Medium"
            t["risk_reason"] = (f"{shade[0].upper()}{shade[1:]}, but a different vial size "
                                f"({vial} vs {subj_vk or '?'}) distinguishes them.")
        else:
            t["risk"] = "Low"
            t["risk_reason"] = (f"Different vial size ({vial} vs {subj_vk or '?'}) and {shade}.")
    taken.sort(key=lambda t: (RISK_ORDER.get(t["risk"], 3),
                              t["delta_e_to_subject"] if t["delta_e_to_subject"] is not None else 1e9))
    return taken


def _min_delta_e(hexv, taken):
    """Smallest ΔE from a candidate hex to ANY taken cap that has a solid hex. Returns (dE, nearest)."""
    best, near = None, None
    for t in taken:
        de = cc.delta_e(hexv, t["hex"])
        if de is None:
            continue
        if best is None or de < best:
            best, near = de, t
    return best, near


def recommend(con, subject_product_id, vendor=ALL_VENDORS, top_n=DEFAULT_TOP_N):
    """Recommend cap colours for one subject presentation. Dict-returning; never raises on data gaps."""
    subject = risk._presentation(con, subject_product_id)
    if subject is None:
        return {"error": f"no product with product_id={subject_product_id}"}

    subj_label = risk._label(subject)
    subj_vial = subject.get("vial_container_size")
    subj_vk = _vial_key(subj_vial)
    subj_caps = sorted(subject["cap_colors"])          # normalised existing subject caps (may be empty)

    # The subject's OWN cap(s) for display (raw text + swatch hex) and to detect an already-made selection.
    caps_display, subject_selected = [], None
    for _name, _code, _vid in _cap_rows(con, subject_product_id):
        _n = cc.normalize(_name, vendor_hint=_VENDOR_BY_ID.get(_vid))
        caps_display.append({"raw": _name, "hex": _n["hex"], "canonical": _n["canonical_color"],
                             "is_unknown": _n["is_unknown"]})
        if subject_selected is None and not _n["is_unknown"] and _n["canonical_color"]:
            subject_selected = {"raw": _name, "hex": _n["hex"], "canonical": _n["canonical_color"]}

    mfr_sites = risk._subject_sites(con, subject_product_id)   # [(site_id, code, name, role='mfr')]

    if not mfr_sites:
        return {"subject": {"product_id": subject_product_id, "label": subj_label,
                            "vial_size": subj_vial, "state": subject.get("modality"),
                            "caps_display": caps_display}, "vendor": vendor,
                "sites": [], "taken": [], "recommended": [], "discouraged": [],
                "subject_selected": subject_selected, "first_unique": None,
                "note": f"{subj_label} has no manufacturing site recorded — cannot assess cap co-location. "
                        "Add its mfr site (Smartsheet / PDD) to enable a recommendation."}

    subj_state = _state_key(subject.get("modality"))
    colocated = _colocated_mfr_products(con, subject)
    if COMPARE_BY_STATE:                       # compare only within the same state (Biologic/Liquid/Lyo…)
        comparator_ids = [pid for pid, mod in colocated if _state_key(mod) == subj_state]
    else:
        comparator_ids = [pid for pid, _mod in colocated]
    taken = _grade_taken(_taken(con, subject, comparator_ids), subj_vk, subject_selected)

    # colours already used AT THE SUBJECT'S VIAL SIZE ⇒ hard-excluded (the colour×size uniqueness rule)
    taken_same_size = {t["color"].upper() for t in taken if t["vial_key"] == subj_vk and t["color"]}

    # vendor=ALL_VENDORS (None) ⇒ both catalogues. off_the_shelf_only=False keeps any non-stock colour in
    # the list so the RANKING can put it below the stock ones, rather than hiding it (deck slide 20 prefers
    # off-the-shelf; it does not forbid showing the alternative).
    candidates = cc.available_for(vendor, subj_vk if re.match(r"\d+MM", subj_vk or "") else None,
                                  off_the_shelf_only=False)
    # (vial_container_size is a nominal 'R'/mL size, not a cap Ø; without a vial-R→cap-mm map we don't
    #  size-filter the palette yet — all off-the-shelf colours are candidates. Tracked as an open item.)

    recommended, discouraged = [], []
    for c in candidates:
        color = c["canonical_color"]
        de, near = _min_delta_e(c["hex"], taken)
        entry = {"color": color, "vendor": c["vendor"], "vendor_code": c["vendor_code"],
                 "vendor_color_name": c["vendor_color_name"], "hex": c["hex"],
                 "off_the_shelf": c["off_the_shelf"], "min_delta_e": round(de, 1) if de is not None else None,
                 "nearest_taken": (f"{near['color']} on {near['product']} @ {near['vial_size']}"
                                   if near else None)}
        if color and color.upper() in taken_same_size:
            entry["reason"] = (f"{color} is already used at vial size {subj_vk} at a shared "
                               "manufacturing site (same colour × vial size).")
            discouraged.append(entry)
        elif de is not None and de < CLOSE_DELTA_E:
            entry["reason"] = (f"perceptually close (ΔE {entry['min_delta_e']}) to {entry['nearest_taken']} "
                               "— distinct colour preferred but not blocked (different vial size).")
            entry["rationale"] = entry["reason"]
            recommended.append(entry)      # allowed, but will rank below clearly-distinct colours
        else:
            entry["rationale"] = (f"distinct from all co-located caps (min ΔE "
                                  f"{entry['min_delta_e'] if entry['min_delta_e'] is not None else 'n/a'})."
                                  if taken else "no co-located caps recorded — any off-the-shelf colour is free.")
            recommended.append(entry)

    # Rank: off-the-shelf first (deck slide 20 — "prefer off-the-shelf; avoid custom"), then most distinct
    # first (largest min ΔE).
    #
    # A `None` ΔE sorts LAST, not first, and that is deliberate — the earlier comment here claimed the
    # opposite of what the code does. None is ambiguous: it means either "no co-located cap has a comparable
    # hex" (genuinely unconstrained) or "THIS CANDIDATE has no hex", which is the real case for
    # `Transparent 6001`. Since the two are indistinguishable, the honest answer is "cannot demonstrate
    # distinctness", so it must not outrank a colour measured as distinct.
    #
    # Sorting is stable, so anything tied on both keys keeps PALETTE order — which is the documented
    # fallback when there is no ΔE signal at all (e.g. a subject whose only same-state neighbour has an
    # unknown cap: every ΔE is None and the answer is simply the catalogue order).
    recommended.sort(
        key=lambda e: (1 if e["off_the_shelf"] else 0,
                       1 if e["min_delta_e"] is not None else 0,
                       e["min_delta_e"] if e["min_delta_e"] is not None else 0.0),
        reverse=True)

    return {
        "subject": {"product_id": subject_product_id, "label": subj_label, "program_no": subject["program_no"],
                    "vial_size": subj_vial, "vial_key": subj_vk, "state": subject.get("modality"),
                    "current_caps": subj_caps, "caps_display": caps_display},
        # The restriction that was applied, if any. None = every supplier was considered, which is the
        # default; a front-end should read `vendor` on each recommendation, not this.
        "vendor": vendor,
        "vendors_considered": sorted({c["vendor"] for c in candidates if c.get("vendor")}),
        "sites": [f"{name or code} ({code})" for _sid, code, name, _role in mfr_sites],
        "n_colocated": len(comparator_ids),
        "subject_selected": subject_selected,    # the subject's already-chosen cap colour (or None)
        "first_unique": None,                     # set by recommend_program_presentations (cross-presentation)
        "taken": taken,
        "recommended": recommended[:top_n],
        "discouraged": discouraged,
        "note": ("Provisional, explainable recommendation ("
                 + ("every supplier's palette" if vendor is None else f"{vendor}'s palette only")
                 + ", off-the-shelf preferred, colour×vial-size "
                 "uniqueness at the manufacturing site, "
                 + (f"compared only against same-state '{subject.get('modality') or '—'}' products, "
                    if COMPARE_BY_STATE else "compared across all states, ")
                 + "ranked by perceptual distinctness). "
                 + ("Colours from both suppliers are ranked together, so the same hue may appear twice — "
                    "one entry per supplier. Cross-supplier ΔE is weaker evidence than within-supplier ΔE, "
                    "because the palette hex values are transcribed approximations. "
                    if vendor is None else "")
                 + "Vial-Ø→cap-Ø size filtering, measured swatch hex and the SME "
                 "distinctness threshold are open items. SME approves the final colour."),
    }


def recommend_program_presentations(con, program_no, vendor=ALL_VENDORS, top_n=DEFAULT_TOP_N):
    """Recommend for EVERY in-scope presentation of a program, enforcing a UNIQUE first recommended colour
    across presentations (so two presentations of the same product never get the same #1 pick — they must
    be visually distinguishable from each other too). Presentations that ALREADY have a selected cap keep
    it: that colour is reserved and not offered as another presentation's #1.

    Returns (results_by_product_id, source_row_to_product_id). Each result gets its `recommended` list
    reordered so the assigned unique colour is first, and `first_unique` set to that colour's name."""
    rows = con.execute(
        f"SELECT product_id, source_row FROM product p WHERE program_no=? AND {scope.scope_sql('p')} "
        "ORDER BY source_row", (program_no,)).fetchall()
    results = {pid: recommend(con, pid, vendor=vendor, top_n=top_n) for pid, _sr in rows}

    used = set()   # canonical colours already spoken for across this product's presentations
    # pass 1: reserve every presentation's already-selected colour
    for pid, _sr in rows:
        sel = results[pid].get("subject_selected")
        if sel and sel.get("canonical"):
            used.add(sel["canonical"].upper())
    # pass 2: for presentations without a selection, take the top recommended colour not yet used, and
    # move it to the front so it is the FIRST option shown for that presentation.
    for pid, _sr in rows:
        res = results[pid]
        if res.get("error") or res.get("subject_selected") or not res.get("recommended"):
            continue
        chosen = next((e for e in res["recommended"] if (e["color"] or "").upper() not in used), None)
        if chosen is None:
            chosen = res["recommended"][0]           # all candidates collide → keep the most distinct
        used.add((chosen["color"] or "").upper())
        res["recommended"] = [chosen] + [e for e in res["recommended"] if e is not chosen]
        res["first_unique"] = chosen["vendor_color_name"]
    return results, {sr: pid for pid, sr in rows}


def recommend_program(program_no, product_id=None, vendor=ALL_VENDORS, db=None, top_n=DEFAULT_TOP_N):
    """Convenience wrapper by program code (optionally a specific presentation product_id)."""
    con = sqlite3.connect(db or paths.db_path())
    try:
        pid = risk._resolve_pid(con, program_no, product_id)
        return recommend(con, pid, vendor=vendor, top_n=top_n)
    finally:
        con.close()


def _print(res):
    if "error" in res:
        print("ERROR:", res["error"]); return
    s = res["subject"]
    scope_text = ("all suppliers: " + ", ".join(res.get("vendors_considered") or ["-"])
                  if res.get("vendor") is None else f"{res['vendor']} only")
    print(f"\n=== Cap-colour recommendation: {s['label']} "
          f"(vial {s['vial_size']} → key {s['vial_key']}) — {scope_text} ===")
    print(f"Manufacturing site(s): {', '.join(res['sites']) or '-'}   |   "
          f"{res.get('n_colocated', 0)} co-located product(s)")
    if s.get("current_caps"):
        print(f"Current subject cap(s): {', '.join(s['current_caps'])}")
    print(f"\nHighest similarity risks — already-utilised caps at these site(s) ({len(res['taken'])}):")
    for t in res["taken"]:
        tag = " [CUSTOM]" if t["is_custom"] else ""
        print(f"  [{t.get('risk', '?'):6}] {t['color'] or '?':10} {t['hex']:8} vial {t['vial_key']:5} "
              f"on {t['product']}{tag}\n            {t.get('risk_reason', '')}")
    print(f"\nRecommended (top {len(res['recommended'])}, off-the-shelf then most distinct):")
    for e in res["recommended"]:
        stock = "" if e.get("off_the_shelf") else " [NOT off-the-shelf]"
        print(f"  {(e.get('vendor') or '?'):9} {e['vendor_color_name']:20} {e['hex']:8} "
              f"minΔE={str(e['min_delta_e']):5}{stock}  — {e['rationale']}")
    if res["discouraged"]:
        print(f"\nDiscouraged ({len(res['discouraged'])}):")
        for e in res["discouraged"]:
            print(f"  {e['vendor_color_name']:20} — {e['reason']}")
    print(f"\n({res['note']})")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    program = sys.argv[1] if len(sys.argv) > 1 else "AGN-151586"
    # An explicit supplier on the command line RESTRICTS; omitting it spans every supplier.
    vendor = sys.argv[2] if len(sys.argv) > 2 else ALL_VENDORS
    _print(recommend_program(program, vendor=vendor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

