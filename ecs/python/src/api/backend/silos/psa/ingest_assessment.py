"""Ingest the human-entered assessment / sign-off record for a product
(Input_Data_Sources/Assessments/<program>.json) into the canonical DB.

These are analyst determinations + record metadata that exist in NO data source
(risk decisions, comparison narratives, OneVault ID, the human-resolved strength).
They are stored with 'Manual' provenance — rendered by the tool, not derived by it.
Signer names / e-signatures are intentionally NOT part of this file (left pending)."""
import json, os, sqlite3

from . import paths


def assess_dir():
    """Where analyst assessment records live. Ingested only when named exactly <program>.json."""
    return os.path.join(paths.root(), "Input_Data_Sources", "Assessments")

# JSON keys captured as doc_attribute rows (skip meta + the promoted strength, handled separately)
ATTR_KEYS = [
    "onevault_id", "b_manufacturing_site",
    "d1_product_families", "d1_similarity_risk", "d2_comments",
    "e1_abbvie_site", "e1_product_families", "e1_similarity_risk", "e2_comments",
]


def assessment_path(program):
    return os.path.join(assess_dir(), f"{program}.json")


def has_assessment(program):
    return os.path.exists(assessment_path(program))


def main(program="AGN-151586"):
    path = assessment_path(program)
    if not os.path.exists(path):
        print(f"No assessment file for {program} — skipping (fields stay pending).")
        return
    data = json.load(open(path, encoding="utf-8"))

    con = sqlite3.connect(paths.db_path())
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    row = cur.execute("SELECT product_id FROM product WHERE program_no=?", (program,)).fetchone()
    if not row:
        print(f"Product {program} not in DB — run ingest first."); con.close(); return
    pid = row[0]

    rel = os.path.relpath(path, paths.root())
    cur.execute(
        "INSERT INTO doc_source (product_id, doc_role, source_type, source_file, ingested_at) "
        "VALUES (?, 'ASSESSMENT', 'json', ?, datetime('now'))", (pid, rel))
    doc_id = cur.lastrowid

    n = 0
    for key in ATTR_KEYS:
        val = data.get(key)
        if val in (None, ""):
            continue
        cur.execute(
            "INSERT INTO doc_attribute (doc_id, attribute_label, attribute_key, value_text, "
            "source_locator, confidence, status) VALUES (?,?,?,?,?,?, 'mapped')",
            (doc_id, key, key, val, rel, 1.0))
        cur.execute(
            "INSERT INTO field_provenance (entity, entity_id, field_name, source_doc, source_locator, "
            "match_type, extraction_method, confidence, extracted_at) "
            "VALUES ('product', ?, ?, 'Manual', ?, 'Exact', 'manual', 1.0, datetime('now'))",
            (pid, key, rel))
        n += 1

    # resolved strength → fill the held product column + mark the conflict resolved (keep the audit row)
    strength = data.get("resolved_per_vial_strength")
    if strength:
        cur.execute("UPDATE product SET per_vial_strength=? WHERE product_id=?", (strength, pid))
        cur.execute(
            "INSERT INTO field_provenance (entity, entity_id, field_name, source_doc, source_locator, "
            "match_type, extraction_method, confidence, extracted_at) "
            "VALUES ('product', ?, 'per_vial_strength', 'Manual', ?, 'Derived', 'manual', 1.0, datetime('now'))",
            (pid, rel))
        cur.execute(
            "UPDATE validation_result SET status='resolved', "
            "message=message || ' RESOLVED: analyst set ' || ? || ' (' || ? || ').' "
            "WHERE entity_id=? AND field_name='per_vial_strength' AND status='review'",
            (strength, data.get("resolved_strength_note", "human review"), pid))

    con.commit()
    con.close()
    print(f"Assessment loaded for {program}: {n} fields + resolved strength → {strength}")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "AGN-151586")
