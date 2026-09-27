"""Environment-driven configuration.

The package is `da_platform`, not `platform`: a top-level `platform` package shadows the
standard library module of the same name, which boto3, uvicorn and requests all import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Two anchors, because the move split things that used to sit side by side. Runtime
# artifacts (.env, certs, dev.db) stay at the project root; the silos are a sibling of
# this package under backend/, so they are found from there rather than from here.
PROJECT_ROOT = Path(__file__).resolve().parents[4]  # ecs/python
BACKEND_ROOT = Path(__file__).resolve().parents[1]  # ecs/python/src/api/backend

# Local development only. Any other environment must supply JWT_SECRET from the
# secret store, so a missing value fails closed rather than silently signing
# sessions with a value that is public in the repository.
_LOCAL_DEV_JWT_SECRET = REDACTED


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Load `.env` into the environment, without overriding what is already set.

    Keys are upper-cased because the credential files in circulation use lowercase
    names (`aws_access_key_id`). That only works by accident on Windows, where
    environment variables are case-insensitive; it silently fails on Linux.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    storage_dir: Path
    storage_backend: str
    silos_dir: Path
    max_upload_mb: int
    worker_heartbeat_s: int
    stale_claim_timeout_s: int
    jwt_secret: str
    jwt_ttl_hours: int
    cookie_secure: bool
    auth_provider: str
    dev_auth_users: list[str]
    ldap_url: str
    ldap_domain: str
    ca_bundle_path: Path | None
    # Notification mail. Best-effort: see api.backend.da_platform.notify for why a failure here must
    # never fail the thing being notified about.
    smtp_host: str
    smtp_port: int
    mail_from: str
    app_base_url: str
    enabled_silos: list[str] = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    env = os.environ.get("DA_ENV", "local").strip().lower()

    jwt_secret = REDACTED
    if not jwt_secret:
        if env != "REDACTED":
            raise RuntimeError(
                "JWT_SECRET must be set outside local development; refusing to "
                "sign sessions with a well-known value."
            )
        jwt_secret = REDACTED

    # LDAP everywhere by default, including locally: any AbbVie account can sign
    # in and there is no allow-list to maintain (D12). The dev provider exists for
    # automated tests and must be requested explicitly — spec section 9 requires
    # that it is never the default.
    auth_provider = os.environ.get("AUTH_PROVIDER", "ldap")
    dev_auth_users = _split(os.environ.get("DEV_AUTH_USERS"))
    if auth_provider == "dev" and not dev_auth_users:
        # Ownership and fork-on-edit cannot be exercised with a single identity,
        # so the dev provider seeds three people rather than one.
        dev_auth_users = ["asha.rao", "ben.carter", "mei.lin"]

    ca_bundle = os.environ.get("CA_BUNDLE_PATH", str(PROJECT_ROOT / "certs" / "ca-bundle.pem"))
    ca_bundle_path = Path(ca_bundle) if ca_bundle else None

    return Settings(
        env=env,
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'dev.db'}"),
        storage_dir=Path(os.environ.get("STORAGE_DIR", str(PROJECT_ROOT / "dev_storage"))),
        storage_backend=os.environ.get("STORAGE_BACKEND", "local").strip().lower(),
        # Configurable so tests can point at a fixture silo rather than shipping a
        # fake silo in the repository.
        silos_dir=Path(os.environ.get("SILOS_DIR", str(BACKEND_ROOT / "silos"))),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "200")),
        worker_heartbeat_s=int(os.environ.get("WORKER_HEARTBEAT_S", "30")),
        # Must exceed the longest stage that reports no progress, or the reaper will
        # re-queue work that is still running.
        stale_claim_timeout_s=int(os.environ.get("STALE_CLAIM_TIMEOUT_S", "900")),
        jwt_secret=REDACTED
        jwt_ttl_hours=int(os.environ.get("JWT_TTL_HOURS", "8")),
        cookie_secure=_bool(os.environ.get("COOKIE_SECURE"), env != "local"),
        auth_provider=auth_provider,
        dev_auth_users=dev_auth_users,
        ldap_url=os.environ.get("LDAP_URL", "ldaps://ldap-ad.abbvienet.com:636"),
        ldap_domain=os.environ.get("LDAP_DOMAIN", "abbvienet.com"),
        ca_bundle_path=ca_bundle_path if ca_bundle_path.exists() else None,
        # The corporate relay directly, NOT the local Postfix. Postfix is installed here
        # and has relayhost set, but it will not relay for us -- it answers submissions for
        # non-local recipients with its own "554 Recipient address rejected: Access
        # denied", which reads exactly like a rejection from the relay and is not one.
        # PDS&T Awards talks to the relay directly and works, so this matches a route
        # already proven on this network.
        smtp_host=os.environ.get("SMTP_HOST", "smtp.abbvienet.com"),
        smtp_port=int(os.environ.get("SMTP_PORT", "25")),
        # No auth, no STARTTLS: the same envelope PDS&T Awards sends with.
        mail_from=os.environ.get("MAIL_FROM", "ARIA <no-reply@abbvie.com>"),
        # Used to put a working link in the notification.
        app_base_url=os.environ.get("APP_BASE_URL", "http://10.224.134.56:8080").rstrip("/"),
        enabled_silos=_split(os.environ.get("ENABLED_SILOS")),
    )


settings = load_settings()
