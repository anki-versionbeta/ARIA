"""Authentication providers.

Two implementations behind one protocol: the dev provider keeps local development
and tests free of a directory dependency, and the LDAP provider is what runs
everywhere else.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ldap3 import ALL, SIMPLE, Connection, Server, Tls
from ldap3.utils.conv import escape_filter_chars

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserInfo:
    username: str
    display_name: str
    email: str | None


class AuthProvider(Protocol):
    def authenticate(self, username: str, password: str) -> UserInfo | None: ...


def _prettify(username: str) -> str:
    return " ".join(part.capitalize() for part in username.replace(".", " ").split())


class DevAuthProvider:
    """Accepts any password for an explicit list of usernames.

    Fails closed: an unlisted username is rejected, and it is only ever selected
    by an explicit AUTH_PROVIDER=dev (spec section 9).
    """

    def __init__(self, usernames: list[str]) -> None:
        self._usernames = {name.strip().lower() for name in usernames if name.strip()}

    def authenticate(self, username: str, password: str) -> UserInfo | None:
        candidate = username.strip().lower()
        if not password or candidate not in self._usernames:
            return None
        return UserInfo(
            username=candidate,
            display_name=_prettify(candidate),
            email=f"{candidate}@abbvie.com",
        )


class LdapAuthProvider:
    """Active Directory authentication by direct simple bind on the UPN.

    A UPN bind (`user@domain`) needs no service account, bind DN, or base DN,
    which is why this does not use search-then-bind: it removes a dependency on
    directory-structure details we would otherwise have to be given.

    TLS is always verified. The reference implementation this replaces used
    `Tls(validate=ssl.CERT_NONE)`; the AbbVie chain in the CA bundle validates
    `ldap-ad.abbvienet.com` (issuer "AbbVie Global Sub CA01"), so there is no
    reason to disable verification.
    """

    def __init__(self, url: str, domain: str, ca_bundle_path: Path | None) -> None:
        self._url = url
        self._domain = domain
        self._ca_bundle_path = ca_bundle_path

    @property
    def _search_base(self) -> str:
        return ",".join(f"dc={part}" for part in self._domain.split("."))

    def _build_tls(self) -> Tls:
        return Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=str(self._ca_bundle_path) if self._ca_bundle_path else None,
        )

    def authenticate(self, username: str, password: str) -> UserInfo | None:
        candidate = username.strip()
        # A simple bind with an empty password is treated as an ANONYMOUS bind by
        # most directories and would otherwise succeed.
        if not candidate or not password:
            return None

        server = Server(self._url, use_ssl=True, tls=self._build_tls(), get_info=ALL)
        user_principal = f"{candidate}@{self._domain}"

        try:
            with Connection(
                server,
                user_principal,
                password=REDACTED
                authentication=SIMPLE,
                auto_bind=True,
            ) as connection:
                return self._read_own_entry(connection, candidate, user_principal)
        except Exception as exc:  # ldap3 raises several distinct bind failures
            logger.info("LDAP bind failed for %s: %s", candidate, exc)
            return None

    def _read_own_entry(
        self, connection: Connection, username: str, user_principal: str
    ) -> UserInfo:
        """Read displayName/mail for the user who just bound.

        A direct bind returns no attributes, but AD normally lets a user read
        their own entry, so the users row can be populated without a service
        account.
        """
        fallback = UserInfo(
            username=username.lower(),
            display_name=_prettify(username),
            email=None,
        )
        try:
            # Two filters, not one. AD accepts alternate UPN suffixes at bind time, so a
            # bind as `user@abbvienet.com` can succeed while the stored userPrincipalName
            # is `user@abbvie.com` — and then a UPN-only filter matches nothing and every
            # real user ends up displayed as their username. sAMAccountName is the short
            # login name and matches either way.
            #
            # Both values are escaped: they come from the login form, and an unescaped
            # `*` or `)` here would be LDAP filter injection.
            search_filter = (
                f"(|(userPrincipalName={escape_filter_chars(user_principal)})"
                f"(sAMAccountName={escape_filter_chars(username)}))"
            )
            found = connection.search(
                search_base=self._search_base,
                search_filter=search_filter,
                attributes=["displayName", "mail"],
            )
            if not found or not connection.entries:
                # Logged at warning, not swallowed silently: the symptom is a display name
                # that is really a username, which looks like a UI bug rather than a
                # directory-read failure.
                logger.warning(
                    "LDAP self-lookup found no entry for %s under %s; using the username "
                    "as the display name",
                    username,
                    self._search_base,
                )
                return fallback
            entry = connection.entries[0]
            display_name = str(getattr(entry, "displayName", "") or "").strip()
            email = str(getattr(entry, "mail", "") or "").strip()
            return UserInfo(
                username=username.lower(),
                display_name=display_name or fallback.display_name,
                email=email or None,
            )
        except Exception as exc:
            # Authentication already succeeded; failing to enrich must not deny access.
            logger.info("LDAP self-lookup failed for %s: %s", username, exc)
            return fallback


def build_provider() -> AuthProvider:
    from da_platform.settings import settings

    if settings.auth_provider == "dev":
        if not settings.is_local:
            raise RuntimeError("The dev auth provider must not be enabled outside local")
        logger.warning(
            "Using the DEV auth provider; any password is accepted for %s",
            ", ".join(settings.dev_auth_users),
        )
        return DevAuthProvider(settings.dev_auth_users)

    if settings.ca_bundle_path is None:
        # Refuse rather than silently falling back to the OS trust store, which
        # does not contain the AbbVie chain the directory certificate needs.
        raise RuntimeError(
            "CA_BUNDLE_PATH is missing or does not exist; LDAP requires it. "
            "Generate it with: python scripts/build_ca_bundle.py"
        )

    return LdapAuthProvider(
        url=settings.ldap_url,
        domain=settings.ldap_domain,
        ca_bundle_path=settings.ca_bundle_path,
    )
