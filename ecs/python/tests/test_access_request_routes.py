"""Asking for module access: the requester half of the flow.

The rules worth pinning down here are the ones a reader cannot infer from the schema. At
most one open request per person, so re-asking edits the open row instead of filing a
second one. Asking for what you already hold is refused, because it means the screen is
stale rather than that an admin has work to do. Withdrawing is idempotent, because
"nothing open" is already the desired end state. And the notification is sent after the
commit, so a mail failure can never lose a saved request.

SILOS_DIR points at tests/fixtures/silos, so `probe` is the only module id the registry
knows; "bop"/"iso" stand in for a module this deployment does not provide.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from api.backend.da_platform.auth.access import (
    ROLE_ADMIN,
    ROLE_SUPER_USER,
    ROLE_USER,
)
from api.backend.da_platform.db.models import AccessRequest, User, UserModuleAccess
from tests.conftest import login

AUDIT_LOGGER = "api.backend.da_platform.audit"


# ── helpers (copied: conftest and the existing test files are off limits) ──────


def by_username(session, username: str) -> User:
    session.expire_all()
    return session.scalars(select(User).where(User.username == username)).one()


def login_with_no_access(client, session, username: str = "asha.rao") -> User:
    """Sign in and then strip the grants `conftest.login` hands out.

    Most of this router's behaviour only shows up for somebody who does *not* already hold
    the module, which is precisely the person the request form exists for.
    """
    login(client, username)
    user = by_username(session, username)
    session.execute(delete(UserModuleAccess).where(UserModuleAccess.user_id == user.id))
    session.commit()
    session.refresh(user)
    return user


def set_role(session, user: User, role: str) -> None:
    user.role = role
    session.commit()
    session.refresh(user)


def requests_of(session, user: User) -> list[AccessRequest]:
    session.expire_all()
    return list(
        session.scalars(
            select(AccessRequest)
            .where(AccessRequest.user_id == user.id)
            .order_by(AccessRequest.created_at)
        ).unique()
    )


# ── the module catalogue ──────────────────────────────────────────────────────


def test_the_module_catalogue_is_visible_to_a_user_with_no_access_at_all(
    client, session, probe
):
    """Not admin-only, and it cannot be: the request form is shown to somebody holding
    nothing, and a form that cannot list its options is not a form."""
    login_with_no_access(client, session)

    body = client.get("/api/modules").json()

    assert body == [{"id": probe.id, "label": probe.label}]


def test_the_module_catalogue_still_requires_a_session(client):
    # Open to every signed-in user, not to the internet.
    assert client.get("/api/modules").status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/access-requests/mine"),
        ("post", "/api/access-requests"),
        ("delete", "/api/access-requests/mine"),
    ],
)
def test_the_request_endpoints_refuse_an_anonymous_caller(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ── "what have I asked for" ───────────────────────────────────────────────────


def test_having_asked_for_nothing_is_null_rather_than_a_404(client, session, probe):
    """An ordinary state for this screen, not an error; a 404 would make the frontend
    treat it as one."""
    login_with_no_access(client, session)

    response = client.get("/api/access-requests/mine")

    assert response.status_code == 200
    assert response.json() is None


def test_mine_returns_the_callers_open_request(client, session, probe):
    login_with_no_access(client, session)
    client.post(
        "/api/access-requests", json={"modules": [probe.id], "note": "for the audit"}
    )

    body = client.get("/api/access-requests/mine").json()

    assert body["modules"] == [probe.id]
    assert body["note"] == "for the audit"
    assert body["status"] == "pending"
    assert body["username"] == "asha.rao"
    assert body["decided_at"] is None


def test_mine_does_not_show_another_users_request(client, session, probe):
    """Two people, one shared endpoint: the row must be selected by the caller's id."""
    caller = login_with_no_access(client, session)
    other = User(
        username="somebody.else", display_name="Somebody Else", email=None, role=ROLE_USER
    )
    session.add(other)
    session.commit()
    session.add(AccessRequest(user_id=other.id, modules=[probe.id], status="pending"))
    session.commit()

    assert client.get("/api/access-requests/mine").json() is None
    assert requests_of(session, caller) == []


# ── filing a request ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("modules", [[], ["   "], ["", " "]])
def test_a_request_for_nothing_is_rejected(client, session, probe, modules):
    login_with_no_access(client, session)

    response = client.post("/api/access-requests", json={"modules": modules})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Choose at least one module to request."


def test_a_request_for_a_module_no_silo_provides_is_rejected(client, session, probe):
    """Refused at the door: a stored request for an unknown id could never be approved,
    since the approval path validates against the same registry."""
    login_with_no_access(client, session)
    caller = by_username(session, "asha.rao")

    response = client.post(
        "/api/access-requests", json={"modules": ["iso", "bop", probe.id]}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Unknown module(s): bop, iso"
    assert requests_of(session, caller) == []


def test_a_request_is_created_pending_and_attributed_to_the_caller(
    client, session, probe
):
    caller = login_with_no_access(client, session)

    response = client.post(
        "/api/access-requests", json={"modules": [probe.id], "note": "  new starter  "}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["user_id"] == caller.id
    assert body["username"] == "asha.rao"
    assert body["modules"] == [probe.id]
    assert body["note"] == "new starter"
    assert body["decided_by"] is None

    rows = requests_of(session, caller)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    # Filing a request must not grant anything by itself.
    assert (
        session.scalars(
            select(UserModuleAccess.module_id).where(
                UserModuleAccess.user_id == caller.id
            )
        ).all()
        == []
    )


@pytest.mark.parametrize("note", [None, "", "    "])
def test_a_blank_note_is_stored_as_null(client, session, probe, note):
    caller = login_with_no_access(client, session)

    client.post("/api/access-requests", json={"modules": [probe.id], "note": note})

    assert requests_of(session, caller)[0].note is None


def test_the_requested_modules_are_trimmed_deduplicated_and_sorted(
    client, session, probe
):
    # The list arrives from a checkbox form, so duplicates and stray whitespace are
    # the client's problem to send and this endpoint's problem to normalise.
    login_with_no_access(client, session)

    body = client.post(
        "/api/access-requests",
        json={"modules": [f"  {probe.id}  ", probe.id, "", f"{probe.id}\t"]},
    ).json()

    assert body["modules"] == [probe.id]


def test_re_asking_updates_the_open_request_instead_of_filing_a_second_one(
    client, session, probe
):
    """One person waiting once is one row in the admin queue, however many times they
    press the button."""
    caller = login_with_no_access(client, session)
    first = client.post(
        "/api/access-requests", json={"modules": [probe.id], "note": "first try"}
    ).json()

    second = client.post(
        "/api/access-requests", json={"modules": [probe.id], "note": "changed my mind"}
    ).json()

    assert second["id"] == first["id"]
    assert second["note"] == "changed my mind"
    rows = requests_of(session, caller)
    assert len(rows) == 1
    assert rows[0].note == "changed my mind"


@pytest.mark.parametrize("role", [ROLE_ADMIN, ROLE_SUPER_USER])
def test_a_privileged_role_is_told_their_role_already_covers_everything(
    client, session, probe, role
):
    """Nothing for an admin to decide, so it is not queued for one."""
    caller = login_with_no_access(client, session)
    set_role(session, caller, role)

    response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "already_granted"
    assert detail["message"] == "Your role already gives you every module."
    assert requests_of(session, caller) == []


def test_asking_for_only_modules_you_already_hold_is_refused(client, session, probe):
    """A sign the screen is out of date rather than a thing to queue for an admin."""
    login(client, "asha.rao")  # grants every discovered module
    caller = by_username(session, "asha.rao")

    response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "already_granted"
    assert "Reload the page" in detail["message"]
    assert requests_of(session, caller) == []


def test_a_stale_grant_does_not_block_a_request_for_the_real_module(
    client, session, probe
):
    """The grant is for an id no silo provides, so it gives no access and must not be
    mistaken for the access being asked for."""
    caller = login_with_no_access(client, session)
    session.add(UserModuleAccess(user_id=caller.id, module_id="silo_that_was_deleted"))
    session.commit()

    response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 201, response.text


def test_filing_a_request_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_with_no_access(client, session)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.post("/api/access-requests", json={"modules": [probe.id]})

    entry = next(
        r.getMessage() for r in caplog.records if "access_requested" in r.getMessage()
    )
    assert "user=asha.rao" in entry
    assert probe.id in entry


def test_a_mail_failure_cannot_lose_a_saved_request(client, session, probe, caplog):
    """The notification runs after the commit and swallows everything, so an unreachable
    relay leaves the request queued rather than failing the call."""
    caller = login_with_no_access(client, session)
    # A fixed administrator with an address on file is what makes `notify` attempt a send;
    # the SMTP host in test settings does not exist, so the attempt fails.
    session.add(
        User(
            username="bapatar",
            display_name="Owner",
            email="owner@abbvie.com",
            role=ROLE_ADMIN,
        )
    )
    session.commit()

    with caplog.at_level(logging.WARNING, logger="api.backend.da_platform.notify"):
        response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 201, response.text
    assert requests_of(session, caller)[0].status == "pending"
    assert any("Could not email" in r.getMessage() for r in caplog.records)


def test_a_missing_owner_address_is_logged_and_the_request_is_still_filed(
    client, session, probe, caplog
):
    caller = login_with_no_access(client, session)

    with caplog.at_level(logging.WARNING, logger="api.backend.da_platform.notify"):
        response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 201, response.text
    assert requests_of(session, caller)[0].status == "pending"
    assert any("No owner email addresses" in r.getMessage() for r in caplog.records)


# ── the race the partial unique index catches ─────────────────────────────────


def test_a_simultaneous_duplicate_submission_returns_the_row_that_won(
    client, session, probe, monkeypatch
):
    """Two tabs submitting at once: one insert loses on the unique index, and the outcome
    is what the user wanted either way, so the loser answers with the winner's row.

    The partial unique index lives in migration 003 and `create_all()` does not build it,
    so the collision is provoked by making the commit fail the way the index would.
    """
    caller = login_with_no_access(client, session)
    winner = AccessRequest(user_id=caller.id, modules=[probe.id], status="pending")
    session.add(winner)
    session.commit()
    winner_id = winner.id
    session.expunge_all()

    def refuse_to_commit(self):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(SaSession, "commit", refuse_to_commit)

    response = client.post("/api/access-requests", json={"modules": [probe.id]})

    assert response.status_code == 201, response.text
    assert response.json()["id"] == winner_id


def test_a_commit_failure_with_nothing_to_fall_back_on_is_not_swallowed(
    client, session, probe, monkeypatch
):
    """Only the duplicate-request case is recoverable. Any other integrity failure has to
    surface rather than be reported as a successful request that was never saved."""
    login_with_no_access(client, session)

    def refuse_to_commit(self):
        raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    monkeypatch.setattr(SaSession, "commit", refuse_to_commit)

    with pytest.raises(IntegrityError):
        client.post("/api/access-requests", json={"modules": [probe.id]})


# ── withdrawing ───────────────────────────────────────────────────────────────


def test_withdrawing_an_open_request_takes_it_off_the_queue(client, session, probe):
    caller = login_with_no_access(client, session)
    client.post("/api/access-requests", json={"modules": [probe.id]})

    response = client.delete("/api/access-requests/mine")

    assert response.status_code == 204
    assert response.content == b""
    rows = requests_of(session, caller)
    assert [row.status for row in rows] == ["withdrawn"]
    # Kept rather than deleted: "who asked for this" stays answerable.
    assert rows[0].modules == [probe.id]
    assert client.get("/api/access-requests/mine").json() is None


def test_withdrawing_when_nothing_is_open_is_a_no_op(client, session, probe):
    """Idempotent: nothing open is already the desired state, so it is not an error."""
    caller = login_with_no_access(client, session)

    assert client.delete("/api/access-requests/mine").status_code == 204
    assert client.delete("/api/access-requests/mine").status_code == 204
    assert requests_of(session, caller) == []


def test_a_withdrawal_records_no_decision_time(client, session, probe):
    """Characterization: `decided_at` is explicitly cleared on withdrawal.

    Suspected defect (routers/access_requests.py:152) -- the row moves to a terminal state
    with no timestamp, so the history view can show a withdrawn request that appears never
    to have been decided, and `_load_pending`'s "already decided" message has no date to
    quote. Setting it to `utcnow()` looks like the intended behaviour. Asserting what the
    code does today.
    """
    caller = login_with_no_access(client, session)
    client.post("/api/access-requests", json={"modules": [probe.id]})

    client.delete("/api/access-requests/mine")

    row = requests_of(session, caller)[0]
    assert row.status == "withdrawn"
    assert row.decided_at is None
    assert row.decided_by is None


def test_withdrawing_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_with_no_access(client, session)
    client.post("/api/access-requests", json={"modules": [probe.id]})

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.delete("/api/access-requests/mine")

    assert any(
        "access_request_withdrawn" in r.getMessage() and "user=asha.rao" in r.getMessage()
        for r in caplog.records
    )


def test_a_new_request_can_be_filed_after_withdrawing(client, session, probe):
    """The withdrawn row is not in the way, because only pending rows are unique."""
    caller = login_with_no_access(client, session)
    first = client.post("/api/access-requests", json={"modules": [probe.id]}).json()
    client.delete("/api/access-requests/mine")

    second = client.post("/api/access-requests", json={"modules": [probe.id]}).json()

    assert second["id"] != first["id"]
    assert [row.status for row in requests_of(session, caller)] == [
        "withdrawn",
        "pending",
    ]


def test_withdrawing_does_not_touch_another_users_open_request(client, session, probe):
    login_with_no_access(client, session)
    other = User(
        username="untouched.user", display_name="Untouched", email=None, role=ROLE_USER
    )
    session.add(other)
    session.commit()
    session.add(AccessRequest(user_id=other.id, modules=[probe.id], status="pending"))
    session.commit()

    client.delete("/api/access-requests/mine")

    assert requests_of(session, other)[0].status == "pending"


def test_an_approved_request_is_no_longer_mine_to_see_or_withdraw(
    client, session, probe
):
    """`mine` is the *open* request, so a decided one drops out of the requester's view."""
    caller = login_with_no_access(client, session)
    client.post("/api/access-requests", json={"modules": [probe.id]})
    row = requests_of(session, caller)[0]
    row.status = "approved"
    row.decided_by = "an.admin"
    session.commit()

    assert client.get("/api/access-requests/mine").json() is None
    assert client.delete("/api/access-requests/mine").status_code == 204
    assert requests_of(session, caller)[0].status == "approved"
