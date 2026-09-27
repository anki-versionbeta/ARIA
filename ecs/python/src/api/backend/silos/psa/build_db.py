"""Create the SQLite DB from schema.sql and seed the template_field_map
(the data-driven map that drives populate_template.py)."""
import os, sqlite3

from . import asset_store, paths

# (part, field_label, table_index, row_index, value_col, source, resolver, placeholder_tag, notes)
TEMPLATE_FIELDS = [
    # Part A -Table 0
    ("A",  "Product Name",               0, 3, 1, "derived",    "product_name_full", None, "Smartsheet short name + TPP INN; strength held for review"),
    ("A",  "List Number",                0, 4, 1, "smartsheet", "list_number",          None, None),
    ("A",  "Form",                       0, 5, 1, "smartsheet", "form",                 None, None),
    ("A",  "Size",                       0, 6, 1, "smartsheet", "vial_container_size",  None, None),
    ("A",  "Shape",                      0, 7, 1, "smartsheet", "shape",                None, None),
    ("A",  "Marking",                    0, 8, 1, "smartsheet", "marking",              None, None),
    ("A",  "Color",                      0, 9, 1, "smartsheet", "product_color",        None, None),
    ("A",  "Photo",                      0,10, 1, "derived",    "photo", "[PENDING: PDD - product image]", "embedded image extracted from PDD"),
    # Part B -Table 0
    ("B",  "DP Manufacturing Site(s)",   0,14, 0, "derived",    "mfr_site_expand",      None, "site-code dict + PDD address pending"),
    ("B",  "DP Packaging Site(s)",       0,14, 2, "derived",    "pkging_site_expand",   None, "site-code dict"),
    # Part C -Table 1
    ("C",  "CMC Quality/PQA Name",       1, 3, 1, "execution",  None, "", None),
    ("C",  "CMC Quality/PQA Signature",  1, 3, 2, "execution",  None, "", None),
    # Part D (Table 2) — similarity vs late-stage PIPELINE products of the same dosage-form family
    # (all sites). Auto-filled as a DRAFT from the risk engine (populate_template merges
    # risk.form_fields into docattrs); an analyst assessment record OVERRIDES the draft. The D.3
    # approval Name & Signature cells stay blank (execution-time signature).
    ("D1", "Product Families (late-stage)", 2, 3, 1, "derived", "product_families_latestage", None, None),
    ("D1", "Similarity Risk (D)",           2, 4, 1, "derived", "risk_d1",     "[PENDING: similarity risk]", "Yes/No box; from risk engine or analyst record"),
    ("D2", "Similarity Risk Comments (D)",  2, 6, 0, "derived", "d2_comments", "[PENDING: D.2 comments]",    None),
    # Part E (Table 3) is rendered DYNAMICALLY — one block PER SITE (mfr + pkging) — by
    # populate_template._render_part_e (the template's single Part E table is cloned per site),
    # so its cells are NOT driven by this static map.
]

# minimal validation rules (structural; SME ranges layer on later)
VALIDATION_RULES = [
    ("list_number",         "required", "{}", "system"),
    ("form",                "required", "{}", "system"),
    ("vial_container_size", "required", "{}", "system"),
    ("product_color",       "required", "{}", "system"),
]


def main():
    # First step of every run, so it is the natural place to guarantee the writable dirs exist.
    paths.ensure_dirs()
    db = paths.db_path()
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.executescript(asset_store.read_text("schema.sql"))
    con.executemany(
        "INSERT INTO template_field_map "
        "(part, field_label, table_index, row_index, value_col, source, resolver, placeholder_tag, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)", TEMPLATE_FIELDS)
    con.executemany(
        "INSERT INTO validation_rule (field_name, rule_type, params, defined_by) VALUES (?,?,?,?)",
        VALIDATION_RULES)
    con.commit()
    n_tables = con.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    n_fields = con.execute("SELECT count(*) FROM template_field_map").fetchone()[0]
    con.close()
    print(f"Created {db}")
    print(f"  tables: {n_tables}  | template_field_map rows: {n_fields}")


if __name__ == "__main__":
    main()
