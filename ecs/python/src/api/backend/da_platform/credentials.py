"""The single place credentials are read.

Silos must never read `os.environ` or construct their own clients — they receive
capabilities through the stage context (`ctx.llm`, `ctx.storage`). A contract test
enforces this, because the whole point of sharing is defeated the first time one
silo quietly reads its own key.

Values are re-read on every call rather than snapshotted at import. The app being
replaced cached an AD token once and then failed silently after it expired; re-reading
means a rotated secret or a refreshed token mount takes effect without a restart.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from api.backend.da_platform.settings import settings

# Defaults observed in the apps being migrated. Individual calls may override the
# model, because ISO and BOP deliberately use different ones.
DEFAULT_ILIAD_BASE_URL = "https://iliad-emerging-api.abbvienet.com"
DEFAULT_TEXT_MODEL = "claude-4.5-sonnet"
DEFAULT_VISION_MODEL = "gpt-5.2-global"


class MissingCredential(RuntimeError):
    """Raised when a required secret is absent.

    Deliberately loud: a missing key must fail the run rather than produce a
    confusing 401 from the gateway.
    """


@dataclass(frozen=True)
class IliadCredentials:
    base_url: str
    api_key: str
    user_token: str | None
    text_model: str
    vision_model: str
    ca_bundle_path: Path | None

    def headers(self) -> dict[str, str]:
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        # ISO sends an Active Directory token in addition to the API key; BOP does
        # not. Sending it when present keeps one client valid for both.
        if self.user_token:
            headers["x-user-token"] = self.user_token
        return headers

    @property
    def verify(self) -> str | bool:
        """Value for requests' `verify=`. Never False.

        ISO already verifies against a local `AbbVieFullChain.pem`; BOP still passes
        `verify=False` with warnings silenced. The combined bundle used here is
        strictly better than ISO's, because it also carries the public roots needed
        when a request is redirected to a host the proxy does not intercept.
        """
        return str(self.ca_bundle_path) if self.ca_bundle_path else True


@dataclass(frozen=True)
class AwsCredentials:
    region: str
    s3_bucket: str | None
    s3_prefix: str
    access_key_id: str | None
    secret_access_key: str | None
    session_token: str | None
    ca_bundle_path: Path | None

    @property
    def has_static_keys(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)

    @property
    def verify(self) -> str | bool:
        return str(self.ca_bundle_path) if self.ca_bundle_path else True

    def client_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "region_name": self.region,
            "verify": self.verify,
        }
        if self.has_static_keys:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
            # Required whenever the key is a temporary STS credential (they begin
            # with "ASIA"); omitting it produces an opaque InvalidAccessKeyId.
            if self.session_token:
                kwargs["aws_session_token"] = self.session_token
        return kwargs


DEFAULT_CMCDW_SCHEMA = "DEVSCI_DM"
DEFAULT_CMCDW_RESULTS_OBJECT = "mv_combined_results"


@dataclass(frozen=True)
class WarehouseCredentials:
    """Read-only access to the CMC data warehouse (spec phase 5).

    `schema` and `results_object` name database objects rather than being secrets: the
    app being replaced reads them from `CMCDW_SCHEMA` / `CMCDW_RESULTS_OBJECT` so a DEV
    materialised view can be swapped for the PROD view without a code change. They are
    carried here because a silo may not read the environment itself, and the silo builds
    its SQL from them.
    """

    user: str
    password: str
    dsn: str
    schema: str
    results_object: str

    @property
    def results_view(self) -> str:
        """The fully-qualified results object, e.g. `DEVSCI_DM.mv_combined_results`."""
        return f"{self.schema}.{self.results_object}"

    def __repr__(self) -> str:
        # Never let a connection error print the password: these objects end up in
        # tracebacks, and a DWH credential in a log is a reportable incident.
        return (
            f"WarehouseCredentials(user={self.user!r}, dsn={self.dsn!r}, "
            f"results_view={self.results_view!r}, password=<redacted>)"
        )


def warehouse_credentials() -> WarehouseCredentials:
    """Resolve the warehouse connection from the environment.

    Raises `MissingCredential` naming the absent variable, which is what the app being
    replaced did — the difference between a five-second fix and a confusing 500.
    """
    values: dict[str, str] = {}
    for name in ("CMCDW_USER", "CMCDW_PASSWORD", "CMCDW_DSN"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise MissingCredential(
                f"{name} is not set. The CMC warehouse needs CMCDW_USER, "
                "CMCDW_PASSWORD and CMCDW_DSN."
            )
        values[name] = value

    return WarehouseCredentials(
        user=values["CMCDW_USER"],
        password=REDACTED
        dsn=values["CMCDW_DSN"],
        schema=os.environ.get("CMCDW_SCHEMA", "").strip() or DEFAULT_CMCDW_SCHEMA,
        results_object=(
            os.environ.get("CMCDW_RESULTS_OBJECT", "").strip()
            or DEFAULT_CMCDW_RESULTS_OBJECT
        ),
    )


TokenRefresher = REDACTED

_token_refresher: TokenRefresher | None = None


def set_iliad_token_refresher(refresher: TokenRefresher | None) -> None:
    """Register a source for a fresh Active Directory token.

    The seam spec section 9 asks for. Until a real token endpoint is available the
    client falls back to re-reading the environment, which covers a mounted secret
    being rotated underneath a running process.
    """
    global _token_refresher
    _token_refresher = REDACTED


def refresh_iliad_token() -> str | None:
    if _token_refresher is None:
        return os.environ.get("ILIAD_USER_TOKEN") or None
    token = REDACTED
    if token:
        os.environ["ILIAD_USER_TOKEN"] = token
    return token


def _ca_bundle() -> Path | None:
    return settings.ca_bundle_path


def iliad_credentials(*, require_key: bool = True) -> IliadCredentials:
    api_key = REDACTED
    if require_key and not api_key:
        raise MissingCredential(
            "ILIAD_API_KEY is not set. It must come from the secret store, never "
            "from source."
        )
    return IliadCredentials(
        base_url=os.environ.get("ILIAD_BASE_URL", DEFAULT_ILIAD_BASE_URL).rstrip("/"),
        api_key=REDACTED
        user_token=REDACTED
        text_model=os.environ.get("LLM_TEXT_MODEL", DEFAULT_TEXT_MODEL),
        vision_model=os.environ.get("LLM_VISION_MODEL", DEFAULT_VISION_MODEL),
        ca_bundle_path=_ca_bundle(),
    )


def aws_credentials() -> AwsCredentials:
    return AwsCredentials(
        region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
        s3_bucket=os.environ.get("S3_BUCKET") or None,
        s3_prefix=os.environ.get("S3_PREFIX", "").strip("/"),
        access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or None,
        secret_access_key=REDACTED
        session_token=REDACTED
        ca_bundle_path=_ca_bundle(),
    )


def llm_max_concurrency() -> int:
    return int(os.environ.get("LLM_MAX_CONCURRENCY", "8"))


def textract_max_concurrency() -> int:
    return int(os.environ.get("TEXTRACT_MAX_CONCURRENCY", "4"))
