from __future__ import annotations

from api.backend.da_platform.db.models import Run
from api.backend.da_platform.records import sections as service
from tests.conftest import login, make_run


def owned_run(client, session) -> tuple[Run, dict]:
    user = login(client)
    run = make_run(session, owner_id=user["id"], title="Editable BOP", status="awaiting_user")
    return run, user


def seed(session, run, user, content=None):
    return service.seed_sections(
        session,
        run.id,
        content or {"purpose": "<p>Original purpose</p>", "scope": "<p>Scope</p>"},
        user["id"],
    )


def test_generated_sections_are_durable_immediately(client, session):
    run, user = owned_run(client, session)
    assert seed(session, run, user) == 2

    payload = client.get(f"/api/documents/{run.id}/sections").json()
    assert [item["section_key"] for item in payload] == ["purpose", "scope"]
    assert payload[0]["revision"] == 1
    # A first version exists without anyone having saved, so the generated document
    # is recoverable from the moment it exists.
    versions = client.get(f"/api/documents/{run.id}/sections/purpose/versions").json()
    assert [version["label"] for version in versions] == ["Generated"]


def test_saving_bumps_the_revision_and_appends_a_version(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)

    response = client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>Edited purpose</p>", "revision": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["version_num"] == 2

    sections = client.get(f"/api/documents/{run.id}/sections").json()
    assert sections[0]["content_html"] == "<p>Edited purpose</p>"


def test_a_stale_revision_is_rejected_rather_than_overwriting(client, session):
    """The same user in two tabs is easy to do when runs take minutes."""
    run, user = owned_run(client, session)
    seed(session, run, user)

    first = client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>From tab one</p>", "revision": 1},
    )
    assert first.status_code == 200

    second = client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>From tab two</p>", "revision": 1},
    )
    assert second.status_code == 409
    assert second.json()["detail"]["current_revision"] == 2

    # The first edit stands; the stale one did not overwrite it.
    sections = client.get(f"/api/documents/{run.id}/sections").json()
    assert sections[0]["content_html"] == "<p>From tab one</p>"


def test_non_owner_gets_403_with_a_fork_offer(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)

    login(client, "ben.carter")
    response = client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>Not mine</p>", "revision": 1},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {"can_fork": True}


def test_non_owner_can_still_read_sections(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)

    login(client, "ben.carter")
    assert client.get(f"/api/documents/{run.id}/sections").status_code == 200


def test_version_history_is_unbounded(client, session):
    """The app being replaced caps in-memory versions at 20 and spills the rest to
    disk; a table has no such limit."""
    run, user = owned_run(client, session)
    seed(session, run, user)

    for index in range(25):
        response = client.put(
            f"/api/documents/{run.id}/sections/purpose",
            json={"html": f"<p>Edit {index}</p>", "revision": index + 1},
        )
        assert response.status_code == 200

    versions = client.get(f"/api/documents/{run.id}/sections/purpose/versions").json()
    assert len(versions) == 26  # 1 generated + 25 edits
    # Newest first.
    assert versions[0]["version_num"] == 26


def test_a_single_version_can_be_fetched(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)
    client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>Second</p>", "revision": 1},
    )

    first = client.get(f"/api/documents/{run.id}/sections/purpose/versions/1").json()
    assert first["content_html"] == "<p>Original purpose</p>"

    missing = client.get(f"/api/documents/{run.id}/sections/purpose/versions/99")
    assert missing.status_code == 404


def test_restore_appends_rather_than_erasing_later_versions(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)
    client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>Second</p>", "revision": 1},
    )

    response = client.post(f"/api/documents/{run.id}/sections/purpose/restore/1")
    assert response.status_code == 200
    assert response.json()["label"] == "Restored v1"

    sections = client.get(f"/api/documents/{run.id}/sections").json()
    assert sections[0]["content_html"] == "<p>Original purpose</p>"
    # History is append-only: nothing was deleted by restoring.
    versions = client.get(f"/api/documents/{run.id}/sections/purpose/versions").json()
    assert [version["version_num"] for version in versions] == [3, 2, 1]


def test_restore_requires_ownership(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)

    login(client, "ben.carter")
    response = client.post(f"/api/documents/{run.id}/sections/purpose/restore/1")
    assert response.status_code == 403


def test_sections_require_authentication(client, session):
    run, user = owned_run(client, session)
    client.cookies.clear()
    assert client.get(f"/api/documents/{run.id}/sections").status_code == 401


def test_unknown_document_is_404(client):
    login(client)
    assert client.get("/api/documents/nope/sections").status_code == 404


def test_seeding_twice_updates_content_and_keeps_history(client, session):
    """A silo re-running its generate stage must not lose the earlier attempt."""
    run, user = owned_run(client, session)
    seed(session, run, user)
    service.seed_sections(
        session, run.id, {"purpose": "<p>Regenerated</p>"}, user["id"], "Regenerated"
    )

    sections = client.get(f"/api/documents/{run.id}/sections").json()
    purpose = next(item for item in sections if item["section_key"] == "purpose")
    assert purpose["content_html"] == "<p>Regenerated</p>"
    assert purpose["revision"] == 2

    versions = client.get(f"/api/documents/{run.id}/sections/purpose/versions").json()
    assert [version["label"] for version in versions] == ["Regenerated", "Generated"]


# ── sanitisation at the boundary ───────────────────────────────────────────────


def test_script_tags_are_stripped_from_saved_content(client, session):
    """Section HTML comes from a browser editor, so it is untrusted input."""
    run, user = owned_run(client, session)
    seed(session, run, user)

    response = client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": "<p>Safe</p><script>alert(1)</script>", "revision": 1},
    )
    assert response.status_code == 200

    stored = client.get(f"/api/documents/{run.id}/sections").json()[0]["content_html"]
    assert "script" not in stored.lower()
    assert "<p>Safe</p>" in stored


def test_event_handler_attributes_are_stripped(client, session):
    run, user = owned_run(client, session)
    seed(session, run, user)

    client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": '<p onclick="steal()">Text</p>', "revision": 1},
    )
    stored = client.get(f"/api/documents/{run.id}/sections").json()[0]["content_html"]
    assert "onclick" not in stored
    assert "Text" in stored


def test_formatting_the_emitter_understands_survives(client, session):
    """Sanitising must not strip the formatting the docx emitter actually renders."""
    run, user = owned_run(client, session)
    seed(session, run, user)

    html = (
        "<p><strong>Bold</strong> and <em>italic</em></p>"
        '<ul><li data-list="bullet">Item</li></ul>'
    )
    client.put(
        f"/api/documents/{run.id}/sections/purpose",
        json={"html": html, "revision": 1},
    )
    stored = client.get(f"/api/documents/{run.id}/sections").json()[0]["content_html"]
    for fragment in ("<strong>", "<em>", "<ul>", "<li", "data-list"):
        assert fragment in stored
