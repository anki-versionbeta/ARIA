from __future__ import annotations

from api.backend.da_platform.db.models import Run, RunFile
from tests.conftest import login, make_run


def test_list_applies_filters_sorting_and_pagination(client, session, monkeypatch):
    """Filtering, sorting and pagination over documents spread across three silos.

    The silo ids are incidental to what is asserted, but they cannot be arbitrary: the
    listing is gated by `accessible_module_ids`, which intersects a user's grants with the
    silos that actually exist, and `SILOS_DIR` points at a fixture directory holding only
    `probe`. Runs recorded against `bop`/`iso` would therefore be invisible and every total
    below would be 0.

    So this test declares the three silos its data needs. Patching
    `access.known_module_ids` rather than adding fixture silo directories keeps the change
    inside this test: `test_auth`, `test_engine` and `test_admin_routes` all assert the
    exact discovered set, and a second fixture silo would silently change what they mean.
    The patch is applied before `login()`, because the conftest helper grants whatever is
    known at the moment it runs.
    """
    from api.backend.da_platform.auth import access

    monkeypatch.setattr(access, "known_module_ids", lambda: {"probe", "bop", "iso"})

    asha = login(client)
    ben = login(client, "ben.carter")
    login(client)  # back to asha

    make_run(session, owner_id=asha["id"], title="Alpha report", silo="bop", status="complete")
    make_run(session, owner_id=asha["id"], title="Beta report", silo="iso", status="failed")
    make_run(session, owner_id=ben["id"], title="Gamma report", silo="iso", status="complete")

    everything = client.get("/api/documents").json()
    assert everything["total"] == 3
    assert everything["page"] == 1

    assert client.get("/api/documents?silo=iso").json()["total"] == 2
    assert client.get("/api/documents?status=complete").json()["total"] == 2
    assert client.get("/api/documents?user=ben.carter").json()["total"] == 1
    assert client.get("/api/documents?q=beta").json()["total"] == 1

    by_name = client.get("/api/documents?sort=name&order=asc").json()
    assert [item["title"] for item in by_name["items"]] == [
        "Alpha report",
        "Beta report",
        "Gamma report",
    ]

    by_owner = client.get("/api/documents?sort=user&order=asc").json()
    assert by_owner["items"][0]["owner"]["display_name"] == "Asha Rao"

    # Page 2 is empty with only three records, but the total still reports 3.
    page_two = client.get("/api/documents?page=2").json()
    assert page_two["items"] == []
    assert page_two["total"] == 3


def test_unknown_document_is_404(client):
    login(client)
    assert client.get("/api/documents/missing").status_code == 404


def test_can_edit_reflects_ownership(client, session):
    asha = login(client)
    ben = login(client, "ben.carter")

    ben_run = make_run(session, owner_id=ben["id"], title="Ben's document")

    # Still signed in as Ben, who owns it.
    assert client.get(f"/api/documents/{ben_run.id}").json()["can_edit"] is True

    login(client, "asha.rao")
    detail = client.get(f"/api/documents/{ben_run.id}").json()
    assert detail["can_edit"] is False
    assert detail["owner"]["display_name"] == "Ben Carter"
    assert asha["id"] != ben["id"]


def test_fork_creates_an_independent_copy(client, session):
    asha = login(client)
    ben = login(client, "ben.carter")
    original = make_run(session, owner_id=ben["id"], title="Original", status="complete")

    login(client, "asha.rao")
    response = client.post(f"/api/documents/{original.id}/fork")
    assert response.status_code == 201
    fork_id = response.json()["id"]

    fork = client.get(f"/api/documents/{fork_id}").json()
    assert fork["title"] == "Copy of Original"
    assert fork["owner"]["id"] == asha["id"]
    assert fork["can_edit"] is True
    assert fork["forked_from_run_id"] == original.id
    # A fork has no output until its owner builds one.
    assert [item["kind"] for item in fork["files"]] == ["input"]
    assert fork["status"] == "awaiting_user"

    # The original is untouched and still owned by Ben.
    untouched = client.get(f"/api/documents/{original.id}").json()
    assert untouched["owner"]["id"] == ben["id"]
    assert untouched["title"] == "Original"


def test_fork_shares_the_input_storage_key(client, session):
    ben = login(client, "ben.carter")
    original = make_run(session, owner_id=ben["id"], title="Shared input", status="complete")

    login(client, "asha.rao")
    fork_id = client.post(f"/api/documents/{original.id}/fork").json()["id"]

    original_input = (
        session.query(RunFile)
        .filter(RunFile.run_id == original.id, RunFile.kind == "input")
        .one()
    )
    fork_input = session.query(RunFile).filter(RunFile.run_id == fork_id).one()

    # Nothing is ever deleted, so the fork can safely point at the same object
    # rather than duplicating a large PDF.
    assert fork_input.storage_key == original_input.storage_key


def test_every_authenticated_user_can_read_any_document(client, session):
    ben = login(client, "ben.carter")
    run = make_run(session, owner_id=ben["id"], title="Readable by all")

    login(client, "asha.rao")
    assert client.get(f"/api/documents/{run.id}").status_code == 200


def test_metadata_column_is_mapped_without_shadowing_declarative_metadata(session):
    """`metadata` is reserved on the declarative base, so the attribute is `meta`
    while the column keeps its documented name."""
    assert "metadata" in Run.__table__.columns
    assert Run.meta.property.columns[0].name == "metadata"
