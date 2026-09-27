"""Verify LDAP authentication with your own credentials.

Nothing is stored and no password is echoed. Run this before switching the API to
AUTH_PROVIDER=ldap.

Run:  python -m scripts.check_ldap [username]
"""

from __future__ import annotations

import getpass
import logging
import ssl
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api.backend.da_platform.auth.providers import LdapAuthProvider  # noqa: E402
from api.backend.da_platform.settings import settings  # noqa: E402


def check_tls() -> bool:
    """Confirm the CA bundle validates the directory certificate before binding,
    so a TLS problem is not misreported as bad credentials."""
    host = settings.ldap_url.split("//", 1)[-1].split(":")[0]
    port = 636
    if settings.ca_bundle_path is None:
        print(f"  CA bundle missing at {settings.ca_bundle_path}")
        print("  Generate it with: python scripts/build_ca_bundle.py")
        return False
    try:
        context = ssl.create_default_context(cafile=str(settings.ca_bundle_path))
        with socket.create_connection((host, port), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                issuer = dict(x[0] for x in tls.getpeercert()["issuer"])
                print(f"  TLS OK: {host}:{port} issued by {issuer.get('commonName')}")
        return True
    except Exception as exc:
        print(f"  TLS FAILED for {host}:{port}: {exc}")
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="  ldap3: %(message)s")

    print(f"Server:   {settings.ldap_url}")
    print(f"Domain:   {settings.ldap_domain}")
    print(f"CA bundle:{settings.ca_bundle_path}")
    print()

    print("Checking TLS...")
    if not check_tls():
        return 2
    print()

    username = sys.argv[1] if len(sys.argv) > 1 else input("Username (no domain): ")
    password = REDACTED

    provider = LdapAuthProvider(
        url=settings.ldap_url,
        domain=settings.ldap_domain,
        ca_bundle_path=settings.ca_bundle_path,
    )
    info = provider.authenticate(username, password)

    print()
    if info is None:
        print("RESULT: authentication FAILED")
        return 1

    print("RESULT: authentication SUCCEEDED")
    print(f"  username:     {info.username}")
    print(f"  display_name: {info.display_name}")
    print(f"  email:        {info.email or '(not readable)'}")
    if info.email is None:
        # The bind worked; only the self-lookup did not return attributes.
        print()
        print("  Note: the directory did not return displayName/mail, so the users")
        print("  row will fall back to a name derived from the username.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
