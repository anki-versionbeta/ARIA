"""Ingest the NBE (Vials) Smartsheet DIRECTLY FROM THE LIVE SHEET via the Smartsheet REST API.

Same destination as ingest_smartsheet.py (the .xlsx reader): product / site / vendor /
cap_color / product_site + field_provenance — everything downstream reads the DB, so this only
swaps the *reader*.  It ALSO pulls each row's product image (which the Excel export drops) and
records it exactly like the PDD photo (doc_source / doc_attribute[product_photo] / field_provenance),
so the populator embeds it with no other change.

Credentials arrive through `config.config()` (SMARTSHEET_ACCESS_TOKEN / SMARTSHEET_SHEET_ID locally,
the platform's secret store in ARIA). Nothing here reads the environment: a silo that names its own
credential fails `test_silo_isolation.py`, and the token authenticates as a real person and can read
every sheet they can see — treat it like a password.

Usage:
  python -m psa.ingest_smartsheet_api --columns   # print the sheet's columns (verify the mapping)
  python -m psa.ingest_smartsheet_api             # ingest live into psa.db
  python -m psa.ingest_smartsheet_api <SHEET_ID>  # override the configured sheet id

No new dependency: uses urllib (stdlib).
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

# reuse the proven parsing/cleaning helpers + the column contract from the .xlsx reader
from . import paths, scope   # scope = the clinical-exclusion rule, shared with report/risk/cap paths
from .config import config
from .ingest_smartsheet import COLS, PROV_FIELDS, clean, parse_color_code, split_sites

API = "https://api.smartsheet.com/2.0"

# Columns are resolved POSITIONALLY by default (the .xlsx was exported from this sheet, so
# COLS[attr] index still aligns).  After running --columns, lock any column by exact title here
# to be resilient to future column reordering, e.g.  "program_no": "Program #".
TITLE_OVERRIDES = {}


# ---------------------------------------------------------------- config / availability
def _token():
    return config().smartsheet_token


def _sheet_id(argv_id=None):
    return argv_id or config().smartsheet_sheet_id


def available():
    """True when the live API should be used (credentials present, unless the source is forced)."""
    cfg = config()
    if cfg.ingest_source == "xlsx":
        return False
    if cfg.ingest_source == "api":
        return True
    return bool(cfg.smartsheet_token and cfg.smartsheet_sheet_id)


# ---------------------------------------------------------------- lightweight LIVE reads (no DB / no images)
# Used by the UI to populate pickers straight from the live sheet, without a full ingest. `source_row` is
# the Smartsheet rowNumber — the SAME value ingest stores in product.source_row — so a picked row maps
# 1:1 to a DB product row once the DB is (re)ingested.
def _live_rows():
    """Fetch the live sheet once and return a light dict per row. [] on any failure (UI must not crash)."""
    if not available():
        return []
    token, sid = _token(), _sheet_id()
    if not (token and sid):
        return []
    try:
        sheet = _get(f"/sheets/{sid}", token)          # no ?include=attachments — we don't need images here
        col_id, _ = _resolver(sheet)
        rows = []
        for row in sheet.get("rows", []):
            cells = _row_cells(row, col_id)
            rows.append({"source_row": row.get("rowNumber"),
                         "program_no": cells.get("program_no"), "program_name": cells.get("program_name"),
                         "vial": cells.get("vial_container_size"), "batch_type": cells.get("batch_type"),
                         "strength": cells.get("strength"), "list_number": cells.get("list_number")})
        return rows
    except Exception:
        return []


def live_programs():
    """Distinct in-scope (non-clinical) (program_no, program_name) from the LIVE sheet. [] if unavailable."""
    seen, out = set(), []
    for r in _live_rows():
        p = (r["program_no"] or "").strip()
        if not p or p.upper() == "N/A" or not scope.is_in_scope(r["batch_type"]):
            continue
        if p not in seen:
            seen.add(p)
            out.append((p, r["program_name"]))
    return out


def live_presentations(program_no):
    """In-scope presentation rows for one program from the LIVE sheet (each keyed by source_row)."""
    return [{"source_row": r["source_row"], "vial": r["vial"], "batch_type": r["batch_type"],
             "strength": r["strength"], "list_number": r["list_number"]}
            for r in _live_rows()
            if (r["program_no"] or "").strip() == program_no and scope.is_in_scope(r["batch_type"])]


# ---------------------------------------------------------------- HTTP (stdlib urllib)
def _get(path, token):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, token, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
        fh.write(r.read())


def _fetch_sheet(token, sheet_id):
    try:
        return _get(f"/sheets/{sheet_id}?include=attachments", token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SystemExit("Smartsheet API: 401 Unauthorized — the token is missing, wrong, or expired.")
        if e.code in (403, 404):
            raise SystemExit(f"Smartsheet API: {e.code} — check SMARTSHEET_SHEET_ID and that the token can access it.")
        raise SystemExit(f"Smartsheet API error {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach api.smartsheet.com ({e.reason}). "
                         "Check internet / corporate proxy (HTTPS_PROXY).")


# ---------------------------------------------------------------- column mapping
def _columns_by_index(sheet):
    return sorted(sheet["columns"], key=lambda c: c["index"])


def _resolver(sheet):
    """Return (col_id(attr), col_title(attr)) using exact-title override, else COLS positional index."""
    by_index = _columns_by_index(sheet)
    by_title = {c["title"]: c for c in sheet["columns"]}

    def column(attr):
        title = TITLE_OVERRIDES.get(attr)
        if title and title in by_title:
            return by_title[title]
        idx = COLS[attr]
        return by_index[idx] if idx < len(by_index) else None

    return (lambda a: (column(a) or {}).get("id"),
            lambda a: (column(a) or {}).get("title"))


def print_columns(token, sheet_id):
    """Dump the sheet's columns and the positional attribute mapping so it can be verified."""
    sheet = _fetch_sheet(token, sheet_id)
    print(f"Sheet: {sheet.get('name')!r}  (id {sheet.get('id')}, {len(sheet['columns'])} columns, "
          f"{len(sheet.get('rows', []))} rows)\n")
    print("index  column title")
    for c in _columns_by_index(sheet):
        print(f"  {c['index']:>3}  {c['title']}")
    _, col_title = _resolver(sheet)
    print("\nAttribute → column it will read (positional; add TITLE_OVERRIDES to lock by title):")
    for attr in COLS:
        print(f"  {attr:<22} → [{COLS[attr]:>2}] {col_title(attr)}")


# ---------------------------------------------------------------- row values
def _cell_value(cell):
    if cell is None:
        return None
    v = clean(cell.get("displayValue", cell.get("value")))
    # the API CSV-quotes multi-line cells (e.g. a packaging cell comes back as '"AP16\nLU"');
    # unwrap so the downstream site-code split/expansion works exactly like the .xlsx path.
    if v and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = clean(v[1:-1].replace('""', '"'))
    return v


def _row_cells(row, col_id):
    """Build the {attribute: value} dict the loader expects, keyed like the .xlsx COLS."""
    by_col = {c["columnId"]: c for c in row.get("cells", [])}
    return {attr: _cell_value(by_col.get(col_id(attr))) for attr in COLS}


# ---------------------------------------------------------------- DB load (mirrors ingest_smartsheet)
def _dim_getters(cur):
    site_id, vendor_id = {}, {}

    def get_site(code):
        if code not in site_id:
            cur.execute("INSERT INTO site (site_code, site_name) VALUES (?,?)", (code, code))
            site_id[code] = cur.lastrowid
        return site_id[code]

    def get_vendor(name):
        if name not in vendor_id:
            cur.execute("INSERT INTO vendor (vendor_name) VALUES (?)", (name,))
            vendor_id[name] = cur.lastrowid
        return vendor_id[name]

    return get_site, get_vendor


def _load_row(cur, cells, source_row, locator_for, get_site, get_vendor):
    """Insert one product-presentation + its provenance / cap_color / site links. Returns product_id or None."""
    if not cells["program_no"] and not cells["program_name"]:
        return None
    cap_vendor = cells["_cap_vendor"]
    vid = get_vendor(cap_vendor) if cap_vendor and cap_vendor.upper() not in ("N/A",) else None

    prod = {k: cells[k] for k in COLS if not k.startswith("_")}
    prod["source_row"] = source_row
    prod["cap_vendor_id"] = vid
    prod["dp_mfr_site_raw"] = cells["_dp_mfr"]
    prod["dp_pkging_site_raw"] = cells["_dp_pkging"]

    keys = list(prod.keys())
    cur.execute(f"INSERT INTO product ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [prod[k] for k in keys])
    pid = cur.lastrowid

    for f in PROV_FIELDS:
        if cells[f] is not None:
            cur.execute(
                "INSERT INTO field_provenance "
                "(entity, entity_id, field_name, source_doc, source_locator, match_type, extraction_method) "
                "VALUES ('product', ?, ?, 'Smartsheet', ?, 'Exact', 'auto')",
                (pid, f, locator_for(f)))

    if cells["_cap_color"]:
        cur.execute(
            "INSERT INTO cap_color (product_id, batch_type, cap_color_name, color_code, vendor_id) "
            "VALUES (?,?,?,?,?)",
            (pid, cells["batch_type"], cells["_cap_color"], parse_color_code(cells["_cap_color"]), vid))

    for code in split_sites(cells["_dp_mfr"]):
        cur.execute("INSERT OR IGNORE INTO product_site VALUES (?,?,?)", (pid, get_site(code), "mfr"))
    for code in split_sites(cells["_dp_pkging"]):
        cur.execute("INSERT OR IGNORE INTO product_site VALUES (?,?,?)", (pid, get_site(code), "pkging"))
    return pid


# ---------------------------------------------------------------- images (the bit the export loses)
def _pick_image_task(row, pid, program, col_id):
    """Return an image task for the row, or None. Prefers a cell image in the Picture columns,
    then any cell image, then an image row-attachment."""
    picture_ids = {col_id("picture1"), col_id("picture2")}
    fallback = None
    for c in row.get("cells", []):
        img = c.get("image")
        if img and img.get("id"):
            task = {"pid": pid, "program": program, "kind": "cell",
                    "imageId": img["id"], "w": img.get("width"), "h": img.get("height")}
            if c["columnId"] in picture_ids:
                return task
            fallback = fallback or task
    if fallback:
        return fallback
    for att in row.get("attachments", []):
        name = (att.get("name") or "").lower()
        if att.get("mimeType", "").startswith("image") or name.endswith((".png", ".jpg", ".jpeg", ".gif")):
            return {"pid": pid, "program": program, "kind": "attachment",
                    "attachmentId": att["id"], "name": att.get("name")}
    return None


def _resolve_cell_image_urls(tasks, token):
    """Batch-resolve cell-image ids → temporary download URLs via POST /imageurls."""
    cell = [t for t in tasks if t["kind"] == "cell"]
    if not cell:
        return {}
    body = [{"imageId": t["imageId"], "height": t["h"] or 0, "width": t["w"] or 0} for t in cell]
    try:
        resp = _post("/imageurls", token, body)
    except urllib.error.HTTPError as e:
        print(f"  image URL resolve failed ({e.code}) — skipping cell images."); return {}
    return {u["imageId"]: u["url"] for u in resp.get("imageUrls", [])}


def capture_row_image(sheet, source_row):
    """The product photo for ONE sheet row, as bytes. Returns (filename, bytes) or None.

    In memory, never to disk — the ARIA counterpart of `_save_images`, whose PNGs on local disk are the
    pattern `da_platform/storage/base.py` warns about. The `fetch` stage stores what this returns as run
    media, and `report` embeds it from there, so the photo travels with the run's snapshot instead of
    depending on a file that a replaced container would not have.

    One row, not all of them: a run assesses one presentation, and the sheet has ~37. Downloading the
    other 36 photos to embed one would be a network trip and an object per run for nothing.

    Needs the token, because resolving a cell-image id to a URL is an authenticated POST. Downloading
    from the returned URL is not — Smartsheet hands back a short-lived signed link.
    """
    token = REDACTED
    if not token:
        return None
    col_id, _col_title = _resolver(sheet)
    row = next((r for r in sheet.get("rows", []) if r.get("rowNumber") == source_row), None)
    if row is None:
        return None

    task = _pick_image_task(row, pid=None, program=None, col_id=col_id)
    if task is None:
        return None

    try:
        if task["kind"] == "cell":
            url = _resolve_cell_image_urls([task], token).get(task["imageId"])
            if not url:
                return None
            with urllib.request.urlopen(url, timeout=120) as r:
                return f"row{source_row}_photo.png", r.read()
        att = _get(f"/sheets/{_sheet_id()}/attachments/{task['attachmentId']}", token)
        url = att.get("url")
        if not url:
            return None
        with urllib.request.urlopen(url, timeout=120) as r:
            name = task.get("name") or f"row{source_row}_photo.png"
            return name, r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        # A missing photo must not fail a run: the report renders a blank photo cell and verify accepts it.
        print(f"  image capture for row {source_row} failed ({exc}) — the report will have no photo.")
        return None


def _save_images(tasks, token, sheet_id, con):
    """Download each row image and record it like the PDD photo (Smartsheet provenance)."""
    if not tasks:
        print("  images: none found on the sheet rows."); return 0
    os.makedirs(paths.image_dir(), exist_ok=True)
    url_by_id = _resolve_cell_image_urls(tasks, token)
    cur = con.cursor()
    saved = 0
    for t in tasks:
        # program-safe file name, made UNIQUE PER ROW by product_id — a program code can have several
        # presentation rows (e.g. ABBV-383 ×3), each with its own image; keying only by program_no would
        # make them overwrite one file on disk, so the chosen row would show another row's picture.
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (t["program"] or "row"))
        dest = os.path.join(paths.image_dir(), f"{safe}_{t['pid']}_smartsheet.png")
        try:
            if t["kind"] == "cell":
                url = url_by_id.get(t["imageId"])
                if not url:
                    continue
                _download(url, dest)
                locator = f"cell-image {t['imageId']}"
            else:
                att = _get(f"/sheets/{sheet_id}/attachments/{t['attachmentId']}", token)
                if not att.get("url"):
                    continue
                _download(att["url"], dest)
                locator = f"attachment {t['attachmentId']} ({t.get('name')})"
        except (urllib.error.URLError, OSError) as e:
            print(f"  image for {t['program']}: download failed ({e})"); continue

        cur.execute(
            "INSERT INTO doc_source (product_id, doc_role, source_type, source_file, ingested_at) "
            "VALUES (?,?,?,?,datetime('now'))", (t["pid"], "SMARTSHEET", "image", dest))
        doc_id = cur.lastrowid
        cur.execute(
            "INSERT INTO doc_attribute (doc_id, attribute_label, attribute_key, value_text, "
            "source_locator, confidence, status) VALUES (?,?,?,?,?,?,?)",
            (doc_id, "Product image (Smartsheet)", "product_photo", dest, locator, 0.8, "mapped"))
        cur.execute(
            "INSERT INTO field_provenance (entity, entity_id, field_name, source_doc, source_locator, "
            "match_type, extraction_method, confidence, extracted_at) "
            "VALUES ('product', ?, 'product_photo', 'Smartsheet', ?, 'Exact', 'auto', 0.8, datetime('now'))",
            (t["pid"], locator))
        saved += 1
    con.commit()
    print(f"  images: saved {saved} to {paths.image_dir()}/ (recorded with Smartsheet provenance)")
    return saved


# ---------------------------------------------------------------- orchestration
def capture(sheet_id=None):
    """The ONE network read: return the live sheet as a plain dict, and touch nothing else.

    Split out from `main()` so a caller can keep the raw response and rebuild the database from those
    exact bytes later — which is what makes the rebuild a pure function of the snapshot rather than a
    second trip to Smartsheet that may return different data. See `psa/snapshot.py`.
    """
    token = REDACTED
    sid = _sheet_id(sheet_id)
    if not token:
        raise SystemExit("SMARTSHEET_ACCESS_TOKEN is not set. Set it and reopen your terminal.")
    if not sid:
        raise SystemExit("SMARTSHEET_SHEET_ID is not set (or pass it as an argument).")
    return _fetch_sheet(token, sid), sid


def load(sheet, sheet_id, skip_images=False):
    """Load an already-captured sheet dict into psa.db. No network unless images are wanted.

    Deterministic for a given `sheet`: the same dict always produces the same catalogue, which is the
    property the run-scoped snapshot model depends on.
    """
    sid = sheet_id
    col_id, col_title = _resolver(sheet)
    print(f"Live Smartsheet {sheet.get('name')!r} (id {sheet.get('id')}): "
          f"{len(sheet['columns'])} columns, {len(sheet.get('rows', []))} rows")

    con = sqlite3.connect(paths.db_path())
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    get_site, get_vendor = _dim_getters(cur)

    image_tasks = []
    n_prod = 0
    for row in sheet.get("rows", []):
        cells = _row_cells(row, col_id)
        rownum = row.get("rowNumber")
        locator_for = (lambda rn: (lambda f: f"SheetId {sid} row {rn} col {col_title(f)!r}"))(rownum)
        pid = _load_row(cur, cells, rownum, locator_for, get_site, get_vendor)
        if pid is None:
            continue
        n_prod += 1
        task = _pick_image_task(row, pid, cells["program_no"] or cells["program_name"], col_id)
        if task:
            image_tasks.append(task)
    con.commit()

    if skip_images:
        print("   (skipping product-image download — not needed by this caller)")
    else:
        _save_images(image_tasks, _token(), sid, con)

    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("product", "site", "vendor", "cap_color", "product_site", "field_provenance")}
    con.close()
    print(f"Ingested {n_prod} products from the live sheet")
    for t, c in counts.items():
        print(f"  {t}: {c}")
    return {"products": n_prod, **counts}


def main(sheet_id=None, skip_images=False):
    """Capture the live sheet and load it. The behaviour every existing caller expects."""
    sheet, sid = capture(sheet_id)
    return load(sheet, sid, skip_images=skip_images)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--columns":
        tok = _token()
        if not tok:
            raise SystemExit("SMARTSHEET_ACCESS_TOKEN is not set.")
        print_columns(tok, _sheet_id(args[1] if len(args) > 1 else None))
    else:
        main(args[0] if args else None)
