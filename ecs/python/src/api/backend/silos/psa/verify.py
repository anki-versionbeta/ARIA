"""Verify the Smartsheet->template slice for ANY product (no product is treated as a
baseline / golden reference). Runs DB sanity, provenance, and template checks, and
asserts that Parts A/B and D/E are populated (D/E auto-filled as a draft from the risk
engine, or from an analyst record) while Part C and the D.3/E.3 signature cells stay blank."""
import os, sqlite3, sys
from docx import Document

from . import paths


def check(cond, msg, fails):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main(program="AGN-151586"):
    fails = []
    con = sqlite3.connect(paths.db_path())
    con.row_factory = sqlite3.Row

    print("[DB sanity]")
    nprod = con.execute("SELECT count(*) FROM product").fetchone()[0]
    check(nprod > 0, f"product rows loaded ({nprod})", fails)
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    check(not fk, f"foreign-key integrity ({len(fk)} violations)", fails)

    p = con.execute("SELECT * FROM product WHERE program_no=?", (program,)).fetchone()
    check(p is not None, f"selected product {program} present", fails)

    print("[Provenance coverage]")
    if p:
        nprov = con.execute(
            "SELECT count(*) FROM field_provenance WHERE entity='product' AND entity_id=?",
            (p["product_id"],)).fetchone()[0]
        check(nprov > 0, f"{program} has provenance rows ({nprov})", fails)
    con.close()

    print("[Generated docx]")
    gen = os.path.join(paths.output_dir(), f"{program}_PSA_generated.docx")
    if not os.path.exists(gen):
        check(False, f"generated file exists ({gen})", fails)
        print(f"\n{'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
        return 0 if not fails else 1

    d = Document(gen)
    t0 = d.tables[0]
    # Part A / B are populated from the Smartsheet (+ any uploaded PDD/TPP/PPT)
    check(bool(t0.rows[3].cells[1].text.strip()), "Part A Product Name populated", fails)
    check(bool(t0.rows[5].cells[1].text.strip()), "Part A Form cell populated", fails)
    check(bool(t0.rows[6].cells[1].text.strip()), "Part A Size cell populated", fails)
    check(bool(t0.rows[9].cells[1].text.strip()), "Part A Color cell populated", fails)
    check(bool(t0.rows[14].cells[2].text.strip()), "Part B packaging populated", fails)
    # Part A photo: an embedded image when the Smartsheet has one, otherwise blank (no placeholder)
    photo_cell = t0.rows[10].cells[1].text.strip()
    check(photo_cell == "" or "[PENDING" in photo_cell or len(d.inline_shapes) >= 1,
          "Part A photo: image embedded or blank when none", fails)

    # Part C name/signature cells are left blank
    check(d.tables[1].rows[3].cells[1].text.strip() == "", "Part C Name blank", fails)
    check(d.tables[1].rows[3].cells[2].text.strip() == "", "Part C Signature/Date blank", fails)

    # Parts D and E are now auto-filled (DRAFT from the risk engine, or an analyst record):
    # the family/comment value cells must be populated and exactly one similarity-risk box marked
    # per part. The D.3 / E.3 approval Name & Signature cells stay blank (execution-time signatures).
    print("[Parts D & E populated]")
    td = d.tables[2]
    check(bool(td.rows[3].cells[1].text.strip()), "D.1 Product Families populated", fails)
    check(bool(td.rows[6].cells[0].text.strip()), "D.2 Comments populated", fails)
    check(td.rows[10].cells[1].text.strip() == "", "D.3 Name blank (signature)", fails)
    check(td.rows[10].cells[2].text.strip() == "", "D.3 Signature/Date blank", fails)
    d_text = "\n".join(c.text for r in td.rows for c in r.cells)
    check("☒" in d_text, "D similarity-risk box marked (Yes or No)", fails)

    # Part E is rendered one block PER SITE (QPP11-04-001-G004 §3: "each AbbVie manufacturing
    # site should complete a separate Part E"). The template's single Part E table (index 3) is
    # cloned per site, so every table from index 3 onward is a Part E block — validate them all.
    e_tables = d.tables[3:]
    check(len(e_tables) >= 1, "at least one Part E site block present", fails)
    for i, te in enumerate(e_tables, 1):
        tag = f"E block {i}"
        check(bool(te.rows[3].cells[1].text.strip()), f"{tag}: AbbVie Site populated", fails)
        check(bool(te.rows[4].cells[1].text.strip()), f"{tag}: Product Families populated", fails)
        check(bool(te.rows[7].cells[0].text.strip()), f"{tag}: Comments populated", fails)
        check(te.rows[11].cells[1].text.strip() == "", f"{tag}: E.3 Name blank (signature)", fails)
        check(te.rows[11].cells[2].text.strip() == "", f"{tag}: E.3 Signature/Date blank", fails)
        e_text = "\n".join(c.text for r in te.rows for c in r.cells)
        check("☒" in e_text, f"{tag}: similarity-risk box marked (Yes or No)", fails)

    print(f"\n{'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "AGN-151586"))
