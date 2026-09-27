"""Who may reach which module.

This is the authorisation core: every silo router depends on `require_module`, and the
module's own docstring gives the reason it exists — "the alternative is every silo router
growing its own half-remembered version of it". Worth testing directly rather than only
through whichever endpoints happen to exercise it.

`discover_silos()` reads SILOS_DIR, which conftest points at tests/fixtures/silos, so the
set of known modules here is the fixture silo rather than bop/iso/mfg_atr.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.backend.da_platform.auth.access import (
    PROTECTED_ADMIN_USERNAMES,
    ROLE_ADMIN,
    ROLE_SUPER_USER,
    ROLE_USER,
    ROLES,
    accessible_module_ids,
    is_protected_admin,
    known_module_ids,
    require_admin,
    require_module,
    sees_every_module,
)
from api.backend.da_platform.db.models import User, UserModuleAccess


def make_user(session, username: str, role: str) -> User:
    user = User(
        username=username,
        display_name=username.replace(".", " ").title(),
        email=f"{username}@abbvie.com",
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def grant(session, user: User, module_id: str) -> None:
    session.add(UserModuleAccess(user_id=user.id, module_id=module_id))
    session.commit()


@pytest.fixture
def known_module(probe) -> str:
    """A module id that really exists, taken from the discovered fixture silo."""
    return probe.id


# ── the role table ────────────────────────────────────────────────────────────


def test_the_roles_are_ordered_most_privileged_first():
    # The admin UI renders them in this order, so the order is part of the contract.
    assert ROLES == (ROLE_ADMIN, ROLE_SUPER_USER, ROLE_USER)


def test_privileged_roles_see_every_module(session):
    for role in (ROLE_ADMIN, ROLE_SUPER_USER):
        user = make_user(session, f"sees.{role}", role)
        assert sees_every_module(user) is True


def test_a_plain_user_does_not_see_every_module(session):
    user = make_user(session, "plain.user", ROLE_USER)

    assert sees_every_module(user) is False


def test_an_unrecognised_role_is_not_privileged(session):
    """Fails closed: a role the code does not know must not widen access."""
    user = make_user(session, "odd.role", "auditor")

    assert sees_every_module(user) is False


# ── protected administrators ──────────────────────────────────────────────────


def test_the_protected_admins_are_recognised(session):
    for username in PROTECTED_ADMIN_USERNAMES:
        user = make_user(session, username, ROLE_ADMIN)
        assert is_protected_admin(user) is True


def test_protection_ignores_case_and_surrounding_space(session):
    # Usernames arrive from LDAP, so casing and stray whitespace are not guaranteed.
    sample = sorted(PROTECTED_ADMIN_USERNAMES)[0]
    user = make_user(session, f"  {sample.upper()}  ", ROLE_ADMIN)

    assert is_protected_admin(user) is True


def test_an_ordinary_admin_is_not_protected(session):
    user = make_user(session, "someone.else", ROLE_ADMIN)

    assert is_protected_admin(user) is False


def test_a_user_with_no_username_is_not_protected():
    # Never crash on the way to a security decision.
    assert is_protected_admin(User(username=None, display_name="x", email=None,
                                  role=ROLE_ADMIN)) is False


# ── which modules a user can reach ────────────────────────────────────────────


def test_the_known_modules_come_from_the_discovered_silos(probe):
    assert probe.id in known_module_ids()


def test_a_privileged_user_reaches_every_known_module(session, probe):
    admin = make_user(session, "admin.user", ROLE_ADMIN)

    assert accessible_module_ids(session, admin) == known_module_ids()


def test_a_plain_user_starts_with_nothing(session, probe):
    """A new account must not gain modules simply by existing."""
    user = make_user(session, "new.starter", ROLE_USER)

    assert accessible_module_ids(session, user) == set()


def test_a_granted_module_becomes_reachable(session, known_module):
    user = make_user(session, "granted.user", ROLE_USER)
    grant(session, user, known_module)

    assert accessible_module_ids(session, user) == {known_module}


def test_a_grant_for_a_silo_that_no_longer_exists_is_ignored(session, probe):
    """The intersection with the discovered silos is the point.

    A grant left behind by a removed silo must not widen access if an unrelated silo
    later claims that id.
    """
    user = make_user(session, "stale.grant", ROLE_USER)
    grant(session, user, "silo_that_was_deleted")

    assert accessible_module_ids(session, user) == set()


def test_one_user_s_grant_does_not_reach_another(session, known_module):
    granted = make_user(session, "has.access", ROLE_USER)
    other = make_user(session, "has.none", ROLE_USER)
    grant(session, granted, known_module)

    assert accessible_module_ids(session, other) == set()


# ── require_module ────────────────────────────────────────────────────────────


def test_require_module_allows_a_holder(session, known_module):
    user = make_user(session, "holder", ROLE_USER)
    grant(session, user, known_module)

    require_module(session, user, known_module)  # must not raise


def test_require_module_allows_a_privileged_user_without_any_grant(session, known_module):
    admin = make_user(session, "privileged", ROLE_SUPER_USER)

    require_module(session, admin, known_module)  # must not raise


def test_require_module_denies_a_user_without_the_grant(session, known_module):
    user = make_user(session, "denied", ROLE_USER)

    with pytest.raises(HTTPException) as raised:
        require_module(session, user, known_module)

    assert raised.value.status_code == 403


def test_the_denial_names_the_missing_module(session, known_module):
    """The UI says which access is missing rather than a bare Forbidden."""
    user = make_user(session, "denied.detail", ROLE_USER)

    with pytest.raises(HTTPException) as raised:
        require_module(session, user, known_module)

    detail = raised.value.detail
    assert detail["reason"] == "module_not_granted"
    assert detail["module"] == known_module
    assert "administrator" in detail["message"].lower()


def test_require_module_denies_an_unknown_module_even_for_a_holder(session):
    """A grant alone is not enough; the silo has to exist."""
    user = make_user(session, "holder.of.nothing", ROLE_USER)
    grant(session, user, "not_a_real_silo")

    with pytest.raises(HTTPException) as raised:
        require_module(session, user, "not_a_real_silo")

    assert raised.value.status_code == 403


# ── require_admin ─────────────────────────────────────────────────────────────


def test_require_admin_allows_an_admin(session):
    require_admin(make_user(session, "the.admin", ROLE_ADMIN))  # must not raise


@pytest.mark.parametrize("role", [ROLE_SUPER_USER, ROLE_USER, "auditor"])
def test_require_admin_denies_everyone_else(session, role):
    # A super_user sees every module but must not manage users.
    user = make_user(session, f"not.admin.{role}", role)

    with pytest.raises(HTTPException) as raised:
        require_admin(user)

    assert raised.value.status_code == 403
    assert raised.value.detail["reason"] == "admin_only"
