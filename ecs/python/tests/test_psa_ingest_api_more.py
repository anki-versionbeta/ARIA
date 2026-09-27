"""The Smartsheet ingest paths that `test_psa_ingest.py` stops short of: the loader body.

`test_psa_ingest.py` pins the pure cell parsers and the SystemExit contract around the one
authenticated read. What it never drives is the code that turns a captured sheet into rows:
`ingest_smartsheet_api.load` and its helpers (`_resolver`, `_cell_value`, `_pick_image_task`,
`_resolve_cell_image_urls`, `_save_images`), plus the low-level `_get`/`_post`/`_download`
transport wrappers. Those are where a silent mis-mapping lives — a column read one position to
the left, a provenance locator naming the wrong cell, a site row written under the wrong code —
and none of it is visible from the parser tests.

**Nothing here opens a socket.** `urllib.request.urlopen` is replaced on the module object
(`api.urllib.request`), the same boundary `test_psa_ingest.py` stubs, so the request that WOULD
have gone out is inspectable: full URL, bearer header, timeout, and body. Where a test asserts a
path is network-free it installs a `urlopen` that raises, rather than trusting that it wasn't
called.

The `.xlsx` sibling (`ingest_smartsheet.main`) is driven for real: an openpyxl workbook is
written under an injected `root`, so the loader reads a genuine file through the genuine
openpyxl path. `pypdf`/`reportlab`/`oracledb` are not installed and are never imported.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sqlite3
import urllib.error

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

from tests.psa_fixtures import (SUBJECT, SUBJECT_ROW, cell, empty_db,  # noqa: F401
                                psa_config, row, sheet, load_psa)

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def api():
    """The network-facing ingest module, loaded the way the silo registry loads it."""
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import ingest_smartsheet_api

    return ingest_smartsheet_api


@pytest.fixture(scope="module")
def sheetmod():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import ingest_smartsheet

    return ingest_smartsheet


class FakeResponse:
    """A urlopen result as the ingest module consumes it: a context manager with `read`."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def recorder(api, monkeypatch, payload: bytes = b"{}"):
    """Install a urlopen that records the outgoing request and returns `payload`."""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        if isinstance(request, str):
            # `_download` passes the signed URL as a bare string rather than a Request.
            seen["url"] = request
        else:
            seen["url"] = request.full_url
            seen["headers"] = dict(request.headers)
            seen["method"] = request.get_method()
            seen["data"] = request.data
        return FakeResponse(payload)

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    return seen


def forbid_network(api, monkeypatch):
    """Any request at all from here on is a test failure, not a slow test."""
    def forbidden(*args, **kwargs):
        raise AssertionError("this path must not open a socket")

    monkeypatch.setattr(api.urllib.request, "urlopen", forbidden)


def http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError(
        url="https://api.smartsheet.com/2.0/imageurls", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


def png_bytes() -> bytes:
    """A real 1x1 PNG, so anything that sniffs the file gets a valid image."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


# ── load(): a captured sheet becomes catalogue rows ───────────────────────────


@pytest.fixture
def loaded(api, empty_db, monkeypatch):
    """The default fixture sheet, loaded through the real `load` with images skipped."""
    forbid_network(api, monkeypatch)
    counts = api.load(sheet(), "SHEET-1", skip_images=True)
    return counts, empty_db


def test_loading_the_fixture_sheet_writes_one_product_row_per_sheet_row(loaded):
    """A row silently dropped by the loader is a program that can never be reported on."""
    counts, con = loaded

    assert counts["products"] == 6
    assert counts["product"] == 6
    assert con.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 6


def test_loading_records_each_distinct_site_code_once(loaded):
    """Sites are deduplicated by code, so a second product at AP16 must reuse the site row
    rather than create a rival one that splits the comparator pool in half."""
    counts, con = loaded

    codes = {r[0] for r in con.execute("SELECT site_code FROM site").fetchall()}
    assert codes == {"AP16", "LU"}
    assert counts["site"] == 2


def test_loading_records_the_cap_vendor_once_for_the_whole_sheet(loaded):
    """Every fixture row names Datwyler; six vendor rows would mean the cap-vendor match in
    the risk engine compares a product against a duplicate of its own vendor."""
    counts, con = loaded

    assert counts["vendor"] == 1
    assert [r[0] for r in con.execute("SELECT vendor_name FROM vendor").fetchall()] == [
        "Datwyler"
    ]


def test_loading_links_both_the_manufacturing_and_packaging_roles_for_each_row(loaded):
    """`product_site`'s key includes the role, so the same physical site appears twice per
    product — once as mfr, once as pkging. Collapsing them would drop a Part E block."""
    counts, con = loaded

    assert counts["product_site"] == 12
    roles = {r[0] for r in con.execute("SELECT DISTINCT role FROM product_site").fetchall()}
    assert len(roles) == 2


def test_loading_writes_a_provenance_row_for_every_populated_field(loaded):
    """Provenance coverage is one of `verify.py`'s gates: a field written without a
    provenance row cannot be traced back to the cell a reviewer would check."""
    counts, con = loaded

    # The fixture row populates 16 of the 17 PROV_FIELDS (`verified_flag` has no cell).
    assert counts["field_provenance"] == 6 * 16
    assert con.execute("SELECT COUNT(*) FROM field_provenance").fetchone()[0] == 6 * 16


def test_loading_keys_each_row_by_its_smartsheet_row_number(loaded):
    """The API path stores `rowNumber`, not the enumerate index — every caller that asks for a
    presentation supplies the row number a user can see in the sheet."""
    _counts, con = loaded

    rows = [r[0] for r in con.execute(
        "SELECT source_row FROM product ORDER BY source_row"
    ).fetchall()]
    assert rows == list(range(SUBJECT_ROW, SUBJECT_ROW + 6))


def test_a_provenance_locator_names_the_sheet_the_row_and_the_column_title(loaded):
    """The locator is what a reviewer retypes into Smartsheet to find the cell; a wrong row or
    a column id instead of a title makes the audit trail unusable."""
    _counts, con = loaded

    got = con.execute(
        "SELECT fp.source_locator, fp.source_doc FROM field_provenance fp"
        " JOIN product p ON p.product_id = fp.entity_id"
        " WHERE fp.entity='product' AND p.program_no=? AND fp.field_name='program_no'",
        (SUBJECT,),
    ).fetchone()
    assert got["source_locator"] == "SheetId SHEET-1 row 20 col 'program_no'"
    assert got["source_doc"] == "Smartsheet"


def test_a_row_with_neither_a_program_number_nor_a_name_is_skipped(api, empty_db,
                                                                  monkeypatch):
    """The sheet has trailing blank rows; loading them would create nameless products that
    appear in every comparator list."""
    forbid_network(api, monkeypatch)

    counts = api.load(sheet(rows=[{"rowNumber": 30, "id": 1, "cells": []}]), "SHEET-1",
                      skip_images=True)

    assert counts["products"] == 0
    assert empty_db.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 0


def test_a_row_with_only_a_program_name_is_still_loaded(api, sheetmod, empty_db,
                                                        monkeypatch):
    """Early-stage programs are entered by name before a number is assigned, and dropping them
    would hide them from the similarity search entirely."""
    forbid_network(api, monkeypatch)
    cols = sheetmod.COLS
    named = {"rowNumber": 31, "id": 2,
             "cells": [cell(cols, "program_name", "Unnumbered Programme")]}

    counts = api.load(sheet(rows=[named]), "SHEET-1", skip_images=True)

    assert counts["products"] == 1
    assert empty_db.execute("SELECT program_name FROM product").fetchone()[0] == (
        "Unnumbered Programme"
    )


def test_a_cap_vendor_of_not_applicable_leaves_the_vendor_link_null(api, sheetmod, empty_db,
                                                                    monkeypatch):
    """'N/A' is a person saying "no vendor", not a vendor called N/A — a vendor row named N/A
    would match every other product whose vendor is also unknown."""
    forbid_network(api, monkeypatch)
    cols = sheetmod.COLS
    unknown_vendor = {
        "rowNumber": 32, "id": 3,
        "cells": [cell(cols, "program_no", "ABBV-900"),
                  cell(cols, "_cap_color", "Blue 6043"),
                  cell(cols, "_cap_vendor", "N/A")],
    }

    api.load(sheet(rows=[unknown_vendor]), "SHEET-1", skip_images=True)

    assert empty_db.execute("SELECT cap_vendor_id FROM product").fetchone()[0] is None
    assert empty_db.execute("SELECT vendor_id FROM cap_color").fetchone()[0] is None
    assert empty_db.execute("SELECT COUNT(*) FROM vendor").fetchone()[0] == 0


def test_loading_with_images_skipped_makes_no_request_at_all(api, empty_db, monkeypatch):
    """`skip_images=True` is what makes a rebuild-from-snapshot offline; if it still resolved
    image URLs, every replay of a stored snapshot would need a live credential."""
    forbid_network(api, monkeypatch)

    counts = api.load(sheet(), "SHEET-1", skip_images=True)

    assert counts["products"] == 6  # reaching here at all proves no urlopen was attempted


# ── the column resolver: exact title beats the positional fallback ────────────


def test_a_column_is_resolved_by_its_position_when_no_override_is_configured(api, sheetmod):
    """`TITLE_OVERRIDES` is empty by default, so the whole sheet is read positionally — which
    is why `COLS` being contiguous matters."""
    col_id, col_title = api._resolver(sheet())

    assert col_id("program_no") == 100 + sheetmod.COLS["program_no"]
    assert col_title("program_no") == "program_no"


def test_a_configured_title_override_wins_over_the_position(api, sheetmod, monkeypatch):
    """The override exists because the sheet's columns get reordered by hand; if the position
    still won, every attribute after the moved column would read the wrong cell."""
    doc = sheet()
    moved_index = sheetmod.COLS["link_tpp"]
    doc["columns"][moved_index] = {"id": 999, "index": moved_index, "title": "Program #"}
    monkeypatch.setitem(api.TITLE_OVERRIDES, "program_no", "Program #")

    col_id, col_title = api._resolver(doc)

    assert col_id("program_no") == 999
    assert col_title("program_no") == "Program #"


def test_an_override_naming_a_title_the_sheet_does_not_have_falls_back_to_the_position(
        api, sheetmod, monkeypatch):
    """A stale override must degrade to the old behaviour rather than blanking the column."""
    monkeypatch.setitem(api.TITLE_OVERRIDES, "program_no", "No Such Column")

    col_id, _col_title = api._resolver(sheet())

    assert col_id("program_no") == 100 + sheetmod.COLS["program_no"]


def test_a_sheet_missing_its_later_columns_resolves_them_to_nothing(api, sheetmod):
    """A truncated sheet (someone deleted the tail columns) must yield None per attribute
    rather than an IndexError that aborts the whole ingest."""
    doc = sheet()
    doc["columns"] = doc["columns"][:3]

    col_id, col_title = api._resolver(doc)

    assert col_id("link_tpp") is None
    assert col_title("link_tpp") is None
    assert col_id("program_no") == 100 + sheetmod.COLS["program_no"]


# ── reading one cell ──────────────────────────────────────────────────────────


def test_a_display_value_is_preferred_over_the_raw_value(api):
    """Smartsheet returns formatted numbers and picklist labels only in `displayValue`; using
    `value` would put a float where the report prints a string."""
    assert api._cell_value({"value": 1.0, "displayValue": "1.0 mg"}) == "1.0 mg"


def test_a_cell_with_only_a_raw_value_still_reads(api):
    assert api._cell_value({"value": "AP16"}) == "AP16"


@pytest.mark.parametrize("stored,expected", [
    ('"AP16\nLU"', "AP16\nLU"),
    ('"a""b"', 'a"b'),
    ("AP16", "AP16"),
    ('"single"', "single"),
])
def test_a_csv_quoted_cell_is_unwrapped(api, stored, expected):
    """Multi-line cells come back CSV-quoted from the API; leaving the quotes in would make
    the site splitter see one site literally named '"AP16 LU"' instead of two."""
    assert api._cell_value({"value": stored}) == expected


def test_a_cell_that_is_not_present_reads_as_nothing(api):
    """Sparse rows are the norm — Smartsheet omits empty cells entirely, so `_row_cells`
    hands `None` straight through."""
    assert api._cell_value(None) is None


def test_the_row_dictionary_has_an_entry_for_every_mapped_attribute(api, sheetmod):
    """`_load_row` indexes `cells[...]` unguarded for each attribute, so a missing key would
    be a KeyError mid-ingest rather than a blank field."""
    col_id, _col_title = api._resolver(sheet())

    cells = api._row_cells(row(sheetmod.COLS, 0, program=SUBJECT), col_id)

    assert set(cells) == set(sheetmod.COLS)
    assert cells["program_no"] == SUBJECT
    assert cells["link_tpp"] is None


# ── the transport wrappers ────────────────────────────────────────────────────


def test_a_get_addresses_the_smartsheet_api_with_a_bearer_token_and_a_timeout(api,
                                                                             monkeypatch):
    """The single place the credential leaves the process. A wrong host, a wrong header name
    or no timeout are each a production incident and none of them raise locally."""
    seen = recorder(api, monkeypatch, json.dumps({"name": "NBE"}).encode("utf-8"))

    got = api._get("/sheets/1", "tok")

    assert got == {"name": "NBE"}
    assert seen["url"] == "https://api.smartsheet.com/2.0/sheets/1"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["timeout"] == 60


def test_a_post_sends_a_json_body_and_declares_its_content_type(api, monkeypatch):
    """Smartsheet rejects an image-url resolve without the JSON content type, and the failure
    is a 400 with no hint about the header."""
    seen = recorder(api, monkeypatch, json.dumps({"imageUrls": []}).encode("utf-8"))
    body = [{"imageId": "x"}]

    got = api._post("/imageurls", "tok", body)

    assert got == {"imageUrls": []}
    assert seen["method"] == "POST"
    # urllib title-cases header keys, so it really is "Content-type" on the request object.
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["data"] == json.dumps(body).encode("utf-8")
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_a_download_writes_the_bytes_to_disk_with_a_longer_timeout(api, monkeypatch,
                                                                  tmp_path):
    """An image download is larger than a JSON read, so it gets its own timeout — and the
    bytes must land intact or the report embeds a corrupt photo."""
    payload = png_bytes()
    seen = recorder(api, monkeypatch, payload)
    target = tmp_path / "x.png"

    api._download("https://x/1.png", str(target))

    assert target.read_bytes() == payload
    assert seen["timeout"] == 120


# ── choosing which image to fetch for a row ───────────────────────────────────


@pytest.fixture
def pick(api):
    """`_pick_image_task` bound to the fixture sheet's resolver, so the picture columns are
    the ones the real loader would use."""
    col_id, _col_title = api._resolver(sheet())

    def call(sheet_row, pid=7, program="ABBV-1"):
        return api._pick_image_task(sheet_row, pid=pid, program=program, col_id=col_id)

    return call


def column_id(sheetmod, attr):
    return 100 + sheetmod.COLS[attr]


@pytest.mark.parametrize("picture_attr", ["picture1", "picture2"])
def test_an_image_in_a_picture_column_is_chosen_as_a_cell_image(pick, sheetmod,
                                                               picture_attr):
    """The picture columns are the curated ones; anything else on the row is a fallback."""
    task = pick({"cells": [{"columnId": column_id(sheetmod, picture_attr),
                            "image": {"id": "IMG1", "width": 40, "height": 60}}]})

    assert task["kind"] == "cell"
    assert task["imageId"] == "IMG1"
    assert (task["w"], task["h"]) == (40, 60)


def test_the_task_carries_the_product_it_belongs_to(pick, sheetmod):
    """`_save_images` names the file by product id precisely because one program has several
    presentation rows; losing the id would make them overwrite one file on disk."""
    task = pick({"cells": [{"columnId": column_id(sheetmod, "picture1"),
                            "image": {"id": "IMG1"}}]}, pid=42, program="ABBV-383")

    assert task["pid"] == 42
    assert task["program"] == "ABBV-383"


def test_an_image_outside_the_picture_columns_is_still_used_as_a_fallback(pick, sheetmod):
    """Users paste the photo into whichever column is convenient; refusing it would leave the
    report's photo cell blank when an image was right there."""
    task = pick({"cells": [{"columnId": column_id(sheetmod, "program_name"),
                            "image": {"id": "STRAY"}}]})

    assert task is not None
    assert task["imageId"] == "STRAY"


def test_a_picture_column_wins_even_when_another_image_appears_first(pick, sheetmod):
    """Cell order follows column order and program_name sits well before the picture columns,
    so the fallback is seen first — if it won, the curated photo would be ignored on exactly
    the rows that have one."""
    task = pick({"cells": [
        {"columnId": column_id(sheetmod, "program_name"), "image": {"id": "STRAY"}},
        {"columnId": column_id(sheetmod, "picture1"), "image": {"id": "CURATED"}},
    ]})

    assert task["imageId"] == "CURATED"


def test_the_first_stray_image_wins_when_there_is_no_curated_one(pick, sheetmod):
    """`fallback = fallback or task` keeps the earliest, so the choice is deterministic for a
    given sheet — which is what makes a snapshot replay reproducible."""
    task = pick({"cells": [
        {"columnId": column_id(sheetmod, "program_name"), "image": {"id": "FIRST"}},
        {"columnId": column_id(sheetmod, "strength"), "image": {"id": "SECOND"}},
    ]})

    assert task["imageId"] == "FIRST"


def test_a_cell_image_with_no_id_is_ignored(pick, sheetmod):
    """An `image` key with no id cannot be resolved to a URL, so treating it as a task would
    buy a wasted /imageurls round trip per row."""
    assert pick({"cells": [{"columnId": column_id(sheetmod, "picture1"), "image": {}}]}) is None


def test_an_attachment_is_used_when_the_row_carries_no_cell_image(pick):
    """Older rows have the photo as a row attachment rather than a cell image, and those still
    have to render."""
    task = pick({"cells": [],
                 "attachments": [{"id": "A1", "name": "photo", "mimeType": "image/png"}]})

    assert task["kind"] == "attachment"
    assert task["attachmentId"] == "A1"


@pytest.mark.parametrize("name", ["x.PNG", "x.JPG", "photo.jpeg", "anim.GIF"])
def test_an_attachment_is_recognised_by_its_file_extension_whatever_the_case(pick, name):
    """Windows uploads arrive as '.PNG'; a case-sensitive check would skip them, and these
    rows have no other photo to fall back to."""
    task = pick({"cells": [], "attachments": [{"id": "A1", "name": name}]})

    assert task is not None
    assert task["kind"] == "attachment"
    assert task["name"] == name


def test_a_non_image_attachment_is_not_mistaken_for_a_photo(pick):
    """Rows carry spec PDFs too, and embedding one as the product photo would corrupt the
    report rather than leave the cell blank."""
    assert pick({"cells": [],
                 "attachments": [{"id": "A1", "name": "spec.pdf",
                                  "mimeType": "application/pdf"}]}) is None


def test_an_attachment_whose_mime_type_is_explicitly_null_raises(pick):
    """Characterising a real defect, NOT endorsing it: the check is
    `att.get("mimeType", "").startswith("image")`, and an explicit `"mimeType": None` makes
    `get` return None rather than the default, so `.startswith` raises AttributeError and the
    whole ingest dies on one malformed attachment. This LOOKS WRONG —
    `(att.get("mimeType") or "")` would be the fix, matching how the same function already
    guards `name`. Pinned so the crash is on record rather than a surprise in production."""
    with pytest.raises(AttributeError):
        pick({"cells": [],
              "attachments": [{"id": "A1", "name": "photo", "mimeType": None}]})


def test_a_row_with_no_image_of_any_kind_yields_no_task(pick):
    assert pick({"cells": [], "attachments": []}) is None


# ── resolving a cell-image id to a download URL ───────────────────────────────


def test_resolving_a_cell_image_asks_for_it_by_id_and_returns_the_url_map(api, monkeypatch):
    """The id in the sheet is not fetchable; only the short-lived URL from /imageurls is. A
    broken map means every photo silently disappears from the reports."""
    sent: dict = {}

    def fake_post(path, token, body):
        sent["path"] = path
        sent["body"] = body
        return {"imageUrls": [{"imageId": "IMG1", "url": "https://x/1.png"}]}

    monkeypatch.setattr(api, "_post", fake_post)

    got = api._resolve_cell_image_urls(
        [{"kind": "cell", "imageId": "IMG1", "w": None, "h": None}], "tok")

    assert got == {"IMG1": "https://x/1.png"}
    assert sent["path"] == "/imageurls"
    # w/h are frequently absent on a pasted image; `or 0` is what keeps the body valid.
    assert sent["body"] == [{"imageId": "IMG1", "height": 0, "width": 0}]


def test_resolving_passes_through_the_dimensions_when_the_sheet_has_them(api, monkeypatch):
    """Smartsheet returns a URL scaled to the requested box, so asking for 0x0 when real
    dimensions are known would fetch a thumbnail into a signed report."""
    sent: dict = {}
    monkeypatch.setattr(api, "_post", lambda path, token, body: sent.update(body=body) or {})

    api._resolve_cell_image_urls([{"kind": "cell", "imageId": "IMG1", "w": 320, "h": 240}],
                                 "tok")

    assert sent["body"] == [{"imageId": "IMG1", "height": 240, "width": 320}]


def test_an_attachment_only_task_list_needs_no_image_url_resolve(api, monkeypatch):
    """Attachments carry their own URL from the attachments endpoint; posting them to
    /imageurls would be a guaranteed 400."""
    monkeypatch.setattr(api, "_post", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("attachments must not be resolved via /imageurls")))

    assert api._resolve_cell_image_urls(
        [{"kind": "attachment", "attachmentId": "A1"}], "tok") == {}


def test_resolving_with_nothing_to_resolve_makes_no_request(api, monkeypatch):
    """A sheet of photoless rows must cost zero requests, which is what makes a full rebuild
    cheap."""
    def forbidden(*args, **kwargs):
        raise AssertionError("no /imageurls call should be made")

    monkeypatch.setattr(api, "_post", forbidden)

    assert api._resolve_cell_image_urls([], "tok") == {}


def test_a_failed_resolve_degrades_to_no_photos_rather_than_failing_the_ingest(api,
                                                                              monkeypatch):
    """A photo is decoration; the catalogue is the product. An /imageurls outage must not stop
    six hundred products from loading."""
    monkeypatch.setattr(api, "_post",
                        lambda *a, **kw: (_ for _ in ()).throw(http_error(500)))

    assert api._resolve_cell_image_urls(
        [{"kind": "cell", "imageId": "IMG1", "w": None, "h": None}], "tok") == {}


# ── saving the photo, and the provenance it carries ───────────────────────────


@pytest.fixture
def one_product(api, empty_db, psa_config):
    """A single product row to hang an image off, plus its own connection for `_save_images`."""
    product_id = empty_db.execute(
        "INSERT INTO product (program_no, source_row) VALUES (?, ?)", (SUBJECT, SUBJECT_ROW)
    ).lastrowid
    empty_db.commit()
    return product_id


def test_saving_a_cell_image_records_a_source_an_attribute_and_its_provenance(
        api, empty_db, one_product, monkeypatch):
    """The photo is evidence in a signed form, so it needs the same three-row trail as any
    other field: where it came from, what it is, and which cell it was read out of."""
    payload = png_bytes()
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {
        "imageUrls": [{"imageId": "IMG1", "url": "https://x/1.png"}]})
    monkeypatch.setattr(api.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(payload))

    saved = api._save_images(
        [{"kind": "cell", "imageId": "IMG1", "pid": one_product, "program": SUBJECT,
          "w": None, "h": None}],
        "tok", "SHEET-1", empty_db,
    )

    assert saved == 1
    source = empty_db.execute(
        "SELECT product_id, doc_role, source_type, source_file FROM doc_source").fetchone()
    assert (source["product_id"], source["doc_role"], source["source_type"]) == (
        one_product, "SMARTSHEET", "image")
    # The file name is keyed by product_id, not program_no: several presentation rows of one
    # program each have their own photo and must not overwrite each other.
    assert source["source_file"].endswith(f"{SUBJECT}_{one_product}_smartsheet.png")
    with open(source["source_file"], "rb") as handle:
        assert handle.read() == payload

    attribute = empty_db.execute(
        "SELECT attribute_key, source_locator, status FROM doc_attribute").fetchone()
    assert attribute["attribute_key"] == "product_photo"
    assert attribute["source_locator"] == "cell-image IMG1"
    assert attribute["status"] == "mapped"

    provenance = empty_db.execute(
        "SELECT entity, entity_id, field_name, source_doc, source_locator FROM field_provenance"
    ).fetchone()
    assert provenance["entity"] == "product"
    assert provenance["entity_id"] == one_product
    assert provenance["field_name"] == "product_photo"
    assert provenance["source_doc"] == "Smartsheet"
    assert provenance["source_locator"] == "cell-image IMG1"


def test_saving_an_attachment_records_the_attachment_id_in_its_locator(
        api, empty_db, one_product, monkeypatch):
    """An attachment and a cell image are different places to look in Smartsheet, so the
    locator has to say which — a reviewer given 'cell-image' for an attachment finds nothing."""
    payload = png_bytes()
    monkeypatch.setattr(api, "_get", lambda path, token: {"url": "https://x/a1.png"})
    monkeypatch.setattr(api.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(payload))

    saved = api._save_images(
        [{"kind": "attachment", "attachmentId": "A1", "name": "photo.png",
          "pid": one_product, "program": SUBJECT}],
        "tok", "SHEET-1", empty_db,
    )

    assert saved == 1
    assert empty_db.execute("SELECT source_locator FROM field_provenance").fetchone()[0] == (
        "attachment A1 (photo.png)"
    )


def test_an_image_whose_url_could_not_be_resolved_is_skipped_without_a_row(
        api, empty_db, one_product, monkeypatch):
    """A resolve that comes back empty must leave no doc_source pointing at a file that was
    never written, or the report would try to embed a missing path."""
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {"imageUrls": []})
    forbid_network(api, monkeypatch)

    saved = api._save_images(
        [{"kind": "cell", "imageId": "IMG1", "pid": one_product, "program": SUBJECT,
          "w": None, "h": None}],
        "tok", "SHEET-1", empty_db,
    )

    assert saved == 0
    assert empty_db.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == 0


def test_a_download_failure_skips_that_image_and_keeps_going(api, empty_db, one_product,
                                                             monkeypatch):
    """One unreachable CDN link must not lose the other photos on the sheet, nor the
    catalogue that has already been committed."""
    payload = png_bytes()
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {"imageUrls": [
        {"imageId": "BAD", "url": "https://x/bad.png"},
        {"imageId": "GOOD", "url": "https://x/good.png"},
    ]})

    def flaky_download(url, dest):
        if "bad" in url:
            raise urllib.error.URLError("no route")
        with open(dest, "wb") as handle:
            handle.write(payload)

    monkeypatch.setattr(api, "_download", flaky_download)

    saved = api._save_images([
        {"kind": "cell", "imageId": "BAD", "pid": one_product, "program": "ABBV-BAD",
         "w": None, "h": None},
        {"kind": "cell", "imageId": "GOOD", "pid": one_product, "program": "ABBV-GOOD",
         "w": None, "h": None},
    ], "tok", "SHEET-1", empty_db)

    assert saved == 1
    assert empty_db.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == 1


def test_a_program_code_with_awkward_characters_is_made_file_safe(api, empty_db, one_product,
                                                                  monkeypatch):
    """The program code comes from a hand-typed cell and lands in a path; a slash in it would
    write outside the assets directory or fail the open outright."""
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {
        "imageUrls": [{"imageId": "IMG1", "url": "https://x/1.png"}]})
    monkeypatch.setattr(api, "_download",
                        lambda url, dest: open(dest, "wb").write(png_bytes()))

    api._save_images(
        [{"kind": "cell", "imageId": "IMG1", "pid": one_product, "program": "AB/BV 12:3",
          "w": None, "h": None}],
        "tok", "SHEET-1", empty_db,
    )

    stored = empty_db.execute("SELECT source_file FROM doc_source").fetchone()[0]
    assert stored.endswith(f"AB_BV_12_3_{one_product}_smartsheet.png")


def test_saving_no_images_writes_nothing_and_makes_no_request(api, empty_db, psa_config,
                                                              monkeypatch):
    """The overwhelmingly common case: a sheet whose rows have no photos yet must cost no
    requests, which is what makes a full rebuild cheap."""
    forbid_network(api, monkeypatch)
    monkeypatch.setattr(api, "_post",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no resolve should happen")))

    assert api._save_images([], "tok", "SHEET-1", empty_db) == 0
    assert empty_db.execute("SELECT COUNT(*) FROM doc_source").fetchone()[0] == 0


# ── capture_row_image: the one-row photo fetch used by the report path ────────


def sheet_carrying_a_cell_image(sheetmod, row_number=SUBJECT_ROW, image_id="IMG1"):
    doc = sheet(rows=[{"rowNumber": row_number, "id": 900, "cells": [
        {"columnId": 100 + sheetmod.COLS["picture1"], "image": {"id": image_id}},
    ]}])
    return doc


def test_capturing_one_rows_photo_returns_a_row_named_file_and_its_bytes(
        api, sheetmod, psa_config, monkeypatch):
    """The report embeds these bytes directly, and the filename carries the row so two
    presentations of the same program do not overwrite each other's photo."""
    payload = png_bytes()
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {
        "imageUrls": [{"imageId": "IMG1", "url": "https://x/1.png"}]})
    monkeypatch.setattr(api.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(payload))

    got = api.capture_row_image(sheet_carrying_a_cell_image(sheetmod), SUBJECT_ROW)

    assert got is not None
    name, data = got
    assert name == f"row{SUBJECT_ROW}_photo.png"
    assert data == payload


def test_a_photo_download_that_cannot_reach_the_host_yields_no_photo(api, sheetmod,
                                                                    psa_config, monkeypatch):
    """A report with no photo is acceptable; a report generation that dies because a CDN was
    unreachable is not."""
    monkeypatch.setattr(api, "_post", lambda *a, **kw: {
        "imageUrls": [{"imageId": "IMG1", "url": "https://x/1.png"}]})
    monkeypatch.setattr(
        api.urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("no route")),
    )

    assert api.capture_row_image(sheet_carrying_a_cell_image(sheetmod), SUBJECT_ROW) is None


# ── the .xlsx fallback reader ─────────────────────────────────────────────────


def test_the_fallback_reader_looks_for_the_export_under_the_silo_root(sheetmod, psa_config):
    """The path is hard-coded, so it is the one thing a deployer has to get right — a silent
    typo means the fallback reader finds nothing and the catalogue comes up empty."""
    assert sheetmod.xlsx_path().endswith(os.path.join(
        "Input_Data_Sources", "Smartsheets", "NBE (Vials) Product Similarity Database.xlsx"))


def write_workbook(sheetmod, root, rows, title=None):
    """Write a real .xlsx where `xlsx_path()` will look, with COLS-ordered columns."""
    import openpyxl

    folder = os.path.join(root, "Input_Data_Sources", "Smartsheets")
    os.makedirs(folder, exist_ok=True)
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheetmod.SHEET if title is None else title
    header = [attr for attr, _ in sorted(sheetmod.COLS.items(), key=lambda kv: kv[1])]
    worksheet.append(header)
    for values in rows:
        worksheet.append([values.get(attr) for attr in header])
    workbook.save(os.path.join(folder, "NBE (Vials) Product Similarity Database.xlsx"))


@pytest.fixture
def xlsx_root(psa_config, tmp_path):
    """Re-inject the config with `root` pointed at tmp_path, so `xlsx_path()` resolves into the
    temp tree. `psa_config` deliberately leaves `root` alone, and writing a workbook into the
    real silo directory would leave a file outside tmp_path."""
    config, _paths, _snapshot = load_psa()
    config.set_config(dataclasses.replace(psa_config, root=str(tmp_path)))
    return str(tmp_path)


@pytest.fixture
def xlsx_db(xlsx_root):
    """A schema-only catalogue under the re-rooted config."""
    _config, paths, _snapshot = load_psa()
    from da_silos.psa import build_db

    build_db.main()
    connection = sqlite3.connect(paths.db_path())
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


XLSX_ROW = {"program_no": SUBJECT, "program_name": "Product 0", "strength": "100 mg",
            "batch_type": "Commercial", "_cap_color": "Blue 6043",
            "_cap_vendor": "Datwyler", "_dp_mfr": "AP16", "_dp_pkging": "AP16, LU"}


def test_the_fallback_reader_loads_the_export_into_the_same_shape_as_the_api_path(
        sheetmod, xlsx_root, xlsx_db):
    """This reader and the API reader must agree, because a host with no token gets its whole
    catalogue from here — a divergence would make the risk engine behave differently per host."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW])

    sheetmod.main()

    product = xlsx_db.execute("SELECT * FROM product").fetchone()
    assert product["program_no"] == SUBJECT
    assert product["dp_pkging_site_raw"] == "AP16, LU"
    assert {r[0] for r in xlsx_db.execute("SELECT site_code FROM site").fetchall()} == {
        "AP16", "LU"}
    assert xlsx_db.execute("SELECT vendor_name FROM vendor").fetchone()[0] == "Datwyler"
    assert xlsx_db.execute("SELECT color_code FROM cap_color").fetchone()[0] == "6043"


def test_the_fallback_reader_numbers_rows_by_their_spreadsheet_position(sheetmod, xlsx_root,
                                                                       xlsx_db):
    """Unlike the API path (which stores Smartsheet's own rowNumber), this one counts from the
    header, so the first data row is 2 — the number a person reads off the .xlsx."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW, dict(XLSX_ROW, program_no="ABBV-400")])

    sheetmod.main()

    assert [r[0] for r in xlsx_db.execute(
        "SELECT source_row FROM product ORDER BY source_row").fetchall()] == [2, 3]


def test_the_fallback_provenance_locator_is_a_spreadsheet_cell_reference(sheetmod, xlsx_root,
                                                                        xlsx_db):
    """A reviewer opens the .xlsx and types this into the name box, so it has to be a real A1
    reference on the real sheet name — a column letter, not the 0-based index."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW])

    sheetmod.main()

    assert xlsx_db.execute(
        "SELECT source_locator FROM field_provenance WHERE field_name='program_no'"
    ).fetchone()[0] == f"{sheetmod.SHEET}!A2"


def test_the_fallback_locator_uses_the_column_letter_of_the_mapped_index(sheetmod, xlsx_root,
                                                                        xlsx_db):
    """The +1 in `get_column_letter(COLS[f] + 1)` is the 0-based-to-1-based conversion; without
    it every locator would name the column to the left of the one actually read."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW])

    sheetmod.main()

    # strength is COLS index 3, i.e. spreadsheet column D.
    assert xlsx_db.execute(
        "SELECT source_locator FROM field_provenance WHERE field_name='strength'"
    ).fetchone()[0] == f"{sheetmod.SHEET}!D2"


def test_the_fallback_reader_skips_the_header_row(sheetmod, xlsx_root, xlsx_db):
    """The header cells are attribute names, and loading them would create a product whose
    program_no is literally 'program_no' and which matches nothing."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW])

    sheetmod.main()

    assert xlsx_db.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1


def test_the_fallback_reader_skips_a_blank_row_without_losing_the_row_numbers(sheetmod,
                                                                             xlsx_root,
                                                                             xlsx_db):
    """Exports carry blank rows in the middle; loading them would put nameless products into
    every comparator list, and renumbering around them would break every stored locator."""
    write_workbook(sheetmod, xlsx_root,
                   [XLSX_ROW, {}, dict(XLSX_ROW, program_no="ABBV-400")])

    sheetmod.main()

    assert [r[0] for r in xlsx_db.execute(
        "SELECT source_row FROM product ORDER BY source_row").fetchall()] == [2, 4]


def test_the_fallback_reader_falls_back_to_the_first_worksheet_when_the_name_differs(
        sheetmod, xlsx_root, xlsx_db):
    """`SHEET` carries a trailing space that is easily lost when someone re-saves the export;
    falling back to the first worksheet is what keeps that from emptying the catalogue."""
    write_workbook(sheetmod, xlsx_root, [XLSX_ROW], title="Renamed By Someone")

    sheetmod.main()

    assert xlsx_db.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1
    # The locator follows the ACTUAL worksheet title, so it stays a usable reference.
    assert xlsx_db.execute(
        "SELECT source_locator FROM field_provenance WHERE field_name='program_no'"
    ).fetchone()[0] == "Renamed By Someone!A2"


def test_the_configured_worksheet_name_has_the_trailing_space_the_export_really_has(sheetmod):
    """Pinned deliberately: the trailing space looks like a typo, and "tidying" it would send
    every run down the first-worksheet fallback instead of the named sheet."""
    assert sheetmod.SHEET == "NBE (Vials) Product Similarity "
    assert sheetmod.SHEET.endswith(" ")


def test_a_not_applicable_cap_vendor_in_the_export_leaves_the_link_null(sheetmod, xlsx_root,
                                                                       xlsx_db):
    """Same rule as the API path: 'N/A' is an absence, and a vendor row named N/A would tie
    together every product whose vendor is merely unknown."""
    write_workbook(sheetmod, xlsx_root, [dict(XLSX_ROW, _cap_vendor="N/A")])

    sheetmod.main()

    assert xlsx_db.execute("SELECT cap_vendor_id FROM product").fetchone()[0] is None
    assert xlsx_db.execute("SELECT COUNT(*) FROM vendor").fetchone()[0] == 0


# ── the site-cell heuristics both readers depend on ──────────────────────────


@pytest.mark.parametrize("empty", [None, "", 0])
def test_an_absent_site_cell_is_not_an_address(sheetmod, empty):
    """Reached for every row with an empty packaging cell — the most common input in the sheet
    — so a truthiness slip here would be a TypeError on the majority of rows."""
    assert sheetmod._looks_like_address(empty) is False


def test_four_comma_separated_parts_are_treated_as_one_address(sheetmod):
    """The four-part rule, pinned because it is counter-intuitive: four short site codes in one
    cell COLLAPSE to a single site rather than splitting. That is deliberate — four parts is
    overwhelmingly an address in this sheet, and a wrongly-split address would demand a separate
    site signature per address fragment in a GxP form. It does mean a genuine four-site cell
    would be under-reported, which is the accepted trade."""
    assert sheetmod._looks_like_address("A, B, C, D") is True
    assert sheetmod.split_sites("A, B, C, D") == ["A, B, C, D"]


def test_three_comma_separated_codes_still_split_into_three_sites(sheetmod):
    """The boundary on the other side of the four-part rule."""
    assert sheetmod.split_sites("AP16, LU, AP01") == ["AP16", "LU", "AP01"]


# ── which reader a host uses, and the light live reads behind the pickers ─────


def inject(config, base, **overrides):
    config.set_config(dataclasses.replace(base, **overrides))


@pytest.mark.parametrize("source,token,sid,expected", [
    # An explicit source wins outright, whatever the credentials say.
    ("xlsx", "tok", "SHEET-1", False),
    ("api", None, None, True),
    # Otherwise it is credentials-driven, and BOTH are needed.
    ("", "tok", "SHEET-1", True),
    ("", "tok", None, False),
    ("", None, "SHEET-1", False),
    ("", None, None, False),
])
def test_the_reader_choice_follows_the_forced_source_then_the_credentials(
        api, psa_config, source, token, sid, expected):
    """This is the switch between the live sheet and the stale .xlsx export. Getting it wrong
    silently serves a catalogue months out of date, or tries to reach the network on a host
    that has no egress."""
    config, _paths, _snapshot = load_psa()
    inject(config, psa_config, ingest_source=source, smartsheet_token=token,
           smartsheet_sheet_id=sid)

    assert api.available() is expected


def test_an_explicit_sheet_id_beats_the_configured_one(api, psa_config):
    """So a DEV sheet can be read without a config change."""
    assert api._sheet_id("OTHER") == "OTHER"
    assert api._sheet_id() == "SHEET-1"
    assert api._token() == psa_config.smartsheet_token


def test_the_picker_reads_return_nothing_rather_than_raising_when_unconfigured(api,
                                                                              psa_config,
                                                                              monkeypatch):
    """These feed the UI's program and presentation dropdowns. An exception here would 500 the
    page; an empty list renders an empty picker, which is the honest answer."""
    config, _paths, _snapshot = load_psa()
    inject(config, psa_config, ingest_source="xlsx")
    forbid_network(api, monkeypatch)

    assert api._live_rows() == []
    assert api.live_programs() == []
    assert api.live_presentations(SUBJECT) == []


def test_a_failing_live_read_degrades_to_an_empty_picker(api, psa_config, monkeypatch):
    """`except Exception` is deliberate breadth here: any upstream shape change must not take
    the page down with it."""
    monkeypatch.setattr(api, "_get", lambda *a, **kw: (_ for _ in ()).throw(
        urllib.error.URLError("no route")))

    assert api._live_rows() == []


def test_the_picker_lists_each_in_scope_program_once(api, psa_config, monkeypatch):
    """One program has several presentation rows; listing it once per row would give the user a
    dropdown with the same code repeated four times."""
    monkeypatch.setattr(api, "_get", lambda *a, **kw: sheet())

    programs = api.live_programs()

    codes = [code for code, _name in programs]
    assert codes == sorted(set(codes), key=codes.index)
    assert SUBJECT in codes
    # The clinical row is out of scope, so its program must not be offered at all.
    assert "ABBV-403" not in codes


def test_the_picker_keys_each_presentation_by_the_smartsheet_row_number(api, psa_config,
                                                                       monkeypatch):
    """The row number is what a subsequent report request sends back, so it has to be the same
    value `load` stores in product.source_row or the two would not line up."""
    monkeypatch.setattr(api, "_get", lambda *a, **kw: sheet())

    presentations = api.live_presentations(SUBJECT)

    assert [p["source_row"] for p in presentations] == [SUBJECT_ROW]
    assert presentations[0]["vial"] == "2R"
    assert presentations[0]["batch_type"] == "Commercial"


def test_a_clinical_presentation_is_not_offered_for_selection(api, psa_config, monkeypatch):
    """Scope is enforced at the picker as well as in the engine, so a user cannot ask for a
    report the engine would then refuse."""
    monkeypatch.setattr(api, "_get", lambda *a, **kw: sheet())

    assert api.live_presentations("ABBV-403") == []


def test_the_picker_read_does_not_ask_for_attachments(api, psa_config, monkeypatch):
    """It needs no images, and `include=attachments` makes Smartsheet return a much larger
    payload for a dropdown that is refreshed on every page load."""
    seen: list[str] = []
    monkeypatch.setattr(api, "_get", lambda path, token: seen.append(path) or sheet())

    api._live_rows()

    assert seen == [f"/sheets/{psa_config.smartsheet_sheet_id}"]
    assert "include=attachments" not in seen[0]


def test_main_captures_the_sheet_and_then_loads_it(api, empty_db, monkeypatch):
    """The one entry point the CLI and the fetch stage both use; if it captured without loading,
    a run would report success against an empty catalogue."""
    forbid_network(api, monkeypatch)
    monkeypatch.setattr(api, "capture", lambda sheet_id=None: (sheet(), "SHEET-1"))

    counts = api.main(skip_images=True)

    assert counts["products"] == 6
    assert empty_db.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 6


def test_main_passes_an_explicit_sheet_id_through_to_the_capture(api, empty_db, monkeypatch):
    """So `python -m ...ingest_smartsheet_api DEV-SHEET` really reads DEV-SHEET."""
    forbid_network(api, monkeypatch)
    asked: list = []
    monkeypatch.setattr(api, "capture",
                        lambda sheet_id=None: asked.append(sheet_id) or (sheet(), "DEV"))

    api.main("DEV-SHEET", skip_images=True)

    assert asked == ["DEV-SHEET"]
    # The id `capture` reports back — not the one requested — is what the locators record.
    assert empty_db.execute(
        "SELECT source_locator FROM field_provenance LIMIT 1").fetchone()[0].startswith(
        "SheetId DEV ")


# ── the column-dump diagnostic ────────────────────────────────────────────────


def test_the_column_dump_shows_which_column_each_attribute_will_read(api, psa_config,
                                                                    monkeypatch, capsys):
    """The only tool available when the sheet has been reordered: it prints the attribute-to-
    column mapping that is otherwise invisible until the ingest silently reads the wrong cell."""
    monkeypatch.setattr(api, "_fetch_sheet", lambda token, sheet_id: sheet())

    api.print_columns("tok", "SHEET-1")

    printed = capsys.readouterr().out
    assert "28 columns" in printed
    assert "6 rows" in printed
    for attr in ("program_no", "_cap_vendor", "link_tpp"):
        assert attr in printed


def test_the_column_dump_reflects_a_configured_override(api, psa_config, monkeypatch, capsys):
    """The point of the dump is to confirm an override took effect before a real ingest is run
    against a reordered sheet."""
    doc = sheet()
    doc["columns"][27] = {"id": 999, "index": 27, "title": "Program #"}
    monkeypatch.setattr(api, "_fetch_sheet", lambda token, sheet_id: doc)
    monkeypatch.setitem(api.TITLE_OVERRIDES, "program_no", "Program #")

    api.print_columns("tok", "SHEET-1")

    printed = capsys.readouterr().out
    assert "program_no" in printed
    assert "Program #" in printed
