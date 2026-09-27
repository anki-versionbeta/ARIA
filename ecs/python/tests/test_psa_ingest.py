"""Reading the Smartsheet: the cell parsers, and the one authenticated network call.

Two modules. `ingest_smartsheet.py` holds the parsers that turn a hand-entered cell into
something a database can hold, and they are pure. `ingest_smartsheet_api.py` is the only code in
the whole silo that touches the network, and every one of its failures is raised as `SystemExit`.

**The `SystemExit` behaviour is the reason this file matters most.** It is reasonable for the CLI
these functions were written for and dangerous inside a worker: `SystemExit` derives from
`BaseException`, not `Exception`, so a worker that wraps a stage in `except Exception` to mark the
run failed would not catch it — the exception would unwind the worker itself and take every OTHER
silo's queued runs down with it. `silo.py`'s `fetch` converts it, and `engines.py` converts it on
the three router paths. What is pinned here is that each failure really does raise it, and that
each message identifies WHICH failure it was — an expired token and an unreachable host need
different responses from whoever reads the log.

`urllib` is stubbed rather than mocked at a higher level, so the request that would have gone out
is inspectable: the URL, the bearer token, and the timeout. Nothing here opens a socket.

The site-splitting rules carry real business meaning and are conservative in a specific
direction: a postal address collapses to ONE site because it is one physical place, and splitting
it on its commas would render a Part E block per address fragment in a signed GxP form.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from api.backend.da_platform.settings import BACKEND_ROOT
from api.backend.da_platform.silo_registry import _load_module

SILO_DIR = BACKEND_ROOT / "silos" / "psa"

pytestmark = pytest.mark.skipif(
    not (SILO_DIR / "silo.py").is_file(), reason="the PSA silo is not present"
)


@pytest.fixture(scope="module")
def psa():
    _load_module("psa", SILO_DIR / "silo.py")
    from da_silos.psa import config, ingest_smartsheet, ingest_smartsheet_api

    return {"sheet": ingest_smartsheet, "api": ingest_smartsheet_api, "config": config}


@pytest.fixture(autouse=True)
def restore_config(psa):
    before = psa["config"]._INJECTED
    yield
    psa["config"]._INJECTED = before


@pytest.fixture
def credentialed(psa):
    """A config with a token and a sheet id, so the credential guards pass."""
    import dataclasses

    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(),
        smartsheet_token=REDACTED
    ))


# ── cleaning a hand-entered cell ──────────────────────────────────────────────


def test_a_value_is_trimmed(psa):
    assert psa["sheet"].clean("  AP01  ") == "AP01"


@pytest.mark.parametrize("blank", [None, "", "   ", "\n", "\t"])
def test_a_blank_cell_becomes_none_rather_than_an_empty_string(psa, blank):
    """NULL and '' mean the same thing to a person and different things to SQL — `IS NULL`
    is what the scope predicate and the provenance rows test on."""
    assert psa["sheet"].clean(blank) is None


def test_a_number_is_kept_as_text(psa):
    """Smartsheet returns fill volumes as numbers, and the column is TEXT."""
    assert psa["sheet"].clean(2.0) == "2.0"


def test_a_zero_is_not_mistaken_for_blank(psa):
    """`str(0).strip()` is truthy, and a fill volume of 0 is data rather than absence."""
    assert psa["sheet"].clean(0) == "0"


# ── the cap-colour code, which is what makes a match authoritative ────────────


def test_a_trailing_code_is_extracted(psa):
    assert psa["sheet"].parse_color_code("Blue 6043") == "6043"


def test_a_pantone_style_code_keeps_its_suffix(psa):
    """'2063C' is a Pantone reference rather than a supplier code, and truncating it to 2063
    would let it tie to a palette row it is not."""
    assert psa["sheet"].parse_color_code("Magenta 2063C") == "2063C"


def test_a_colour_with_no_code_has_none(psa):
    """Which sends the normaliser down the colour-word path instead."""
    assert psa["sheet"].parse_color_code("Blue") is None


def test_a_blank_colour_has_no_code(psa):
    assert psa["sheet"].parse_color_code("") is None
    assert psa["sheet"].parse_color_code(None) is None


# ── splitting the multi-valued site cells ─────────────────────────────────────


def test_a_single_site_code_is_one_site(psa):
    assert psa["sheet"].split_sites("AP01") == ["AP01"]


@pytest.mark.parametrize("cell", ["AP01, LU", "AP01\nLU", "AP01,LU", " AP01 , LU "])
def test_a_multi_valued_cell_splits_on_commas_and_newlines(psa, cell):
    """The sheet uses both, sometimes in the same column. Each site gets its own Part E
    block, so a missed split silently drops a site from a signed form."""
    assert psa["sheet"].split_sites(cell) == ["AP01", "LU"]


@pytest.mark.parametrize("placeholder", ["N/A", "TBD", "TBC", "TO BE ADDED", "",
                                         "n/a", "  tbd  "])
def test_a_placeholder_yields_no_site(psa, placeholder):
    """A phantom site called 'TBD' would render an empty Part E block for a site that does
    not exist."""
    assert psa["sheet"].split_sites(placeholder) == []


def test_a_placeholder_among_real_sites_is_dropped(psa):
    assert psa["sheet"].split_sites("AP01, TBD, LU") == ["AP01", "LU"]


def test_a_parenthesised_site_name_is_not_split(psa):
    """'TPM (UPS VDL)' is one legitimate multi-token site code, not prose. The address
    heuristic explicitly exempts parenthesised names for this reason."""
    assert psa["sheet"].split_sites("TPM (UPS VDL)") == ["TPM (UPS VDL)"]


def test_a_postal_address_collapses_to_one_site(psa):
    """One address is one physical place — BoNT/E's Westport site is recorded this way.
    Splitting on its commas would produce a Part E block per address fragment, each one
    demanding a separate site signature in a GxP form."""
    address = "1 Waverley Road, Westport, Co Mayo, Ireland"

    sites = psa["sheet"].split_sites(address)

    assert len(sites) == 1
    assert "Westport" in sites[0]


def test_a_multi_line_address_becomes_a_single_readable_line(psa):
    """It ends up as a site name in the form, so the newlines have to go somewhere sensible."""
    sites = psa["sheet"].split_sites("1 Waverley Road\nWestport\nCo Mayo\nIreland")

    assert len(sites) == 1
    assert "\n" not in sites[0]


def test_an_address_is_recognised_by_its_street_type(psa):
    """The heuristic is deliberately conservative: only clear cues fire, so legitimate
    multi-word site names are not misclassified as prose."""
    assert len(psa["sheet"].split_sites("22 Industrial Road, Barceloneta")) == 1


def test_a_two_code_cell_is_not_treated_as_an_address(psa):
    """The counterpart. If two site codes tripped the address heuristic, the second site
    would lose its Part E block."""
    assert len(psa["sheet"].split_sites("AP01, LU")) == 2


# ── the column map, which is positional ───────────────────────────────────────


def test_every_mapped_column_has_a_distinct_position(psa):
    """The loader reads each attribute by index, so two attributes sharing a position would
    silently read the same cell into both fields."""
    positions = list(psa["sheet"].COLS.values())

    assert len(positions) == len(set(positions))


def test_the_positions_are_contiguous_from_zero(psa):
    """A gap would mean a column was removed from the sheet without the map being updated,
    and every attribute after it would read one cell to the left."""
    positions = sorted(psa["sheet"].COLS.values())

    assert positions == list(range(len(positions)))


def test_the_provenance_fields_are_all_mapped_columns(psa):
    """A provenance row for an unmapped attribute could never be populated, and provenance
    coverage is one of `verify.py`'s checks."""
    for field in psa["sheet"].PROV_FIELDS:
        assert field in psa["sheet"].COLS, field


# ── the credential guards ─────────────────────────────────────────────────────


def test_a_missing_token_is_reported_before_any_request(psa):
    """No point opening a socket to be told 401, and the message names the variable so it is
    a five-second fix rather than a confusing error from Smartsheet."""
    import dataclasses

    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(), smartsheet_token=None, smartsheet_sheet_id="SHEET-1"
    ))

    with pytest.raises(SystemExit, match="SMARTSHEET_ACCESS_TOKEN is not set"):
        psa["api"].capture()


def test_a_missing_sheet_id_is_reported_before_any_request(psa):
    import dataclasses

    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(), smartsheet_token="fake-token", smartsheet_sheet_id=None
    ))

    with pytest.raises(SystemExit, match="SMARTSHEET_SHEET_ID is not set"):
        psa["api"].capture()


# ── the network failures, all of which raise SystemExit ───────────────────────


def http_error(code: int, body: bytes = b"{}"):
    """A urllib HTTPError as `_fetch_sheet` would see it."""
    return urllib.error.HTTPError(
        url="https://api.smartsheet.com/2.0/sheets/1", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


def test_an_expired_token_says_so(psa, credentialed, monkeypatch):
    """The most common failure in practice, and the one whose message has to be
    unambiguous — a 401 from a token is not a 403 from sheet permissions."""
    monkeypatch.setattr(psa["api"], "_get",
                        lambda *a, **kw: (_ for _ in ()).throw(http_error(401)))

    with pytest.raises(SystemExit, match="401 Unauthorized"):
        psa["api"].capture()


@pytest.mark.parametrize("code", [403, 404])
def test_a_permission_or_missing_sheet_points_at_the_sheet_id(psa, credentialed,
                                                             monkeypatch, code):
    """Both mean "the token is fine but cannot see this sheet", which is a different fix
    from renewing a credential."""
    monkeypatch.setattr(psa["api"], "_get",
                        lambda *a, **kw: (_ for _ in ()).throw(http_error(code)))

    with pytest.raises(SystemExit, match="SMARTSHEET_SHEET_ID"):
        psa["api"].capture()


def test_another_http_error_carries_the_response_body(psa, credentialed, monkeypatch):
    """An unrecognised status is the case where the body is the only diagnostic available."""
    monkeypatch.setattr(
        psa["api"], "_get",
        lambda *a, **kw: (_ for _ in ()).throw(http_error(500, b"upstream exploded")),
    )

    with pytest.raises(SystemExit, match="upstream exploded"):
        psa["api"].capture()


def test_an_unreachable_host_suggests_the_proxy(psa, credentialed, monkeypatch):
    """api.smartsheet.com is the first public-internet dependency in this platform, so
    "cannot reach it" is far more likely to be egress than an outage — and the message says
    where to look."""
    monkeypatch.setattr(
        psa["api"], "_get",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("no route to host")),
    )

    with pytest.raises(SystemExit, match="proxy"):
        psa["api"].capture()


def test_the_unreachable_message_names_the_host(psa, credentialed, monkeypatch):
    monkeypatch.setattr(
        psa["api"], "_get",
        lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("timed out")),
    )

    with pytest.raises(SystemExit, match="api.smartsheet.com"):
        psa["api"].capture()


def test_every_network_failure_is_a_baseexception_not_an_exception(psa, credentialed,
                                                                  monkeypatch):
    """The property that makes the conversion in `silo.py` necessary. Pinned so that if
    someone "improves" these to RuntimeError, the reason the conversion exists is on record
    rather than becoming a mystery."""
    monkeypatch.setattr(psa["api"], "_get",
                        lambda *a, **kw: (_ for _ in ()).throw(http_error(401)))

    with pytest.raises(BaseException) as caught:
        psa["api"].capture()

    assert isinstance(caught.value, SystemExit)
    assert not isinstance(caught.value, Exception), (
        "SystemExit is not an Exception, which is why a worker's except Exception misses it"
    )


# ── the one successful read ───────────────────────────────────────────────────


def test_a_capture_returns_the_sheet_and_the_id_it_used(psa, credentialed, monkeypatch):
    """The id travels back because `_sheet_id` may have resolved it from config rather than
    from the argument, and the snapshot records what was actually read."""
    monkeypatch.setattr(psa["api"], "_get", lambda *a, **kw: {"name": "NBE (Vials)"})

    sheet, sid = psa["api"].capture()

    assert sheet == {"name": "NBE (Vials)"}
    assert sid == "SHEET-1"


def test_an_explicit_sheet_id_overrides_the_configured_one(psa, credentialed, monkeypatch):
    """So a DEV sheet can be read without a code change."""
    monkeypatch.setattr(psa["api"], "_get", lambda *a, **kw: {})

    _sheet, sid = psa["api"].capture("OTHER-SHEET")

    assert sid == "OTHER-SHEET"


def test_the_request_asks_for_the_attachments(psa, credentialed, monkeypatch):
    """The product photos are attachments, and without `include=attachments` the reply has no
    image ids to resolve — so the photo capture would silently find nothing."""
    seen: list[str] = []
    monkeypatch.setattr(psa["api"], "_get",
                        lambda path, token: seen.append(path) or {})

    psa["api"].capture()

    assert "include=attachments" in seen[0]


def test_the_request_names_the_sheet_being_read(psa, credentialed, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(psa["api"], "_get",
                        lambda path, token: seen.append(path) or {})

    psa["api"].capture()

    assert "/sheets/SHEET-1" in seen[0]


def test_the_token_is_sent_as_a_bearer_credential(psa, credentialed, monkeypatch):
    """Checked at the transport boundary, because this is where the credential leaves the
    process and the header name is not something to get wrong silently."""
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"name": "NBE"}).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(psa["api"].urllib.request, "urlopen", fake_urlopen)

    psa["api"].capture()

    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["url"].startswith("https://")


def test_the_read_has_a_timeout(psa, credentialed, monkeypatch):
    """A worker blocked forever on a hung sheet read holds its claim until the reaper takes
    it, so every request sets one."""
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(
        psa["api"].urllib.request, "urlopen",
        lambda request, timeout=None: captured.update(timeout=timeout) or FakeResponse(),
    )

    psa["api"].capture()

    assert captured["timeout"] == 60


# ── the product photo, for one row only ───────────────────────────────────────


def sheet_with_row(row_number=20):
    from da_silos.psa.ingest_smartsheet import COLS

    return {
        "name": "NBE (Vials)",
        "columns": [{"id": 100 + i, "index": i, "title": t}
                    for t, i in sorted(COLS.items(), key=lambda kv: kv[1])],
        "rows": [{"rowNumber": row_number, "id": 900, "cells": []}],
    }


def test_no_photo_is_fetched_without_a_token(psa):
    """Resolving a cell-image id to a URL is an authenticated POST, so without a credential
    there is nothing to try — and a run with no photo is a normal run."""
    import dataclasses

    psa["config"].set_config(dataclasses.replace(
        psa["config"].defaults(), smartsheet_token=None
    ))

    assert psa["api"].capture_row_image(sheet_with_row(), 20) is None


def test_a_row_that_is_not_in_the_sheet_yields_no_photo(psa, credentialed):
    """A stale row number after a sheet edit, which must not raise mid-run."""
    assert psa["api"].capture_row_image(sheet_with_row(20), 999) is None


def test_a_row_with_no_image_yields_no_photo(psa, credentialed):
    """The common case: most rows have no product photo yet. The report renders a blank photo
    cell and `verify.py` accepts it."""
    assert psa["api"].capture_row_image(sheet_with_row(20), 20) is None


def test_the_photo_lookup_never_touches_the_network_when_there_is_nothing_to_fetch(
        psa, credentialed, monkeypatch):
    """The guards run before the POST, so a sheet full of photoless rows costs no requests."""
    def forbidden(*args, **kwargs):
        raise AssertionError("no request should be made")

    monkeypatch.setattr(psa["api"].urllib.request, "urlopen", forbidden)

    assert psa["api"].capture_row_image(sheet_with_row(20), 20) is None
