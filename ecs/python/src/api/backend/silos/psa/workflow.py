"""Document-driven workflow engine.

Given one or more uploaded documents (PDD / TPP / PPT), this:
  1. builds the DB and ingests the Smartsheet (the product catalogue),
  2. IDENTIFIES the product from the documents' text (match against the catalogue),
  3. extracts the uploaded documents, loads the assessment file (if any),
  4. populates the PSA template, and
  5. verifies the result.

It wires together the existing modules (build_db, ingest_smartsheet, extract_docs,
ingest_assessment, populate_template, verify) — it does not re-implement them.
The UI calls process_documents(); it also runs standalone from the CLI.

The document-list parameters are named `docs`, not `paths`: `paths` is the module that resolves where to
read and write, and a parameter of that name silently shadows it inside the function."""
import contextlib, io, os, re, sqlite3, sys

from . import (build_db, extract_docs, ingest_assessment, ingest_smartsheet,
               ingest_smartsheet_api, paths, populate_template, risk, scope, verify)


# ---------------------------------------------------------------- text reading
def read_text(path):
    """Plain text of a PDF / DOCX / PPTX, used to identify the product."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import fitz
            d = fitz.open(path)
            try:
                return "\n".join(d[i].get_text() for i in range(d.page_count))
            finally:
                d.close()
        if ext == ".docx":
            from docx import Document
            doc = Document(path)
            parts = [p.text for p in doc.paragraphs]
            for t in doc.tables:
                for r in t.rows:
                    parts += [c.text for c in r.cells]
            return "\n".join(parts)
        if ext == ".pptx":
            from pptx import Presentation
            parts = []
            for s in Presentation(path).slides:
                for sh in s.shapes:
                    if sh.has_text_frame:
                        parts.append(sh.text_frame.text)
                    if sh.has_table:
                        for r in sh.table.rows:
                            parts += [c.text for c in r.cells]
            return "\n".join(parts)
    except Exception as e:        # never let a bad file kill identification
        return f"(could not read {os.path.basename(path)}: {e})"
    return ""


def classify_role(path):
    """Map an uploaded file to a document role for the extractors."""
    name = os.path.basename(path).lower()
    if "tpp" in name:
        return "TPP"
    return "PDD"           # Product Definition / .pptx / .docx default to the PDD slot


# ---------------------------------------------------------------- identification
# A confident catalogue match must be backed by the SUBJECT's program code appearing in the
# filename / title (authoritative), or a clear name match there — never by comparator names
# mentioned only in the body (which caused the Gate-Review-deck mis-identifications).
CODE_RE = re.compile(r"(ABBV-CLS-\d{2,6}|ABBV-\d{2,6}|AGN-\d{2,6}|ABT-\d{2,6}|RGX-\d{2,6})", re.I)
FILENAME_WEIGHT = 100      # a hit in the filename counts 100x a body mention
TITLE_WEIGHT = 8           # a hit on the first page / slide (title) counts 8x
MIN_SCORE = 12             # body-only confidence floor
MARGIN = 1.6               # winner must beat the runner-up by this factor (body-only path)


def _norm(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def _tokens(program_no, program_name):
    toks = {_norm(program_no)} if program_no else set()
    if program_name:
        toks.add(_norm(program_name))
        toks.update(_norm(w) for w in re.split(r"[^A-Za-z0-9]+", program_name))
    return {t for t in toks if len(t) >= 4}


def _codes_in(text):
    """Program codes (denormalized, upper-cased) found in raw text, in order, de-duplicated."""
    seen, out = set(), []
    for m in CODE_RE.finditer(text or ""):
        c = m.group(1).upper()
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def list_products(con):
    return [(r[0], r[1]) for r in con.execute(
        "SELECT program_no, MAX(program_name) FROM product "
        "WHERE program_no IS NOT NULL AND TRIM(program_no) <> '' "
        "GROUP BY program_no ORDER BY program_no")]


def _read_docs(docs):
    """Read each doc once; return raw filename/title/body blobs + their normalized forms."""
    raw_names = " ".join(os.path.basename(p) for p in docs)
    titles, body = [], []
    for p in docs:
        t = read_text(p)
        body.append(t)
        titles.append((t or "")[:800])          # first page / slide ~= title area
    raw_titles, raw_body = "\n".join(titles), "\n".join(body)
    return {"raw_names": raw_names, "raw_titles": raw_titles, "raw_body": raw_body,
            "fn": _norm(raw_names), "title": _norm(raw_titles), "body": _norm(raw_body)}


def _score_products(con, d):
    scored = []
    for program_no, name in list_products(con):
        code = _norm(program_no)
        name_toks = {t for t in _tokens(program_no, name) if t != code}
        s, code_fn, code_title, name_fn = 0, False, False, False
        if code:
            code_fn = code in d["fn"]
            code_title = code in d["title"]
            s += d["fn"].count(code) * len(code) * FILENAME_WEIGHT
            s += d["title"].count(code) * len(code) * TITLE_WEIGHT
            s += d["body"].count(code) * len(code)
        for t in name_toks:
            if t in d["fn"]:
                name_fn = True
            s += d["fn"].count(t) * len(t) * FILENAME_WEIGHT
            s += d["title"].count(t) * len(t) * TITLE_WEIGHT
            s += d["body"].count(t) * len(t)
        if s:
            scored.append({"score": s, "program_no": program_no, "name": name,
                           "strong": code_fn or code_title or name_fn})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def identify(docs, con):
    """Decide the subject product from the uploaded document(s).
    Returns {decision: 'smartsheet'|'new_product'|'undetermined', program_no, name,
             subject_code, alternatives:[(score,program_no,name)...], reason}."""
    d = _read_docs(docs)
    cat_codes = {_norm(pn): (pn, nm) for pn, nm in list_products(con)}
    scored = _score_products(con, d)
    alts = [(r["score"], r["program_no"], r["name"]) for r in scored[:4]]

    if len(re.sub(r"\s", "", (d["raw_body"] or "").replace("(could not read", ""))) < 20:
        return {"decision": "undetermined", "program_no": None, "name": None,
                "subject_code": None, "alternatives": alts, "reason": "unreadable"}

    # 1) A program code in the filename / title is authoritative for the SUBJECT product.
    subj = _codes_in(d["raw_names"]) or _codes_in(d["raw_titles"])
    if subj:
        code = subj[0]
        if _norm(code) in cat_codes:
            pn, nm = cat_codes[_norm(code)]
            return {"decision": "smartsheet", "program_no": pn, "name": nm,
                    "subject_code": code, "alternatives": alts, "reason": "code-in-filename"}
        return {"decision": "new_product", "program_no": code, "name": None,
                "subject_code": code, "alternatives": alts, "reason": "code-in-filename-not-in-catalogue"}

    # 2) No code named — accept a scored match only if it is anchored in the filename/title
    #    AND clearly beats the runner-up (guards against comparator-name collisions).
    top = scored[0] if scored else None
    second = scored[1]["score"] if len(scored) > 1 else 0
    if top and top["strong"] and top["score"] >= MIN_SCORE and top["score"] >= MARGIN * second:
        return {"decision": "smartsheet", "program_no": top["program_no"], "name": top["name"],
                "subject_code": None, "alternatives": alts, "reason": "name-in-filename"}

    # 3) A code appears in the body only and is not in the catalogue → treat as a new product.
    for c in _codes_in(d["raw_body"]):
        if _norm(c) not in cat_codes:
            return {"decision": "new_product", "program_no": c, "name": None,
                    "subject_code": c, "alternatives": alts, "reason": "code-in-body-not-in-catalogue"}

    return {"decision": "undetermined", "program_no": None, "name": None,
            "subject_code": None, "alternatives": alts, "reason": "low-confidence"}


# ---------------------------------------------------------------- orchestration
def _rel(path):
    ap, ar = os.path.abspath(path), os.path.abspath(paths.root())
    return os.path.relpath(ap, ar) if ap.startswith(ar) else ap


def _pick_presentation(program_no, docs, con):
    """When several Smartsheet rows share the same program code (multiple presentations), pick the
    one whose DISTINGUISHING attributes best match the uploaded document — the document is the
    baseline. Returns (product_id, n_rows). Falls back to the first row (source_row order) when the
    document gives no distinguishing signal (tie)."""
    rows = con.execute(
        "SELECT product_id, source_row, strength, vial_container_size, target_fill_volume_ml, "
        "batch_type, product_color, form FROM product WHERE program_no=? ORDER BY source_row",
        (program_no,)).fetchall()
    # exclude clinical-stage presentations from auto-selection (out of scope); fall back to all
    # rows only if EVERY presentation is clinical, so an all-clinical program still generates.
    rows = [r for r in rows if scope.is_in_scope(r[5])] or rows
    if len(rows) <= 1:
        return (rows[0][0] if rows else None), len(rows)
    doc = _norm("\n".join(read_text(p) for p in docs))

    def row_tokens(r):
        toks = set()
        for val in r[2:]:                                   # the 6 distinguishing attribute values
            for cand in [_norm(val)] + [_norm(p) for p in re.split(r"[^A-Za-z0-9]+", str(val or ""))]:
                # keep alphanumeric codes that contain a LETTER ('2R','10R','20MG','COMMERCIAL','DW6032');
                # drop bare numbers ('20','10','200') that collide with years / page numbers in the doc
                if len(cand) >= 2 and re.search(r"[A-Z]", cand):
                    toks.add(cand)
        return toks

    per_row = [(r, row_tokens(r)) for r in rows]
    freq = {}
    for _r, toks in per_row:
        for t in toks:
            freq[t] = freq.get(t, 0) + 1
    # a token distinguishes only if it is NOT shared by every row (drops 'VIAL', a common colour, ...)
    distinguishing = {t for t, c in freq.items() if c < len(rows)}

    best_id, best_score, best_src = rows[0][0], -1, rows[0][1]
    print(f">> {program_no}: {len(rows)} presentations - scoring DISTINGUISHING attributes against "
          f"the uploaded document (the baseline):")
    for r, toks in per_row:
        pid, src = r[0], r[1]
        score, hits = 0, []
        for t in sorted(toks & distinguishing):
            c = doc.count(t)
            if c:
                score += c * len(t)
                hits.append(f"{t}x{c}")
        print(f"     source_row {src} (product_id {pid}): score {score}"
              + (f" - matched {', '.join(hits)}" if hits else " - no distinguishing match"))
        if score > best_score:
            best_score, best_id, best_src = score, pid, src
    print(f"     -> selected product_id {best_id} (source_row {best_src})"
          + ("" if best_score > 0 else " - no distinguishing signal in the document; defaulted to first row"))
    return best_id, len(rows)


def list_presentations(program_no, con):
    """All Smartsheet rows for a program with a human label, for the dropdown / --presentation flag.
    Keys are STABLE across runs (source_row / list_number); product_id is NOT (the DB is rebuilt each run)."""
    rows = con.execute(
        "SELECT product_id, source_row, strength, vial_container_size, list_number, batch_type "
        "FROM product WHERE program_no=? ORDER BY source_row", (program_no,)).fetchall()
    out = []
    for pid, src, strength, size, listno, batch in rows:
        if not scope.is_in_scope(batch):     # hide clinical-stage presentations from the picker
            continue
        parts = [str(x).strip() for x in (strength, size, listno, batch) if x and str(x).strip()]
        out.append({"product_id": pid, "source_row": src, "list_number": listno,
                    "label": " / ".join(parts) or f"row {src}"})
    return out


def _match_presentation(presentations, sel):
    """Resolve a user selector (source_row or list_number) to this run's product_id; None if unmatched."""
    if sel is None or str(sel).strip() == "":
        return None
    s = _norm(sel)
    for pr in presentations:
        if str(pr["source_row"]) == str(sel).strip() or (pr["list_number"] and _norm(pr["list_number"]) == s):
            return pr["product_id"]
    return None


def load_new_product(program_no, docs, con):
    """Insert a transient product row for a subject NOT in the Smartsheet, filling Part A/B
    columns via best-effort extraction from the uploaded document(s). Records provenance
    (source_doc = PPT/PDD/TPP). docs: {role: relpath}. Returns product_id. Never fabricates —
    unfound fields are simply left NULL and render as the template's [PENDING]/TBD."""
    cur = con.cursor()
    files = []
    for role, rel in docs.items():
        txt = read_text(os.path.join(paths.root(), rel))
        ext = os.path.splitext(rel)[1].lower().lstrip(".") or "doc"
        src = "PPT" if ext == "pptx" else ("TPP" if role == "TPP" else "PDD")
        files.append((role, rel, ext, src, txt))
    combined = "\n".join(f[4] for f in files)
    fields = extract_docs.extract_generic_ab(combined)
    name = fields.pop("program_name", None) or program_no
    if "dp_mfr_site_raw" not in fields:                 # try the full manufacturing-site address too
        addr = extract_docs.extract_mfr_address(combined)
        if addr:
            fields["mfr_site_address"] = addr
    primary_src = next((s for (_r, _rel, _e, s, _t) in files if s != "TPP"),
                       files[0][3] if files else "PDD")

    cols = {"program_no": program_no, "program_name": name}
    cols.update(fields)
    keys = list(cols.keys())
    cur.execute(f"INSERT INTO product ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [cols[k] for k in keys])
    pid = cur.lastrowid
    for role, rel, ext, _src, _txt in files:
        cur.execute("INSERT INTO doc_source (product_id, doc_role, source_type, source_file, ingested_at) "
                    "VALUES (?,?,?,?,datetime('now'))", (pid, role, ext, rel))
    for f in ["program_no", "program_name"] + list(fields.keys()):
        cur.execute(
            "INSERT INTO field_provenance (entity, entity_id, field_name, source_doc, source_locator, "
            "match_type, extraction_method, confidence, extracted_at) "
            "VALUES ('product', ?, ?, ?, 'document', 'Transformed', 'local-parse', 0.5, datetime('now'))",
            (pid, f, primary_src))
    con.commit()
    print(f"  new-product row for {program_no}: extracted {list(fields) or 'nothing -> all [PENDING]/TBD'}")
    return pid


def process_documents(docs, program_override=None, presentation=None, rebuild=True, photo=None):
    """End-to-end: build → ingest (LIVE Smartsheet) → identify → fill A&B (Smartsheet or document)
    → populate → verify. Returns a result dict the UI/CLI can render.
    `presentation` (source_row or list_number) forces a specific presentation for multi-row products;
    when omitted the best match is auto-picked and the caller may override afterward."""
    log = io.StringIO()
    info = {}
    with contextlib.redirect_stdout(log):
        # `rebuild=False` means the caller has already populated psa.db and this run must not go back to
        # Smartsheet — the ARIA `report` stage, which rebuilt from the run's stored snapshot so that the
        # report describes exactly the catalogue the recommendation did.
        if rebuild:
            build_db.main()
            if ingest_smartsheet_api.available():
                print(">> Smartsheet source: LIVE API")
                ingest_smartsheet_api.main()
            else:
                print(">> WARNING: live Smartsheet not configured (set SMARTSHEET_ACCESS_TOKEN + "
                      "SMARTSHEET_SHEET_ID). Falling back to the STALE .xlsx export — "
                      "product data and images may be out of date.")
                ingest_smartsheet.main()
        else:
            print(">> Smartsheet source: the run's stored snapshot (already loaded; no live read)")

        con = sqlite3.connect(paths.db_path())
        cat_codes = {_norm(pn) for pn, _ in list_products(con)}

        if program_override:
            program = program_override
            decision = "smartsheet" if _norm(program) in cat_codes else "new_product"
            subject_code = program
        else:
            info = identify(docs, con)
            decision = info["decision"]
            if decision == "undetermined":
                cands = list_products(con)
                con.close()
                msg = ("The uploaded document(s) could not be read (they may be scanned / image-only) — "
                       "please provide a text-based PDF/DOCX/PPTX."
                       if info.get("reason") == "unreadable" else
                       "Could not confidently identify the product from the uploaded document(s). "
                       "Pick the product manually (or enter its code) and generate again.")
                return {"status": "needs_override", "message": msg, "candidates": cands,
                        "subject_code": info.get("subject_code"),
                        "alternatives": info.get("alternatives", []), "log": log.getvalue()}
            program = info["program_no"]
            subject_code = info.get("subject_code") or program

        # {role: repo-relative path} for the extractors. A SEPARATE name from the `docs` parameter on
        # purpose: this used to be built from a parameter called `paths`, and renaming that parameter to
        # `docs` (because it shadowed the `paths` module) silently collided with this dict — `docs = {}`
        # destroyed the caller's file list and `for p in docs` then iterated an empty dict, so document
        # extraction became a no-op for every caller that passes files.
        doc_roles = {}
        for p in docs:
            doc_roles[classify_role(p)] = _rel(p)

        presentations, presentation_used = [], None
        if decision == "new_product":
            print(f"\n>> New product (not in Smartsheet): {program} — extracting A&B from document(s): {doc_roles}")
            pid = load_new_product(program, doc_roles, con)
        else:
            presentations = list_presentations(program, con)
            chosen = _match_presentation(presentations, presentation)
            if chosen is not None:
                pid = chosen
                print(f"\n>> {program}: using the selected presentation ({presentation}) -> product_id {pid}.")
            else:
                pid, nrows = _pick_presentation(program, docs, con)
                if nrows > 1:
                    print(f"\n>> {program}: {nrows} presentations — auto-selected the best match "
                          f"(product_id {pid}); the presentation can be overridden.")
            presentation_used = next((pr["source_row"] for pr in presentations if pr["product_id"] == pid), None)
            print(f">> Identified product: {program} (in Smartsheet)  | uploaded: {doc_roles}")
            extract_docs.extract_and_load(program, con, docs=doc_roles)  # PDF enrichment; no-op otherwise

        name_row = con.execute("SELECT MAX(program_name) FROM product WHERE program_no=?", (program,)).fetchone()
        identified_name = name_row[0] if name_row else None
        con.close()

        ingest_assessment.main(program)
        populate_template.populate(program, product_id=pid, photo=photo)
        rc = verify.main(program)

        # Provisional similarity / mix-up risk (explainable; analysis/audit only —
        # does NOT auto-fill the form's Parts D/E Similarity Risk boxes).
        try:
            risk_result = risk.assess_program(program, product_id=pid, db=paths.db_path())
            print(f">> Provisional mix-up risk: {risk_result['overall_risk']} "
                  f"({risk_result['n_comparators']} co-located in-scope comparators)")
        except Exception as e:
            risk_result = {"overall_risk": "n/a", "n_comparators": 0, "closest": None,
                           "comparators": [], "rationale": f"risk assessment unavailable: {e}",
                           "note": "", "error": str(e)}

    out = os.path.join(paths.output_dir(), f"{program}_PSA_generated.docx")
    return {"status": "ok", "program": program, "identified_name": identified_name,
            "decision": decision, "subject_code": subject_code, "reason": info.get("reason"),
            "presentations": [{"source_row": pr["source_row"], "list_number": pr["list_number"],
                               "label": pr["label"]} for pr in presentations],
            "presentation_used": presentation_used,
            "output_path": out, "output_exists": os.path.exists(out),
            "verify_ok": rc == 0, "risk": risk_result,
            "alternatives": info.get("alternatives", []), "log": log.getvalue()}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # avoid cp1252 crashes on non-ASCII (→, —, ✓)
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("usage: python -m psa.workflow <uploaded_file> [more_files ...] "
              "[--product CODE] [--presentation SOURCE_ROW|LIST_NUMBER]")
        return 1
    args = sys.argv[1:]
    override = presentation = None
    if "--product" in args:
        i = args.index("--product")
        override = args[i + 1]
        args = args[:i] + args[i + 2:]
    if "--presentation" in args:
        i = args.index("--presentation")
        presentation = args[i + 1]
        args = args[:i] + args[i + 2:]
    res = process_documents(args, program_override=override, presentation=presentation)
    print(res["log"])
    if res["status"] == "needs_override":
        print(res["message"])
        print("Products:", ", ".join(c[0] for c in res["candidates"]))
        return 2
    print(f"\nProduct: {res['program']} ({res['identified_name']})")
    print(f"Output:  {res['output_path']}  (exists={res['output_exists']})")
    r = res.get("risk") or {}
    if r.get("overall_risk"):
        print(f"Mix-up risk (provisional): {r['overall_risk']}  |  {r.get('rationale', '')}")
    print("ALL CHECKS PASSED" if res["verify_ok"] else "VERIFY: some checks failed")
    return 0 if res["verify_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
