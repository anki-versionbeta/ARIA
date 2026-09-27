"""Similarity / mix-up risk engine (PROVISIONAL, explainable placeholder).

Answers the PSA question: "Could this product be confused with another product
at the same manufacturing / packaging site?" — the same judgement the analyst
records by hand in Parts D.2 / E.2 of the assessment.

Model (placeholder until SME thresholds arrive — CLAUDE.md F-4 / Risk Index):
  1. Comparators = other IN-SCOPE presentations (scope.py: clinical excluded)
     that share a site with the subject presentation.
  2. For each comparator, compare the visual / physical DISTINGUISHING attributes
     (form, vial size, fill volume, product colour, cap colour, strength, shape,
     marking). Count how many DIFFER.
  3. Fewer differences ⇒ the two look more alike ⇒ HIGHER mix-up risk.
     0–1 diffs → High, 2 → Med, ≥3 → Low  (GRADE_THRESHOLDS; calibrated to the signed baselines).
  4. The subject's overall risk is driven by its MOST-SIMILAR comparator.

Output is explainable (closest comparator + per-attribute comparison + rationale)
and persisted to the existing risk_assessment / risk_attribute_cmp tables.

IMPORTANT: this is analysis/audit output for the UI. It does NOT auto-fill the
form's Similarity Risk boxes (Parts D/E stay analyst/SME work) — see CLAUDE.md.

Grading is pluggable: replace GRADE_THRESHOLDS / ATTRIBUTE_WEIGHTS with the SME
model when defined — no structural change.
"""
import re
import sqlite3
import sys

from . import paths, scope

# Distinguishing appearance/physical attributes (product columns) + display label + weight.
# Weights are reserved for the future SME model; the placeholder grades on an unweighted count.
# NOTE: "strength" is intentionally EXCLUDED (business decision 2026-07-23) — it is not a visual
# mix-up cue and often differs between otherwise look-alike products, so it is not compared here.
APPEARANCE_ATTRS = [
    ("form",                  "Form"),
    ("vial_container_size",   "Vial / container size"),
    ("target_fill_volume_ml", "Fill volume"),
    ("product_color",         "Product colour"),
    ("shape",                 "Shape"),
    ("marking",               "Marking"),
    # cap colour is handled separately (it lives in the cap_color table, may be multi-valued)
]
ATTRIBUTE_WEIGHTS = {k: 1.0 for k, _ in APPEARANCE_ATTRS}
ATTRIBUTE_WEIGHTS["cap_color"] = 1.0

# count of DISTINGUISHING differences → risk grade. Calibrated against the three signed baselines
# (docs/PSA_Baseline_Test_Combined.md): assessors treat two clear differentiators as enough to rule
# out mix-up, so only near-identical products (0–1 diffs) grade High. SME-tunable.
GRADE_THRESHOLDS = {"high_max_diffs": 1, "med_max_diffs": 2}   # 0–1=High, 2=Med, ≥3=Low

# Which risk levels set the form's "Similarity Risk = Yes" when D/E is auto-filled.
# Baseline-calibrated: Yes ONLY when near-identical (High = ≤1 distinguishing attribute). A 2-diff
# "Med" pair maps to No but is surfaced as a borderline "SME to confirm" note in D.2/E.2 (see
# _narrative). This lifts auto-alignment vs the signed examples from 1/7 to 6/7. SME-tunable — and a
# human assessment record, when present, always overrides this auto-fill (see populate_template).
RISK_YES_LEVELS = {"High"}

# Dosage-form FAMILY grouping for the D/E comparator universe (from the signed baselines:
# "filled liquid vials", "lyophilised powder vial products"). Groups the finer Smartsheet
# `modality` values into the families the assessors compare within. SME-tunable.
FAMILY = {"LIQUID": {"Liquid", "Frozen Liquid"},
          "LYOPHILISED": {"Lyo Powder", "Lyo-Cake"}}
FAMILY_LABEL = {"LIQUID": "Liquid vial drug products", "LYOPHILISED": "Lyophilised powder vial products"}


def _family_key(subject):
    m = (subject.get("modality") or "").strip()
    for key, mods in FAMILY.items():
        if m in mods:
            return key
    if "lyophil" in (subject.get("form") or "").lower():
        return "LYOPHILISED"
    return None


def _family_predicate(subject):
    """(SQL fragment, params) matching products in the SAME dosage-form family as `subject`
    (for a `product` row aliased p2). Falls back to same `form`, then to no filter."""
    key = _family_key(subject)
    if key:
        mods = FAMILY[key]
        return "p2.modality IN (%s)" % ",".join("?" * len(mods)), list(mods)
    if subject.get("form"):
        return "p2.form = ?", [subject["form"]]
    return "1=1", []


def _family_label(subject):
    return FAMILY_LABEL.get(_family_key(subject), "Vial drug products")


def _same_family(subject, comp):
    """True if `comp` is in the same dosage-form family as `subject` — mirrors the report's
    _family_predicate so the preview's PRIMARY assessment matches Part D/E scoping."""
    sk = _family_key(subject)
    if sk:
        return _family_key(comp) == sk
    if subject.get("form"):
        return _norm(comp.get("form")) == _norm(subject.get("form"))
    return True


def _norm(s):
    """Normalise a value for equality comparison (mirror of workflow._norm)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def grade(n_diffs):
    """Map a distinguishing-difference count to a risk level (fewer diffs ⇒ higher risk)."""
    if n_diffs <= GRADE_THRESHOLDS["high_max_diffs"]:
        return "High"
    if n_diffs <= GRADE_THRESHOLDS["med_max_diffs"]:
        return "Med"
    return "Low"


def _cap_colors(con, pid):
    """Set of normalised cap-colour identifiers for a presentation (name preferred, else code)."""
    out = set()
    for name, code in con.execute(
            "SELECT cap_color_name, color_code FROM cap_color WHERE product_id=?", (pid,)):
        val = _norm(name) or _norm(code)
        if val:
            out.add(val)
    return out


def _presentation(con, pid):
    """Load a presentation's identity + appearance attributes as a dict."""
    cols = ["program_no", "program_name", "batch_type", "status", "modality"] + [k for k, _ in APPEARANCE_ATTRS]
    row = con.execute(f"SELECT {','.join(cols)} FROM product WHERE product_id=?", (pid,)).fetchone()
    if not row:
        return None
    d = dict(zip(cols, row))
    d["product_id"] = pid
    d["cap_colors"] = _cap_colors(con, pid)
    return d


def _shared_sites(con, subject_id, comparator_id):
    """[(site_code, role)] the two presentations have in common."""
    return [(sc, role) for sc, role in con.execute(
        "SELECT DISTINCT s.site_code, ps1.role FROM product_site ps1 "
        "JOIN product_site ps2 ON ps2.site_id=ps1.site_id AND ps2.role=ps1.role "
        "JOIN site s ON s.site_id=ps1.site_id "
        "WHERE ps1.product_id=? AND ps2.product_id=?", (subject_id, comparator_id))]


def _compare(subject, comparator):
    """Per-attribute comparison. Returns (rows, n_distinguishing).
    An attribute is DISTINGUISHING only when BOTH sides are known AND differ —
    an unknown value can't be relied on to tell products apart (conservative: not distinguishing)."""
    rows = []
    for key, label in APPEARANCE_ATTRS:
        sv, cv = subject.get(key), comparator.get(key)
        ns, nc = _norm(sv), _norm(cv)
        distinguishing = bool(ns) and bool(nc) and ns != nc
        rows.append({"attribute": label, "subject_value": (sv or "").strip() if sv else "",
                     "comparator_value": (cv or "").strip() if cv else "",
                     "is_distinguishing": distinguishing, "weight": ATTRIBUTE_WEIGHTS.get(key, 1.0)})
    # cap colour (set comparison): distinguishing when both have colours and share none
    scaps, ccaps = subject["cap_colors"], comparator["cap_colors"]
    cap_dist = bool(scaps) and bool(ccaps) and scaps.isdisjoint(ccaps)
    rows.append({"attribute": "Cap colour",
                 "subject_value": ", ".join(sorted(scaps)), "comparator_value": ", ".join(sorted(ccaps)),
                 "is_distinguishing": cap_dist, "weight": ATTRIBUTE_WEIGHTS["cap_color"]})
    n_dist = sum(1 for r in rows if r["is_distinguishing"])
    return rows, n_dist


def _comparators(con, subject):
    """Distinct in-scope product_ids sharing ≥1 site with the subject (different program)."""
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT p2.product_id FROM product_site ps1 "
        "JOIN product_site ps2 ON ps2.site_id=ps1.site_id AND ps2.role=ps1.role "
        "JOIN product p2 ON p2.product_id=ps2.product_id "
        f"WHERE ps1.product_id=? AND p2.product_id<>? AND {scope.scope_sql('p2')} "
        "AND (p2.program_no IS NULL OR p2.program_no<>?)",
        (subject["product_id"], subject["product_id"], subject["program_no"]))]
    return ids


def _label(p):
    """Human label for a presentation (program name preferred; program_no when meaningful)."""
    name = (p.get("program_name") or "").strip()
    prog = (p.get("program_no") or "").strip()
    if prog and prog.upper() not in ("N/A", "NA", ""):
        return f"{prog}" + (f" ({name})" if name and _norm(name) != _norm(prog) else "")
    return name or prog or f"product {p['product_id']}"


def _assess_raw(con, subject_product_id):
    """Core comparison: return (subject, results) with results sorted most-similar first.
    Each result carries the raw comparator dict, per-attribute comparison, n_distinguishing,
    shared_sites as (site_code, role) tuples, and the per-comparator risk_level."""
    subject = _presentation(con, subject_product_id)
    if subject is None:
        raise ValueError(f"no product with product_id={subject_product_id}")
    results = []
    for cid in _comparators(con, subject):
        comparator = _presentation(con, cid)
        if comparator is None:
            continue
        rows, n_dist = _compare(subject, comparator)
        shared = _shared_sites(con, subject_product_id, cid)
        results.append({"comparator": comparator, "attributes": rows,
                        "n_distinguishing": n_dist, "shared_sites": shared,
                        "risk_level": grade(n_dist)})
    results.sort(key=lambda r: r["n_distinguishing"])   # fewest diffs ⇒ highest risk, first
    return subject, results


def _dedup_by_program(results):
    """Collapse to the most-similar presentation per program (results must be pre-sorted)."""
    out, seen = [], set()
    for r in results:
        key = _norm(r["comparator"]["program_no"]) or _norm(_label(r["comparator"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def assess(con, subject_product_id, persist=True):
    """Assess one subject presentation against its co-located in-scope comparators.

    The PRIMARY assessment (grade / closest / comparators) is scoped exactly like the report's
    Part D/E — comparators that share a MANUFACTURING site with the subject AND are the same
    dosage-form family — so the preview headline agrees with the generated report. Everything else
    that is merely co-located (packaging-site-shared or a different family) is returned separately as
    `informational` — shown in the UI for context but NOT assessed and NOT written to the report.
    """
    subject, results = _assess_raw(con, subject_product_id)   # all co-located, any role, any family

    def _is_primary(r):
        shares_mfr = any(role == "mfr" for _sc, role in r["shared_sites"])
        return shares_mfr and _same_family(subject, r["comparator"])

    primary_raw = [r for r in results if _is_primary(r)]
    info_raw = [r for r in results if not _is_primary(r)]

    if persist:
        _persist(con, subject, primary_raw)     # audit the ASSESSED (mfr + same-family) set

    primary = _dedup_by_program(primary_raw)     # most-similar presentation per program
    seen = {_norm(r["comparator"]["program_no"]) or _norm(_label(r["comparator"])) for r in primary}
    informational = []
    for r in _dedup_by_program(info_raw):        # exclude programs already shown as primary
        key = _norm(r["comparator"]["program_no"]) or _norm(_label(r["comparator"]))
        if key not in seen:
            informational.append(r)

    overall = grade(primary[0]["n_distinguishing"]) if primary else "None"
    closest = primary[0] if primary else None
    rationale = _rationale(subject, closest, len(primary))

    return {
        "subject": {"product_id": subject_product_id, "program_no": subject["program_no"],
                    "program_name": subject["program_name"], "batch_type": subject["batch_type"],
                    "label": _label(subject)},
        "overall_risk": overall,
        "n_comparators": len(primary),
        "closest": _slim(closest),
        "comparators": [_slim(r) for r in primary],
        "informational": [_slim(r) for r in informational],
        "rationale": rationale,
        "note": "Provisional heuristic (count of distinguishing attributes). Assessment = same-family "
                "products co-manufactured at a shared site (matches the report). 'Informational' rows are "
                "packaging-site or other-family co-location — shown for context, not assessed, not in the report.",
    }


def _slim(r):
    """UI-friendly view of one comparator result (or None)."""
    if r is None:
        return None
    c = r["comparator"]
    return {"label": _label(c), "program_no": c["program_no"], "batch_type": c["batch_type"],
            "risk_level": r["risk_level"], "n_distinguishing": r["n_distinguishing"],
            "shared_sites": [f"{sc} ({role})" for sc, role in r["shared_sites"]],
            "distinguishing": [a["attribute"] for a in r["attributes"] if a["is_distinguishing"]],
            "shared_attributes": [a["attribute"] for a in r["attributes"]
                                  if not a["is_distinguishing"] and (a["subject_value"] or a["comparator_value"])],
            "attributes": r["attributes"]}


def _rationale(subject, closest, n):
    if closest is None:
        return (f"{_label(subject)} shares no manufacturing site with an in-scope, same-family product — "
                "no co-located mix-up comparator found for the assessment (see any informational "
                "packaging-site co-location below).")
    c = _slim(closest)
    sites = ", ".join(c["shared_sites"]) or "a shared site"
    if closest["n_distinguishing"] == 0:
        return (f"Highest concern: {c['label']} at {sites} matches {_label(subject)} on every compared "
                f"attribute — no visual differentiator found (High). Review appearance / labelling controls.")
    diffs = ", ".join(a["attribute"].lower() for a in closest["attributes"] if a["is_distinguishing"])
    return (f"Closest co-located product is {c['label']} at {sites}, distinguished from {_label(subject)} "
            f"by {closest['n_distinguishing']} attribute(s): {diffs} ⇒ {c['risk_level']} mix-up risk. "
            f"{n} in-scope co-located comparator(s) assessed.")


def _persist(con, subject, results):
    """Write results to risk_assessment / risk_attribute_cmp (idempotent per subject)."""
    cur = con.cursor()
    old = [r[0] for r in cur.execute(
        "SELECT assessment_id FROM risk_assessment WHERE subject_product_id=?", (subject["product_id"],))]
    if old:
        qs = ",".join("?" * len(old))
        cur.execute(f"DELETE FROM risk_attribute_cmp WHERE assessment_id IN ({qs})", old)
        cur.execute(f"DELETE FROM risk_assessment WHERE assessment_id IN ({qs})", old)
    for r in results:
        c = r["comparator"]
        site_id = None
        if r["shared_sites"]:
            row = cur.execute("SELECT site_id FROM site WHERE site_code=?",
                              (r["shared_sites"][0][0],)).fetchone()
            site_id = row[0] if row else None
        cur.execute(
            "INSERT INTO risk_assessment (subject_product_id, comparator_product_id, site_id, "
            "assessed_date, num_distinguishing_diffs, risk_level, rationale, status) "
            "VALUES (?,?,?,datetime('now'),?,?,?, 'auto-provisional')",
            (subject["product_id"], c["product_id"], site_id, r["n_distinguishing"], r["risk_level"],
             _rationale(subject, r, len(results))))
        aid = cur.lastrowid
        for a in r["attributes"]:
            cur.execute(
                "INSERT INTO risk_attribute_cmp (assessment_id, attribute, subject_value, "
                "comparator_value, is_distinguishing, weight) VALUES (?,?,?,?,?,?)",
                (aid, a["attribute"], a["subject_value"], a["comparator_value"],
                 1 if a["is_distinguishing"] else 0, a["weight"]))
    con.commit()


def assess_program(program_no, product_id=None, db=None, persist=True):
    """Convenience wrapper: assess a program (optionally a specific presentation product_id)."""
    con = sqlite3.connect(db or paths.db_path())
    try:
        return assess(con, _resolve_pid(con, program_no, product_id), persist=persist)
    finally:
        con.close()


def _resolve_pid(con, program_no, product_id):
    if product_id is not None:
        return product_id
    row = con.execute("SELECT product_id FROM product WHERE program_no=? ORDER BY source_row",
                      (program_no,)).fetchone()
    if not row:
        raise ValueError(f"no product with program_no={program_no}")
    return row[0]


# ------------------------------------------------ Parts D & E auto-fill (draft)
# Mapping follows the signed baselines (see docs/PSA_DE_Mapping_Design.md):
#   Part D = late-stage PIPELINE products of the same dosage-form family (ALL sites), one block.
#   Part E = products AT EACH SITE of the same family, one block PER SITE.
DRAFT_TAG = "[Auto-generated draft from the similarity analysis — SME to review and finalise.]"


def _assess_against(con, subject, comparator_ids):
    """Compare the subject to an explicit set of comparator product_ids; sorted most-similar first."""
    results = []
    for cid in comparator_ids:
        c = _presentation(con, cid)
        if c is None:
            continue
        rows, n = _compare(subject, c)
        results.append({"comparator": c, "attributes": rows, "n_distinguishing": n, "risk_level": grade(n)})
    results.sort(key=lambda r: r["n_distinguishing"])
    return results


def _pipeline_comparators(con, subject):
    """Late-stage (non-clinical, non-discontinued) products of the same dosage-form family, ANY site."""
    fam_sql, fam_args = _family_predicate(subject)
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT p2.product_id FROM product p2 "
        f"WHERE {scope.late_stage_sql('p2')} AND {fam_sql} AND p2.product_id<>? "
        f"AND (p2.program_no IS NULL OR p2.program_no<>?)",
        fam_args + [subject["product_id"], subject["program_no"]])]


def _site_comparators(con, subject, site_id):
    """In-scope same-family products MANUFACTURED (role='mfr') at a specific site.
    Part E assesses manufacturing-site co-location only; packaging co-location is preview-only."""
    fam_sql, fam_args = _family_predicate(subject)
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT p2.product_id FROM product_site ps2 JOIN product p2 ON p2.product_id=ps2.product_id "
        f"WHERE ps2.site_id=? AND ps2.role='mfr' AND {scope.scope_sql('p2')} AND {fam_sql} AND p2.product_id<>? "
        f"AND (p2.program_no IS NULL OR p2.program_no<>?)",
        [site_id] + fam_args + [subject["product_id"], subject["program_no"]])]


def _subject_sites(con, pid):
    """Distinct MANUFACTURING sites (site_id, site_code, site_name, role) of the subject.
    Part E is scoped to manufacturing sites only (business decision 2026-07-23; QPP §3);
    packaging sites appear in the app preview but are NEVER written to the report's Part E."""
    return con.execute(
        "SELECT DISTINCT s.site_id, s.site_code, s.site_name, ps.role FROM product_site ps "
        "JOIN site s ON s.site_id=ps.site_id WHERE ps.product_id=? AND ps.role='mfr' "
        "ORDER BY s.site_code", (pid,)).fetchall()


def _families_text(subject, results, context):
    """Enumerate the same-family comparators (most-similar first) for a D.1 / E.1 families cell."""
    names = [_label(r["comparator"]) for r in _dedup_by_program(results)]
    lead = f"{_family_label(subject)} {context}"
    return lead + (": " + ", ".join(names) + "." if names else " — none identified in the catalogue.")


def _narrative(subject, results, against_label, level, box):
    """D.2 / E.2 draft comment for a comparator set."""
    if not results:
        return (f"{DRAFT_TAG}\nNo in-scope {against_label} were found in the catalogue for comparison; "
                "no mix-up risk identified.")
    n = len(_dedup_by_program(results))
    closest = results[0]
    c = closest["comparator"]
    diffs = [a["attribute"].lower() for a in closest["attributes"] if a["is_distinguishing"]]
    lines = [DRAFT_TAG, f"{_label(subject)} was compared against {n} in-scope {against_label}."]
    if closest["n_distinguishing"] == 0:
        lines.append(f"The most similar, {_label(c)}, matches on every compared appearance attribute — "
                     "no visual differentiator was found.")
    else:
        lines.append(f"The most similar, {_label(c)}, is distinguished by "
                     f"{closest['n_distinguishing']} attribute(s): {', '.join(diffs)}.")
    lines.append(f"Provisional mix-up risk: {level} → Similarity Risk = {box}.")
    if box == "Yes":
        lines.append("Recommend SME review of appearance / labelling / handling controls.")
    elif level == "Med":
        lines.append(f"Borderline: the closest product differs by only {closest['n_distinguishing']} "
                     "attribute(s) — SME to confirm this is sufficient differentiation to rule out mix-up.")
    return "\n".join(lines)


def _box(level):
    return "Yes" if level in RISK_YES_LEVELS else "No"


def form_fields(db, program_no, product_id=None):
    """Draft Parts D & E from the risk analysis, for populate_template to consume.
    Returns D fields (keys match the analyst-JSON so an analyst record overrides them via docattrs)
    plus `_e_sites` — one entry per site for the dynamic per-site Part E rendering.
    Never raises — returns {} on any failure (D/E then fall back to derived/blank)."""
    try:
        con = sqlite3.connect(db or paths.db_path())
        try:
            pid = _resolve_pid(con, program_no, product_id)
            subject = _presentation(con, pid)
            if subject is None:
                return {}
            # Part D — same-family late-stage pipeline products, all sites
            d_res = _assess_against(con, subject, _pipeline_comparators(con, subject))
            d_level = grade(d_res[0]["n_distinguishing"]) if d_res else "None"
            d_box = _box(d_level)
            d_families = _families_text(subject, d_res, "in late-stage development")
            d_comments = _narrative(subject, d_res, "late-stage pipeline products", d_level, d_box)
            # Part E — one block per site, same-family products at that site
            e_sites = []
            for site_id, site_code, site_name, role in _subject_sites(con, pid):
                s_res = _assess_against(con, subject, _site_comparators(con, subject, site_id))
                lvl = grade(s_res[0]["n_distinguishing"]) if s_res else "None"
                box = _box(lvl)
                role_word = "Manufacturing" if role == "mfr" else "Packaging"
                disp = (f"{site_name} ({site_code})" if site_name and site_name != site_code else site_code)
                e_sites.append({
                    "site": f"{disp} — {role_word}",
                    "families": _families_text(subject, s_res, f"at {site_code}"),
                    "risk": box,
                    "comments": _narrative(subject, s_res, f"products at {site_code}", lvl, box),
                    "qa_label": f"Site QA - {site_code}",
                })
        finally:
            con.close()
        return {"d1_product_families": d_families, "d1_similarity_risk": d_box,
                "d2_comments": d_comments, "_e_sites": e_sites}
    except Exception:
        return {}


def _print(res):
    print(f"\n=== Similarity / mix-up risk: {res['subject']['label']} "
          f"(batch_type={res['subject']['batch_type']}) ===")
    print(f"Overall risk: {res['overall_risk']}   |   {res['n_comparators']} in-scope co-located comparator(s)")
    print(f"Rationale: {res['rationale']}\n")
    for r in res["comparators"][:8]:
        print(f"  [{r['risk_level']:>4}] {r['label']:<28} diffs={r['n_distinguishing']} "
              f"sites={', '.join(r['shared_sites']) or '-'}")
        if r["distinguishing"]:
            print(f"         distinguished by: {', '.join(r['distinguishing'])}")
    print(f"\n({res['note']})")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    program = sys.argv[1] if len(sys.argv) > 1 else "AGN-151586"
    res = assess_program(program)
    _print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
