"""The user-management API: roles, module grants, and access-request decisions.

This router is the whole administrative security surface. Every endpoint on it is
`require_admin`-gated, and three of its rules exist only to stop an administrator locking
everybody out of user management: the protected-admin constant, the last-admin guard, and
the refusal to grant a module that no silo provides. Those rules cannot be verified by
reading the DB schema -- they live in this router and in `auth/access.py` -- so they are
exercised here through real HTTP calls with the resulting database state asserted.

SILOS_DIR points at tests/fixtures/silos, so `probe` is the only module id that
`known_module_ids()` will ever admit. Anything else ("bop", "iso") is a useful stand-in for
"a module id the deployment does not provide", which is exactly what the 422 paths guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.backend.da_platform.auth.access import (
    PROTECTED_ADMIN_USERNAMES,
    ROLE_ADMIN,
    ROLE_SUPER_USER,
    ROLE_USER,
)
from api.backend.da_platform.db.models import AccessRequest, User, UserModuleAccess
from tests.conftest import login

AUDIT_LOGGER = "api.backend.da_platform.audit"


# ── helpers (copied rather than shared: conftest and test_access.py are off limits) ──


def make_user(session, username: str, role: str, *, email: str | None = None) -> User:
    user = User(
        username=username,
        display_name=username.replace(".", " ").title(),
        # Left None by default so `notify` finds no owner address and never opens a socket.
        email=email,
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def grant(session, user: User, module_id: str) -> None:
    session.add(UserModuleAccess(user_id=user.id, module_id=module_id))
    session.commit()


def by_username(session, username: str) -> User:
    session.expire_all()
    return session.scalars(select(User).where(User.username == username)).one()


def login_as_admin(client, session, username: str = "asha.rao") -> User:
    """Sign in through the dev provider, then promote the row.

    `login()` leaves the account on role="user", which every endpoint here refuses, and the
    protected administrators are not in DEV_AUTH_USERS so they cannot be signed in at all.
    """
    login(client, username)
    user = by_username(session, username)
    user.role = ROLE_ADMIN
    session.commit()
    session.refresh(user)
    return user


def make_request_row(
    session,
    user: User,
    modules: list[str],
    *,
    status: str = "pending",
    note: str | None = None,
    created_at: datetime | None = None,
    decided_by: str | None = None,
) -> AccessRequest:
    row = AccessRequest(
        user_id=user.id,
        modules=list(modules),
        status=status,
        note=note,
        decided_by=decided_by,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def modules_of(session, user: User) -> list[str]:
    session.expire_all()
    return sorted(
        session.scalars(
            select(UserModuleAccess.module_id).where(
                UserModuleAccess.user_id == user.id
            )
        ).all()
    )


PROTECTED = sorted(PROTECTED_ADMIN_USERNAMES)[0]


# ── the admin-only gate ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/admin/roles", None),
        ("get", "/api/admin/users", None),
        ("get", "/api/admin/access-requests", None),
        ("put", "/api/admin/users/{uid}/role", {"role": ROLE_ADMIN}),
        ("put", "/api/admin/users/{uid}/modules", {"modules": []}),
        ("post", "/api/admin/access-requests/any-id/approve", {}),
        ("post", "/api/admin/access-requests/any-id/reject", {}),
    ],
)
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_SUPER_USER])
def test_every_admin_endpoint_refuses_a_caller_who_is_not_an_administrator(
    client, session, probe, method, path, body, role
):
    # A super_user reaches every module but must not manage users; if any endpoint here
    # ever answered 200 for one, the whole router would be open to them.
    login(client, "asha.rao")
    caller = by_username(session, "asha.rao")
    caller.role = role
    session.commit()

    response = getattr(client, method)(
        path.format(uid=caller.id), **({"json": body} if body is not None else {})
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "admin_only"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/admin/roles"),
        ("get", "/api/admin/users"),
        ("get", "/api/admin/access-requests"),
    ],
)
def test_the_admin_endpoints_refuse_an_anonymous_caller(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ── the role catalogue ────────────────────────────────────────────────────────


def test_the_role_list_is_the_three_roles_most_privileged_first(client, session, probe):
    # The admin UI renders the dropdown in this order, so the order is the contract.
    login_as_admin(client, session)

    body = client.get("/api/admin/roles").json()

    assert [row["id"] for row in body] == [ROLE_ADMIN, ROLE_SUPER_USER, ROLE_USER]
    assert [row["label"] for row in body] == ["Admin", "Super user", "User"]


# ── listing users ─────────────────────────────────────────────────────────────


def test_the_user_list_is_ordered_by_display_name(client, session, probe):
    login_as_admin(client, session)
    make_user(session, "zoe.zane", ROLE_USER)
    make_user(session, "abe.able", ROLE_USER)

    names = [row["display_name"] for row in client.get("/api/admin/users").json()]

    assert names == sorted(names)
    assert {"Abe Able", "Zoe Zane", "Asha Rao"} <= set(names)


@pytest.mark.parametrize("role", [ROLE_ADMIN, ROLE_SUPER_USER])
def test_a_privileged_user_is_listed_as_holding_every_module_by_role(
    client, session, probe, role
):
    """Their access is real, it just comes from the role, and the UI must show it as such
    rather than as an empty set of checkboxes."""
    login_as_admin(client, session)
    target = make_user(session, f"privileged.{role}", role)

    row = next(
        row for row in client.get("/api/admin/users").json() if row["id"] == target.id
    )

    assert row["modules"] == [probe.id]
    assert row["modules_from_role"] is True
    assert row["protected"] is False


def test_a_plain_users_listed_modules_are_their_grants_only(client, session, probe):
    login_as_admin(client, session)
    target = make_user(session, "granted.one", ROLE_USER)
    grant(session, target, probe.id)
    ungranted = make_user(session, "granted.none", ROLE_USER)

    rows = {row["id"]: row for row in client.get("/api/admin/users").json()}

    assert rows[target.id]["modules"] == [probe.id]
    assert rows[target.id]["modules_from_role"] is False
    assert rows[ungranted.id]["modules"] == []


def test_a_stale_grant_for_a_removed_silo_is_not_listed(client, session, probe):
    """`accessible_module_ids` intersects with discovered silos, so a leftover row must not
    show up as access the person still has."""
    login_as_admin(client, session)
    target = make_user(session, "stale.holder", ROLE_USER)
    grant(session, target, "silo_that_was_deleted")

    rows = {row["id"]: row for row in client.get("/api/admin/users").json()}

    assert rows[target.id]["modules"] == []


def test_a_fixed_administrator_is_flagged_as_protected(client, session, probe):
    login_as_admin(client, session)
    target = make_user(session, PROTECTED, ROLE_ADMIN)

    rows = {row["id"]: row for row in client.get("/api/admin/users").json()}

    assert rows[target.id]["protected"] is True


# ── changing a role ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_role", ["auditor", "", "Admin", "superuser"])
def test_an_unknown_role_is_rejected_and_names_the_valid_ones(
    client, session, probe, bad_role
):
    login_as_admin(client, session)
    target = make_user(session, "role.target", ROLE_USER)

    response = client.put(
        f"/api/admin/users/{target.id}/role", json={"role": bad_role}
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "super_user" in detail and "admin" in detail
    assert by_username(session, "role.target").role == ROLE_USER


@pytest.mark.parametrize("suffix", ["role", "modules"])
def test_an_unknown_user_id_is_a_404(client, session, probe, suffix):
    login_as_admin(client, session)
    body = {"role": ROLE_USER} if suffix == "role" else {"modules": []}

    response = client.put(f"/api/admin/users/does-not-exist/{suffix}", json=body)

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown user"


@pytest.mark.parametrize("new_role", [ROLE_USER, ROLE_SUPER_USER, ROLE_ADMIN])
def test_a_fixed_administrators_role_cannot_be_changed_by_another_admin(
    client, session, probe, new_role
):
    """The last-admin guard alone is not enough: it only keeps *someone* an admin, so
    without this a third admin could demote both fixed ones and own user management."""
    login_as_admin(client, session)
    protected = make_user(session, PROTECTED, ROLE_ADMIN)

    response = client.put(
        f"/api/admin/users/{protected.id}/role", json={"role": new_role}
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "protected_admin"
    assert "code change" in detail["message"]
    assert by_username(session, PROTECTED).role == ROLE_ADMIN


def test_protection_survives_odd_casing_from_the_directory(client, session, probe):
    # Usernames arrive from LDAP, where casing is not guaranteed.
    login_as_admin(client, session)
    protected = make_user(session, PROTECTED.upper(), ROLE_ADMIN)

    response = client.put(
        f"/api/admin/users/{protected.id}/role", json={"role": ROLE_USER}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "protected_admin"


def test_the_only_administrator_cannot_demote_themselves(client, session, probe):
    """Without this guard one careless change locks everybody out of user management and
    the only way back is hand-editing the database."""
    admin = login_as_admin(client, session)

    response = client.put(f"/api/admin/users/{admin.id}/role", json={"role": ROLE_USER})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "last_admin"
    assert "Promote someone else" in detail["message"]
    assert by_username(session, "asha.rao").role == ROLE_ADMIN


def test_the_only_administrator_can_be_demoted_once_a_second_one_exists(
    client, session, probe
):
    admin = login_as_admin(client, session)
    make_user(session, "second.admin", ROLE_ADMIN)

    response = client.put(f"/api/admin/users/{admin.id}/role", json={"role": ROLE_USER})

    assert response.status_code == 200, response.text
    assert response.json()["role"] == ROLE_USER
    assert by_username(session, "asha.rao").role == ROLE_USER


def test_reasserting_the_admin_role_on_the_last_admin_is_allowed(client, session, probe):
    # The guard is about *losing* the role, not about writing it again; an idempotent
    # save from the admin screen must not be a 409.
    admin = login_as_admin(client, session)

    response = client.put(f"/api/admin/users/{admin.id}/role", json={"role": ROLE_ADMIN})

    assert response.status_code == 200, response.text
    assert by_username(session, "asha.rao").role == ROLE_ADMIN


def test_promoting_a_plain_user_gives_them_every_module_without_any_grant_rows(
    client, session, probe
):
    login_as_admin(client, session)
    target = make_user(session, "to.promote", ROLE_USER)

    response = client.put(
        f"/api/admin/users/{target.id}/role", json={"role": ROLE_SUPER_USER}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == ROLE_SUPER_USER
    assert body["modules"] == [probe.id]
    assert body["modules_from_role"] is True
    # The grant table is untouched: role access is not materialised into rows.
    assert modules_of(session, target) == []


def test_a_role_change_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_as_admin(client, session)
    target = make_user(session, "audited.user", ROLE_USER)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.put(
            f"/api/admin/users/{target.id}/role", json={"role": ROLE_SUPER_USER}
        )

    entry = next(r.getMessage() for r in caplog.records if "role_changed" in r.getMessage())
    assert "user=asha.rao" in entry
    assert "audited.user: user -> super_user" in entry


# ── the access-request queue ──────────────────────────────────────────────────


def test_the_queue_shows_only_pending_requests_oldest_first(client, session, probe):
    """Oldest first is the order an admin is meant to work in."""
    login_as_admin(client, session)
    older = make_user(session, "waited.longer", ROLE_USER)
    newer = make_user(session, "waited.less", ROLE_USER)
    decided = make_user(session, "already.done", ROLE_USER)
    now = datetime.now(timezone.utc)
    make_request_row(session, newer, [probe.id], created_at=now - timedelta(minutes=1))
    make_request_row(session, older, [probe.id], created_at=now - timedelta(hours=2))
    make_request_row(session, decided, [probe.id], status="approved")

    body = client.get("/api/admin/access-requests").json()

    assert [row["username"] for row in body] == ["waited.longer", "waited.less"]


def test_the_history_view_includes_decided_requests_newest_first(client, session, probe):
    login_as_admin(client, session)
    requester = make_user(session, "has.history", ROLE_USER)
    now = datetime.now(timezone.utc)
    make_request_row(
        session, requester, [probe.id], status="rejected",
        created_at=now - timedelta(days=2),
    )
    make_request_row(
        session, requester, [probe.id], status="withdrawn",
        created_at=now - timedelta(days=1),
    )
    make_request_row(session, requester, [probe.id], created_at=now)

    body = client.get(
        "/api/admin/access-requests", params={"include_decided": True}
    ).json()

    assert [row["status"] for row in body] == ["pending", "withdrawn", "rejected"]


def test_a_queued_request_carries_who_asked_and_what_for(client, session, probe):
    login_as_admin(client, session)
    requester = make_user(session, "asked.nicely", ROLE_USER, email="a@b.com")
    row = make_request_row(session, requester, [probe.id, "bop"], note="need it")

    entry = client.get("/api/admin/access-requests").json()[0]

    assert entry["id"] == row.id
    assert entry["user_id"] == requester.id
    assert entry["display_name"] == "Asked Nicely"
    assert entry["email"] == "a@b.com"
    assert entry["modules"] == [probe.id, "bop"]
    assert entry["note"] == "need it"
    assert entry["decided_at"] is None and entry["decided_by"] is None


# ── approving ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_deciding_an_unknown_request_is_a_404(client, session, probe, decision):
    login_as_admin(client, session)

    response = client.post(
        f"/api/admin/access-requests/no-such-id/{decision}", json={}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown request"


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("already", ["approved", "rejected", "withdrawn"])
def test_a_request_can_only_be_decided_once(client, session, probe, decision, already):
    """Two admins on the queue at the same time must not both get to decide."""
    login_as_admin(client, session)
    requester = make_user(session, "decided.twice", ROLE_USER)
    row = make_request_row(
        session, requester, [probe.id], status=already, decided_by="someone.else"
    )

    response = client.post(
        f"/api/admin/access-requests/{row.id}/{decision}", json={}
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "already_decided"
    assert already in detail["message"]
    assert "by someone.else." in detail["message"]


def test_the_already_decided_message_copes_with_no_recorded_decider(
    client, session, probe
):
    # Withdrawn requests are decided by the requester, so `decided_by` is never set.
    login_as_admin(client, session)
    requester = make_user(session, "withdrew.it", ROLE_USER)
    row = make_request_row(session, requester, [probe.id], status="withdrawn")

    response = client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["message"].endswith("withdrawn.")


def test_approving_grants_the_requested_module_and_records_the_decision(
    client, session, probe
):
    login_as_admin(client, session)
    requester = make_user(session, "gets.access", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    response = client.post(
        f"/api/admin/access-requests/{row.id}/approve",
        json={"note": "  approved by phone  "},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == requester.id
    assert body["modules"] == [probe.id]
    assert body["modules_from_role"] is False
    assert modules_of(session, requester) == [probe.id]

    session.expire_all()
    stored = session.get(AccessRequest, row.id)
    assert stored.status == "approved"
    assert stored.decided_by == "asha.rao"
    assert stored.decided_at is not None
    assert stored.decision_note == "approved by phone"


def test_the_grant_records_which_admin_made_it(client, session, probe):
    login_as_admin(client, session)
    requester = make_user(session, "granted.by.admin", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    session.expire_all()
    granted_by = session.scalars(
        select(UserModuleAccess.granted_by).where(
            UserModuleAccess.user_id == requester.id
        )
    ).all()
    assert granted_by == ["asha.rao"]


@pytest.mark.parametrize("note", [None, "", "   "])
def test_a_blank_decision_note_is_stored_as_null(client, session, probe, note):
    login_as_admin(client, session)
    requester = make_user(session, "no.note", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    client.post(f"/api/admin/access-requests/{row.id}/approve", json={"note": note})

    session.expire_all()
    assert session.get(AccessRequest, row.id).decision_note is None


def test_an_admin_can_approve_part_of_a_request(client, session, probe):
    """The common answer is "you can have this one but not that one"; the rest of the
    request is closed with it rather than left dangling."""
    login_as_admin(client, session)
    requester = make_user(session, "partial.grant", ROLE_USER)
    row = make_request_row(session, requester, [probe.id, "bop"])

    response = client.post(
        f"/api/admin/access-requests/{row.id}/approve",
        json={"modules": [probe.id]},
    )

    assert response.status_code == 200, response.text
    assert modules_of(session, requester) == [probe.id]
    session.expire_all()
    assert session.get(AccessRequest, row.id).status == "approved"


def test_approving_adds_to_existing_access_rather_than_replacing_it(
    client, session, probe
):
    """A request is for *more* access, so approving one must not drop a grant made
    separately -- even one for a silo this deployment no longer provides."""
    login_as_admin(client, session)
    requester = make_user(session, "already.has.some", ROLE_USER)
    grant(session, requester, "kept_from_before")
    row = make_request_row(session, requester, [probe.id])

    client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    assert modules_of(session, requester) == ["kept_from_before", probe.id]


def test_approving_a_module_the_user_already_holds_does_not_duplicate_the_grant(
    client, session, probe
):
    login_as_admin(client, session)
    requester = make_user(session, "double.grant", ROLE_USER)
    grant(session, requester, probe.id)
    row = make_request_row(session, requester, [probe.id])

    response = client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    assert response.status_code == 200, response.text
    assert modules_of(session, requester) == [probe.id]


def test_approving_a_request_for_a_module_no_silo_provides_is_rejected(
    client, session, probe
):
    """A grant for an id the registry does not know is dead weight: `accessible_module_ids`
    intersects with the discovered silos, so it would never take effect."""
    login_as_admin(client, session)
    requester = make_user(session, "asked.for.ghost", ROLE_USER)
    row = make_request_row(session, requester, ["bop", "iso"])

    response = client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Unknown module(s): bop, iso"
    assert modules_of(session, requester) == []
    session.expire_all()
    assert session.get(AccessRequest, row.id).status == "pending"


def test_an_admin_cannot_smuggle_an_unrequested_module_into_an_approval(
    client, session, probe
):
    """Approving is authorised by what the person asked for; widening it here would leave
    an audit trail that does not match the grant. Direct editing is the honest route."""
    login_as_admin(client, session)
    requester = make_user(session, "asked.for.one", ROLE_USER)
    row = make_request_row(session, requester, ["bop"])

    response = client.post(
        f"/api/admin/access-requests/{row.id}/approve", json={"modules": [probe.id]}
    )

    assert response.status_code == 422, response.text
    assert "not requested" in response.json()["detail"]
    assert probe.id in response.json()["detail"]
    assert modules_of(session, requester) == []


def test_approving_an_empty_request_grants_nothing_but_still_closes_it(
    client, session, probe, caplog
):
    """Characterization: a request row with no modules can be approved, granting nothing.

    Suspected defect (routers/admin.py:195) -- `payload.modules or list(request.modules)`
    yields an empty `granting`, so the endpoint answers 200, closes the request and audits
    "nothing". Arguably it should be a 422, since approving a request for nothing is
    meaningless; the request-creation path already refuses an empty module list
    (routers/access_requests.py:85). Asserting today's behaviour so a change is visible.
    """
    login_as_admin(client, session)
    requester = make_user(session, "asked.for.nothing", ROLE_USER)
    row = make_request_row(session, requester, [])

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        response = client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    assert response.status_code == 200, response.text
    assert modules_of(session, requester) == []
    session.expire_all()
    assert session.get(AccessRequest, row.id).status == "approved"
    assert any(
        "access_request_approved" in r.getMessage() and "nothing" in r.getMessage()
        for r in caplog.records
    )


def test_an_approval_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_as_admin(client, session)
    requester = make_user(session, "audited.approval", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.post(f"/api/admin/access-requests/{row.id}/approve", json={})

    entry = next(
        r.getMessage()
        for r in caplog.records
        if "access_request_approved" in r.getMessage()
    )
    assert "user=asha.rao" in entry
    assert f"audited.approval: {probe.id}" in entry


# ── rejecting ─────────────────────────────────────────────────────────────────


def test_rejecting_records_the_decision_and_grants_nothing(client, session, probe):
    login_as_admin(client, session)
    requester = make_user(session, "turned.down", ROLE_USER)
    row = make_request_row(session, requester, [probe.id], note="please")

    response = client.post(
        f"/api/admin/access-requests/{row.id}/reject",
        json={"note": "  not this quarter  "},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == row.id
    assert body["status"] == "rejected"
    assert body["decided_by"] == "asha.rao"
    assert body["decision_note"] == "not this quarter"
    assert body["note"] == "please"  # the original ask is preserved alongside the answer
    assert body["decided_at"] is not None
    assert modules_of(session, requester) == []


def test_rejecting_a_request_for_an_unknown_module_still_works(client, session, probe):
    # No grant happens, so there is nothing to validate the module ids against.
    login_as_admin(client, session)
    requester = make_user(session, "rejected.ghost", ROLE_USER)
    row = make_request_row(session, requester, ["bop"])

    response = client.post(f"/api/admin/access-requests/{row.id}/reject", json={})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"


def test_a_rejection_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_as_admin(client, session)
    requester = make_user(session, "audited.rejection", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.post(f"/api/admin/access-requests/{row.id}/reject", json={})

    entry = next(
        r.getMessage()
        for r in caplog.records
        if "access_request_rejected" in r.getMessage()
    )
    assert "audited.rejection" in entry


def test_a_rejected_request_leaves_the_queue(client, session, probe):
    login_as_admin(client, session)
    requester = make_user(session, "off.the.queue", ROLE_USER)
    row = make_request_row(session, requester, [probe.id])

    client.post(f"/api/admin/access-requests/{row.id}/reject", json={})

    assert client.get("/api/admin/access-requests").json() == []


# ── editing module grants directly ────────────────────────────────────────────


def test_setting_modules_replaces_the_whole_set(client, session, probe):
    """The admin screen edits a set of checkboxes, so the write is a replace and cannot
    half-apply."""
    login_as_admin(client, session)
    target = make_user(session, "checkbox.user", ROLE_USER)
    grant(session, target, "left_over")

    response = client.put(
        f"/api/admin/users/{target.id}/modules", json={"modules": [probe.id]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["modules"] == [probe.id]
    assert modules_of(session, target) == [probe.id]


def test_setting_modules_to_nothing_revokes_everything(client, session, probe):
    login_as_admin(client, session)
    target = make_user(session, "revoked.user", ROLE_USER)
    grant(session, target, probe.id)

    response = client.put(f"/api/admin/users/{target.id}/modules", json={"modules": []})

    assert response.status_code == 200, response.text
    assert response.json()["modules"] == []
    assert modules_of(session, target) == []


def test_setting_modules_is_idempotent(client, session, probe):
    login_as_admin(client, session)
    target = make_user(session, "repeat.write", ROLE_USER)

    for _ in range(2):
        response = client.put(
            f"/api/admin/users/{target.id}/modules",
            json={"modules": [probe.id, probe.id]},
        )
        assert response.status_code == 200, response.text

    assert modules_of(session, target) == [probe.id]


def test_granting_a_module_no_silo_provides_is_rejected(client, session, probe):
    """Rejected rather than stored: the intersection in `accessible_module_ids` would make
    such a row a silent no-op, and a write that means nothing is worse than an error."""
    login_as_admin(client, session)
    target = make_user(session, "ghost.grant", ROLE_USER)

    response = client.put(
        f"/api/admin/users/{target.id}/modules", json={"modules": [probe.id, "iso"]}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Unknown module(s): iso"
    # The whole write is refused, including the module that was valid.
    assert modules_of(session, target) == []


def test_a_fixed_administrators_modules_cannot_be_edited(client, session, probe):
    """They hold every module by role, so a grant would be a silent no-op."""
    login_as_admin(client, session)
    protected = make_user(session, PROTECTED, ROLE_ADMIN)

    response = client.put(
        f"/api/admin/users/{protected.id}/modules", json={"modules": [probe.id]}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "protected_admin"
    assert modules_of(session, protected) == []


def test_a_module_change_is_written_to_the_audit_trail(client, session, probe, caplog):
    login_as_admin(client, session)
    target = make_user(session, "audited.modules", ROLE_USER)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        client.put(
            f"/api/admin/users/{target.id}/modules", json={"modules": [probe.id]}
        )
        client.put(f"/api/admin/users/{target.id}/modules", json={"modules": []})

    entries = [
        r.getMessage() for r in caplog.records if "modules_changed" in r.getMessage()
    ]
    assert f"audited.modules: {probe.id}" in entries[0]
    assert "audited.modules: none" in entries[1]


def test_an_admin_can_edit_their_own_module_grants(client, session, probe):
    """Characterization: the write is accepted even though role access makes it moot.

    An admin sees every module by role, so the stored rows change nothing that
    `accessible_module_ids` returns -- the response still reports every module and
    `modules_from_role` stays true. Only the *protected* admins are refused
    (routers/admin.py:270). Recording today's behaviour: if the protection were widened to
    all admins for the same "silent no-op" reason, this would become a 409.
    """
    admin = login_as_admin(client, session)

    response = client.put(
        f"/api/admin/users/{admin.id}/modules", json={"modules": []}
    )

    assert response.status_code == 200, response.text
    assert response.json()["modules"] == [probe.id]
    assert response.json()["modules_from_role"] is True
