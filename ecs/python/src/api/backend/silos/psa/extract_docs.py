"""Extract PDD/TPP data with LOCAL text parsing (PyMuPDF/fitz) — no API, no key.

For a product, read its PDD and TPP PDFs, pull the target fields by anchored regex,
record them losslessly in doc_source/doc_attribute + field_provenance, and promote
the auto-fillable ones into product columns. The 1500-vs-1400 U/vial strength conflict
is NOT auto-filled — it is flagged for review (validation_result + doc_attribute status).

Usage:
  python -m psa.extract_docs --dump AGN-151586     # print raw PDF text (tune patterns)
  python -m psa.extract_docs AGN-151586            # extract + load into psa.db
"""
import os, re, sqlite3, sys
import fitz  # PyMuPDF

from . import paths

# product → its source documents (only BoNT/E has files today; extend as more arrive).
# Optional "photo": {"page": N, "index": M} picks a specific PDD embedded image (1-based page);
# omit it and the largest image on any page is used.
REGISTRY = {
    "AGN-151586": {
        "PDD": "Input_Data_Sources/PDDs/Product Definition 3 for BoNTE.pdf",
        "TPP": "Input_Data_Sources/TPPs/CMC TPP for BoNTE.pdf",
        "photo": {"page": 7, "index": 1},   # BoNT/E vial image (148×384) — chosen from --images
    },
}

# the signed reference output (for --dump-pdf, to transcribe verbatim assessment text)
EXAMPLE_PDF = "Output_Files/BoNTE AP16 Product Similarity Assessment.pdf"


def has_docs(program):
    return program in REGISTRY


def _pages(path):
    doc = fitz.open(path)
    try:
        return [doc[i].get_text() for i in range(doc.page_count)]
    finally:
        doc.close()


def _find(pattern, text, group=1, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def _page_of(pages, needle):
    """1-based page number where `needle` (regex) first appears, or None."""
    for i, t in enumerate(pages, 1):
        if re.search(needle, t, re.I):
            return i
    return None


def _strength(text):
    """Per-vial strength, tolerant of thousands separators ('1,500' / '1 500' / '1500')."""
    m = re.search(r"(\d{1,2}[,\s]?\d{3}|\d{3,4})\s*U(?:nits)?\s*(?:/|per)\s*vial", text, re.I)
    return f"{re.sub(r'[^0-9]', '', m.group(1))} U/vial" if m else None


# ---------------------------------------------------------------------------
# Field extractors.  Regexes are isolated here and tuned to the real PDF text
# (run `--dump` first).  Each returns dict: key -> {label, value, page, conf}.
# ---------------------------------------------------------------------------
def extract_tpp(pages):
    text = "\n".join(pages)
    out = {}

    inn = _find(r"\b(Trenibotulinumtoxin\s*[A-Z]?)\b", text)
    if inn:
        out["inn_name"] = {"label": "INN / Nonproprietary name", "value": re.sub(r"\s+", "", inn),
                           "page": _page_of(pages, r"Trenibotulinumtoxin"), "conf": 0.9}

    strength = _strength(text)
    if strength:
        out["per_vial_strength"] = {"label": "Per-vial strength", "value": strength,
                                    "page": _page_of(pages, r"\d[\d,\s]*U(?:nits)?\s*(?:/|per)\s*vial"),
                                    "conf": 0.85}

    # prefer a clean person-name capture ending in "Duffy" (avoids grabbing "Approved by" / newlines);
    # fall back to the name preceding "Director, CMC Product Development" (spaces only, no line crossing)
    approver = _find(r"\b([A-Z][a-z]+(?:\s+\(?[A-Z][a-z.]+\)?){0,2}\s+Duffy)\b", text)
    if not approver:
        approver = _find(r"([A-Z][a-z]+(?:[ ]+[A-Z][a-z.()]+){1,3})[ ]*,?[ ]*Director,?[ ]*CMC[ ]+Product[ ]+Development", text)
    if approver:
        approver = re.sub(r"\s+", " ", approver).strip()
        out["pd_director_name"] = {"label": "CMC Product Development Director", "value": approver,
                                   "page": _page_of(pages, r"Duffy|CMC\s+Product\s+Development"), "conf": 0.75}
    return out


def extract_pdd(pages):
    text = "\n".join(pages)
    out = {}

    # manufacturing-site address: capture the line(s) around Westport / County Mayo
    # address block: an optional company line (AbbVie/Allergan) + "Castlebar Road … Ireland"
    # (the PDD lays the address across ~3 short lines)
    m = re.search(r"((?:AbbVie|Allergan)[^\n]*\n)?(Castlebar Road[\s\S]{0,80}?Ireland)", text, re.I)
    addr = ((m.group(1) or "") + m.group(2)) if m else _find(
        r"([^\n]*(?:Westport|Castlebar Road|County Mayo)[^\n]*Ireland)", text)
    if addr:
        out["mfr_site_address"] = {"label": "DP manufacturing-site address",
                                   "value": re.sub(r"\s*\n\s*", ", ", addr).strip().strip(","),
                                   "page": _page_of(pages, r"Castlebar Road|Westport|County Mayo"),
                                   "conf": 0.8}

    loc = _find(r"(Lake County,\s*Illinois|North Chicago,?\s*Illinois|North Chicago,?\s*IL)", text)
    if loc:
        out["site_location"] = {"label": "US site location", "value": loc,
                                "page": _page_of(pages, r"Illinois|North Chicago"), "conf": 0.7}

    strength = _strength(text)
    if strength:
        out["pdd_strength"] = {"label": "Per-vial strength (PDD)", "value": strength,
                               "page": _page_of(pages, r"\d[\d,\s]*U(?:nits)?\s*(?:/|per)\s*vial"), "conf": 0.85}
    return out


EXTRACTORS = {"PDD": extract_pdd, "TPP": extract_tpp}


# ---------------------------------------------------------------------------
# Generic, format-agnostic Part A/B extraction (for products NOT in the Smartsheet).
# Best-effort "Label: value" scan over the plain text of any PDD/TPP/PPT/DOCX. Fields not
# found are simply omitted → the template renders its [PENDING]/TBD. Never fabricates.
# Keys map 1:1 onto `product` columns the A/B resolvers read.
# ---------------------------------------------------------------------------
AB_LABEL_PATTERNS = {
    "program_name":        [r"Product\s*Name", r"Compound\s*Name", r"Molecule\s*Name", r"Drug\s*Product\s*Name", r"\bMolecule\b"],
    "form":                [r"Dosage\s*Form(?:\s*Type)?", r"Drug\s*Product\s*Type", r"Product\s*Type", r"\bForm\b"],
    "strength":            [r"Strength\s*\(s\)", r"\bStrength\b", r"\bConcentration\b", r"Dose\s*Strength"],
    "route_of_admin":      [r"Route\s*of\s*Administration", r"\bRoute\b"],
    "vial_container_size": [r"Container[\s/]*Closure", r"\bPresentation\b", r"Vial\s*(?:Container\s*)?Size", r"Container\s*Size", r"Fill\s*Volume"],
    "product_color":       [r"Colou?r\s*\(s\)", r"\bColou?r\b"],
    "shape":               [r"\bShape\b"],
    "marking":             [r"\bMarking\b", r"\bDebossing\b", r"\bImprint\b"],
    "dp_mfr_site_raw":     [r"(?:DP\s*)?Manufacturing\s*Site", r"Drug\s*Product\s*Manufactur\w*\s*Site", r"Mfg\s*Site"],
    "dp_pkging_site_raw":  [r"Packaging\s*Site", r"Pkg\s*Site"],
}


def extract_generic_ab(text):
    """Best-effort Part A/B field extraction from arbitrary document text.
    Returns {product_column: value}; unfound fields are omitted (→ [PENDING]/TBD)."""
    out = {}
    if not text:
        return out
    joined = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    for col, labels in AB_LABEL_PATTERNS.items():
        for lab in labels:
            m = re.search(rf"{lab}\s*[:\-–—]\s*(.+)", joined, re.I)
            if not m:
                continue
            val = re.split(r"\s{2,}|\t", m.group(1).strip())[0].strip(" :\t-–—|")
            if 1 <= len(val) <= 120 and not re.fullmatch(r"[\W_]+", val):
                out[col] = val
                break
    return out


def extract_mfr_address(text):
    """Best-effort, format-agnostic: find a drug-product MANUFACTURING SITE address near a
    manufacturing anchor in the document text. Conservative — only returns a capture that both has a
    comma AND an address cue (street type / country / postal code); otherwise None so the caller
    falls back to the Smartsheet site code (never a wrong address on a governance form)."""
    if not text:
        return None
    m = re.search(
        r"(?:drug\s+product\s+)?(?:manufactur\w*\s+(?:site|location|facility|address)|"
        r"site\s+of\s+manufactur\w*|manufactured\s+(?:at|by)|DP\s+manufactur\w*\s+site)"
        r"[^\n:]{0,40}[:\-]?\s*(.{10,220})", text, re.I | re.S)
    if not m:
        return None
    cand = re.split(r"\n\s*\n", m.group(1))[0]              # stop at the first blank line
    cand = re.sub(r"\s*\n\s*", ", ", cand)                  # join wrapped address lines
    cand = re.sub(r"\s{2,}", " ", cand).strip(" ,;:\t")
    cue = re.search(
        r"\b(road|street|st\.|ave|avenue|drive|dr\.|lane|ln\.|blvd|boulevard|way|highway|plaza|"
        r"county|ireland|germany|switzerland|singapore|puerto\s+rico|united\s+states|u\.?s\.?a|"
        r"[A-Z]{2}\s*\d{5}|\d{5}(?:-\d{4})?)\b", cand, re.I)
    if cand.count(",") >= 1 and cue and 12 <= len(cand) <= 200:
        return cand
    return None


def _doc_files(program):
    """(role, relpath) pairs for actual document files (skips the 'photo' config entry)."""
    return [(r, v) for r, v in REGISTRY.get(program, {}).items() if isinstance(v, str)]


def dump(program):
    if not _doc_files(program):
        print(f"No PDD/TPP files registered for {program}"); return
    for role, rel in _doc_files(program):
        path = os.path.join(paths.root(), rel)
        print(f"\n########## {role}: {rel} ##########")
        if not os.path.exists(path):
            print("  (file not found)"); continue
        for i, t in enumerate(_pages(path), 1):
            print(f"\n----- page {i} -----\n{t}")


def dump_pdf(rel_or_abs):
    """Dump text of any PDF by path (e.g. the signed example) for verbatim transcription."""
    path = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(paths.root(), rel_or_abs)
    if not os.path.exists(path):
        print(f"  (file not found: {path})"); return
    for i, t in enumerate(_pages(path), 1):
        print(f"\n----- page {i} -----\n{t}")


def list_images(program):
    """List every embedded image in the product's PDD (page, index, dimensions) to pick the photo."""
    files = dict(REGISTRY.get(program, {}))
    pdd = files.get("PDD")
    if not pdd:
        print(f"No PDD registered for {program}"); return
    doc = fitz.open(os.path.join(paths.root(), pdd))
    try:
        print(f"PDD images in {pdd}:")
        for pno in range(doc.page_count):
            for idx, img in enumerate(doc.get_page_images(pno, full=True)):
                print(f"  page {pno + 1}  index {idx}  xref={img[0]}  {img[2]}x{img[3]}px  name={img[7]}")
        print('\nSet REGISTRY["%s"]["photo"] = {"page": P, "index": I} to pick one (else largest is used).' % program)
    finally:
        doc.close()


def extract_pdd_photo(program, pdd_rel=None):
    """Save the chosen PDD image to Output_Files/assets/<program>_photo.png; return the relative path.
    `pdd_rel` overrides the registry PDD path (used when the PDD was uploaded)."""
    files = dict(REGISTRY.get(program, {}))
    pdd = pdd_rel or files.get("PDD")
    if not pdd:
        return None
    doc = fitz.open(os.path.join(paths.root(), pdd))
    try:
        sel = files.get("photo")
        if sel:                                   # explicit page/index
            xref = doc.get_page_images(sel["page"] - 1, full=True)[sel["index"]][0]
        else:                                     # default: largest image by pixel area
            best = None
            for pno in range(doc.page_count):
                for img in doc.get_page_images(pno, full=True):
                    area = img[2] * img[3]
                    if not best or area > best[0]:
                        best = (area, img[0])
            if not best:
                return None
            xref = best[1]
        pix = fitz.Pixmap(doc, xref)
        if pix.n - pix.alpha >= 4:        # CMYK → RGB so python-docx can embed it
            pix = fitz.Pixmap(fitz.csRGB, pix)
        # Absolute, like the Smartsheet images in ingest_smartsheet_api: populate_template accepts
        # either, and an absolute path stays correct when the output dir is relocated.
        assets = paths.image_dir()
        os.makedirs(assets, exist_ok=True)
        out = os.path.join(assets, f"{program}_photo.png")
        pix.save(out)
        return out
    finally:
        doc.close()


def extract_and_load(program, con, docs=None):
    # docs: optional {role: relpath} of UPLOADED files; defaults to the registry/discovery set.
    items = list(docs.items()) if docs else _doc_files(program)
    if not items:
        print(f"No PDD/TPP files for {program} — skipping document extraction."); return
    cur = con.cursor()
    row = cur.execute("SELECT product_id FROM product WHERE program_no=?", (program,)).fetchone()
    if not row:
        print(f"Product {program} not in DB — run ingest first."); return
    pid = row[0]

    strengths = {}          # role -> "NNNN U/vial"
    promote = {}            # product column -> (value, source_doc, locator, conf, match_type)
    pdd_doc_id = None
    pdd_rel = None
    all_text = []           # accumulated document text (for the generic mfr-address fallback)

    for role, rel in items:
        path = os.path.join(paths.root(), rel)
        if not os.path.exists(path):
            print(f"  {role} file missing: {rel}"); continue
        if os.path.splitext(path)[1].lower() != ".pdf":
            # the anchored regex extractors + _pages() are PDF-only; non-PDF enrichment is skipped
            # (new-product A&B extraction handles pptx/docx via workflow.load_new_product).
            print(f"  {role}: {os.path.basename(rel)} is not a PDF — skipping regex enrichment."); continue
        pages = _pages(path)
        all_text.append("\n".join(pages))
        cur.execute(
            "INSERT INTO doc_source (product_id, doc_role, source_type, source_file, ingested_at) "
            "VALUES (?,?,?,?,datetime('now'))", (pid, role, "pdf", rel))
        doc_id = cur.lastrowid
        if role == "PDD":
            pdd_doc_id = doc_id
            pdd_rel = rel
        fields = EXTRACTORS[role](pages)
        for key, f in fields.items():
            status = "mapped"
            cur.execute(
                "INSERT INTO doc_attribute (doc_id, attribute_label, attribute_key, value_text, "
                "source_locator, confidence, status) VALUES (?,?,?,?,?,?,?)",
                (doc_id, f["label"], key, f["value"], f"p{f['page']}", f["conf"], status))
            if key in ("per_vial_strength", "pdd_strength"):
                strengths[role] = f["value"]
            # auto-fillable product columns
            if key in ("inn_name", "mfr_site_address", "site_location"):
                promote[key] = (f["value"], role, f"p{f['page']}", f["conf"], "Transformed")
        print(f"  {role}: extracted {list(fields)} ({len(pages)} pp)")

    # ---- strength conflict handling (flag for review, do NOT auto-fill) ----
    tpp_s, pdd_s = strengths.get("TPP"), strengths.get("PDD")
    if tpp_s and pdd_s and tpp_s != pdd_s:
        cur.execute(
            "INSERT INTO validation_result (entity, entity_id, field_name, status, severity, message, validated_at) "
            "VALUES ('product', ?, 'per_vial_strength', 'review', 'warning', ?, datetime('now'))",
            (pid, f"Strength conflict: TPP {tpp_s} vs PDD {pdd_s} — held for review, not auto-filled."))
        print(f"  STRENGTH CONFLICT: TPP {tpp_s} vs PDD {pdd_s} — flagged for review (per_vial_strength left NULL).")
    elif tpp_s or pdd_s:
        promote["per_vial_strength"] = (tpp_s or pdd_s, "TPP" if tpp_s else "PDD", "", 0.85, "Exact")

    # ---- generic DP manufacturing-site address (any product) when no role extractor found one ----
    if "mfr_site_address" not in promote:
        addr = extract_mfr_address("\n".join(all_text))
        if addr:
            promote["mfr_site_address"] = (addr, "PDD", "generic-parse", 0.55, "Transformed")
            print(f"  generic mfr-site address extracted: {addr!r}")

    # ---- promote auto-fillable columns + provenance ----
    for col, (val, src, loc, conf, mt) in promote.items():
        cur.execute(f"UPDATE product SET {col}=? WHERE product_id=?", (val, pid))
        cur.execute(
            "INSERT INTO field_provenance (entity, entity_id, field_name, source_doc, source_locator, "
            "match_type, extraction_method, confidence, extracted_at) "
            "VALUES ('product', ?, ?, ?, ?, ?, 'local-parse', ?, datetime('now'))",
            (pid, col, src, loc, mt, conf))

    # ---- product image: per user decision the form photo comes ONLY from the Smartsheet (never the
    #      PDD). PDD photo extraction is intentionally disabled; extract_pdd_photo() + the --images
    #      helper remain defined if this is ever reinstated. ----
    _ = (pdd_doc_id, pdd_rel)   # retained above only for the (now disabled) PDD-photo path

    con.commit()
    print(f"  promoted to product columns: {list(promote)}")


def main(program="AGN-151586"):
    if not has_docs(program):
        print(f"No PDD/TPP for {program} — skipping document extraction."); return
    con = sqlite3.connect(paths.db_path())
    con.execute("PRAGMA foreign_keys = ON")
    extract_and_load(program, con)
    con.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--dump":
        dump(args[1] if len(args) > 1 else "AGN-151586")
    elif args and args[0] == "--images":
        list_images(args[1] if len(args) > 1 else "AGN-151586")
    elif args and args[0] == "--dump-pdf":
        dump_pdf(args[1] if len(args) > 1 else EXAMPLE_PDF)
    else:
        main(args[0] if args else "AGN-151586")
