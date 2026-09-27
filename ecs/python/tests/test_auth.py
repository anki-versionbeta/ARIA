from __future__ import annotations

import ssl
from dataclasses import replace

import pytest

from api.backend.da_platform import settings as settings_module
from api.backend.da_platform.auth.providers import (
    DevAuthProvider,
    LdapAuthProvider,
    build_provider,
)
from api.backend.da_platform.auth.tokens import COOKIE_NAME
from tests.conftest import login


def test_healthz_reports_database(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_me_requires_a_session(client):
    assert client.get("/api/auth/me").status_code == 401


def test_documents_require_a_session(client):
    assert client.get("/api/documents").status_code == 401


def test_login_rejects_unknown_user(client):
    response = client.post(
        "/api/auth/login", json={"username": "not.a.user", "password": "x"}
    )
    assert response.status_code == 401


def test_login_sets_httponly_cookie_and_creates_user(client):
    response = client.post(
        "/api/auth/login", json={"username": "asha.rao", "password": "anything"}
    )
    assert response.status_code == 200
    assert response.json()["username"] == "asha.rao"

    cookie_header = response.headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert COOKIE_NAME in cookie_header

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "Asha Rao"


def test_logout_clears_the_session(client):
    login(client)
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401


def test_silos_lists_whatever_the_registry_discovers(client):
    """Tests point SILOS_DIR at a fixture silo, so this asserts discovery works
    rather than that the list is empty."""
    login(client)
    assert [silo["id"] for silo in client.get("/api/silos").json()] == ["probe"]


def test_silos_requires_authentication(client):
    assert client.get("/api/silos").status_code == 401


def test_dev_provider_rejects_unlisted_users_and_blank_passwords():
    provider = DevAuthProvider(["asha.rao"])
    assert provider.authenticate("asha.rao", "anything") is not None
    assert provider.authenticate("someone.else", "anything") is None
    # A blank password must never authenticate.
    assert provider.authenticate("asha.rao", "") is None


def test_ldap_provider_rejects_blank_password_without_contacting_the_server():
    """An empty simple bind is treated as anonymous by most directories and would
    otherwise succeed, so it has to be refused before any connection is made."""
    provider = LdapAuthProvider(
        url="ldaps://unreachable.invalid:636", domain="abbvienet.com", ca_bundle_path=None
    )
    assert provider.authenticate("asha.rao", "") is None
    assert provider.authenticate("", "secret") is None


def test_ldap_search_base_is_derived_from_the_domain():
    provider = LdapAuthProvider(
        url="ldaps://x:636", domain="abbvienet.com", ca_bundle_path=None
    )
    # Derived rather than configured, which is why no base DN is needed.
    assert provider._search_base == "dc=abbvienet,dc=com"


class FakeEntry:
    def __init__(self, display_name: str = "", mail: str = "") -> None:
        self.displayName = display_name
        self.mail = mail


class FakeConnection:
    """Records the filter it was asked for and answers with `entries`."""

    def __init__(self, entries: list[FakeEntry] | None = None) -> None:
        self.entries = entries or []
        self.search_filter: str | None = None

    def search(self, *, search_base, search_filter, attributes):  # noqa: ARG002
        self.search_filter = search_filter
        return bool(self.entries)


def _provider() -> LdapAuthProvider:
    return LdapAuthProvider(
        url="ldaps://x:636", domain="abbvienet.com", ca_bundle_path=None
    )


def test_the_self_lookup_matches_on_sam_account_name_as_well_as_the_upn():
    """AD accepts alternate UPN suffixes at bind time, so a bind as `user@abbvienet.com`
    can succeed while the stored userPrincipalName is `user@abbvie.com`. A UPN-only filter
    then matches nothing and every real user is displayed as their username."""
    connection = FakeConnection([FakeEntry("Mohan Kumar", "mohan@abbvie.com")])

    info = _provider()._read_own_entry(
        connection, "mohanax25", "mohanax25@abbvienet.com"
    )

    assert "userPrincipalName=mohanax25@abbvienet.com" in connection.search_filter
    assert "sAMAccountName=mohanax25" in connection.search_filter
    assert connection.search_filter.startswith("(|")
    assert info.display_name == "Mohan Kumar"
    assert info.email == "mohan@abbvie.com"


def test_a_missing_directory_entry_falls_back_to_the_prettified_username():
    info = _provider()._read_own_entry(
        FakeConnection([]), "mohanax25", "mohanax25@abbvienet.com"
    )

    assert info.display_name == "Mohanax25"
    assert info.email is None


def test_an_empty_display_name_does_not_win_over_the_fallback():
    info = _provider()._read_own_entry(
        FakeConnection([FakeEntry("   ", "")]), "asha.rao", "asha.rao@abbvienet.com"
    )

    assert info.display_name == "Asha Rao"


def test_the_self_lookup_filter_escapes_the_username():
    """The username comes from the login form. Unescaped, a `*` or `)` here would be LDAP
    filter injection — `*` alone would match every entry in the directory."""
    connection = FakeConnection([FakeEntry("Someone")])

    _provider()._read_own_entry(connection, "a*b)c", "a*b)c@abbvienet.com")

    assert "a*b)c" not in connection.search_filter
    assert r"a\2ab\29c" in connection.search_filter


def test_ldap_tls_always_validates_the_certificate(tmp_path):
    """Guards against reintroducing `Tls(validate=ssl.CERT_NONE)`, which is what
    the app being replaced did and what spec section 9 sets out to fix."""
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    tls = LdapAuthProvider(
        url="ldaps://x:636", domain="abbvienet.com", ca_bundle_path=bundle
    )._build_tls()

    assert tls.validate == ssl.CERT_REQUIRED
    assert tls.ca_certs_file == str(bundle)


def test_build_provider_refuses_ldap_without_a_ca_bundle(monkeypatch):
    """Falling back to the OS trust store would fail, because it does not contain
    the AbbVie chain that signs the directory certificate."""
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, auth_provider="ldap", ca_bundle_path=None),
    )
    with pytest.raises(RuntimeError, match="CA_BUNDLE_PATH"):
        build_provider()


def test_build_provider_returns_ldap_when_configured(monkeypatch, tmp_path):
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, auth_provider="ldap", ca_bundle_path=bundle),
    )
    assert isinstance(build_provider(), LdapAuthProvider)


def test_dev_provider_is_refused_outside_local(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, env="dev", auth_provider="dev"),
    )
    with pytest.raises(RuntimeError, match="must not be enabled outside local"):
        build_provider()
