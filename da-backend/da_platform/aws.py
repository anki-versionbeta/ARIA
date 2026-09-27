"""Shared AWS clients (S3 and Textract).

The latest ISO relies on the default boto3 credential chain and hardcodes the
bucket, prefix and region in source. Both move here: credentials come from
`credentials.py` and the location comes from `S3_BUCKET` / `S3_PREFIX` /
`AWS_REGION`, so a silo never names a bucket or a region itself.

Static keys remain supported because local development may only have temporary STS
credentials, but an instance or workload identity is the intended path in every
deployed environment (spec section 11.1, item 4).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from da_platform.credentials import aws_credentials, textract_max_concurrency
from da_platform.llm.limiter import ProviderLimiter

logger = logging.getLogger(__name__)

# botocore reports an expired STS token under several codes depending on service.
EXPIRED_CREDENTIAL_CODES = frozenset(
    {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId"}
)

# Let botocore absorb throttling and transient 5xx before our own retry sees them.
CLIENT_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5})

# Re-resolving credentials is not instant: an in-flight refresh, or an IMDS/SSO round
# trip, needs a moment. One immediate retry was not enough — it re-sent the same dead
# token, which is exactly how ExpiredToken reached users.
EXPIRY_RETRY_DELAYS_S = (0.0, 1.0, 4.0)

T = TypeVar("T")


class AwsClients:
    """Caches clients on a session, and re-resolves credentials when they expire.

    ISO caches boto3 singletons that are never refreshed, so a run that outlives its
    STS credentials fails with an opaque error. Expiry is handled in one place here
    instead of in each silo.

    Clients are built from a session this class owns rather than from `boto3.client`,
    which resolves through the process-global default session. That distinction is the
    whole point: dropping a client built on the global session and rebuilding it hands
    back the *same* already-resolved credential object, so the rebuild re-sends the
    token that just expired. Owning the session means discarding it forces a real
    re-resolve from the instance role, SSO cache or profile on the next call.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._session: boto3.Session | None = None
        self._lock = threading.Lock()
        self._textract_limiter = ProviderLimiter(
            "textract", textract_max_concurrency()
        )

    def _describe_credentials(self, session: boto3.Session) -> str:
        """Provider and expiry for the log line. Never raises: diagnostics only."""
        try:
            resolved = session.get_credentials()
            if resolved is None:
                return "provider=none expiry=unknown"
            expiry = getattr(resolved, "_expiry_time", None)
            return (
                f"provider={getattr(resolved, 'method', 'unknown')} "
                f"refreshable={type(resolved).__name__ != 'Credentials'} "
                f"expiry={expiry.isoformat() if expiry else 'none'}"
            )
        except Exception:  # pragma: no cover - never break a request to log
            return "provider=unavailable expiry=unknown"

    def client(self, service: str) -> Any:
        with self._lock:
            existing = self._cache.get(service)
            if existing is not None:
                return existing
            credentials = aws_credentials()
            if self._session is None:
                self._session = boto3.Session()
            created = self._session.client(
                service, config=CLIENT_CONFIG, **credentials.client_kwargs()
            )
            self._cache[service] = created
            logger.info(
                "Built AWS %s client (region=%s, static_keys=%s, %s)",
                service,
                credentials.region,
                credentials.has_static_keys,
                self._describe_credentials(self._session),
            )
            return created

    def invalidate(self, service: str | None = None) -> None:
        with self._lock:
            if service is None:
                self._cache.clear()
            else:
                self._cache.pop(service, None)

    def _reset_credentials(self) -> None:
        """Drop the session and every client on it, so credentials are re-resolved.

        Clearing the whole cache rather than one service is deliberate: the credentials
        are shared, so if S3's have expired Textract's have too.
        """
        with self._lock:
            self._session = None
            self._cache.clear()

    def call(self, service: str, operation: str, /, **kwargs: Any) -> Any:
        """Invoke an operation, re-resolving credentials if they have expired."""

        def invoke() -> Any:
            return getattr(self.client(service), operation)(**kwargs)

        if service == "textract":
            # Textract is charged per page and rate limited, so it gets its own
            # ceiling separate from the LLM's.
            with self._textract_limiter.slot():
                return self._with_expiry_retry(service, invoke)
        return self._with_expiry_retry(service, invoke)

    def _with_expiry_retry(self, service: str, invoke: Callable[[], T]) -> T:
        last: ClientError | None = None
        for attempt, delay in enumerate(EXPIRY_RETRY_DELAYS_S, start=1):
            if delay:
                time.sleep(delay)
            try:
                return invoke()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in EXPIRED_CREDENTIAL_CODES:
                    raise
                last = exc
                logger.warning(
                    "AWS %s credentials expired (%s) on attempt %d/%d; re-resolving "
                    "credentials and retrying. %s",
                    service,
                    code,
                    attempt,
                    len(EXPIRY_RETRY_DELAYS_S),
                    self._describe_credentials(self._session)
                    if self._session is not None
                    else "provider=unbuilt",
                )
                self._reset_credentials()

        # Every attempt re-resolved and still got an expired token, so the credentials
        # are not refreshable — static STS keys in the environment or a stale SSO cache,
        # which no retry here can renew. Say so, because the botocore error alone sends
        # people looking at the bucket policy instead of at their own credentials.
        assert last is not None
        logger.error(
            "AWS %s credentials are expired and could not be renewed after %d attempts "
            "(%s). Static AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN cannot be refreshed: on a "
            "workstation re-authenticate (AWS SSO/profile), and on an EC2 host prefer the "
            "instance role so botocore renews them itself.",
            service,
            len(EXPIRY_RETRY_DELAYS_S),
            aws_credentials().has_static_keys
            and "static keys are set in the environment"
            or "no static keys; check the instance role or SSO cache",
        )
        raise last

    # ── convenience ───────────────────────────────────────────────────────────

    def s3(self) -> Any:
        return self.client("s3")

    def textract(self) -> Any:
        return self.client("textract")

    def analyze_document(self, png_bytes: bytes, feature_types: list[str] | None = None) -> Any:
        """Textract LAYOUT + TABLES analysis of a single rendered page."""
        return self.call(
            "textract",
            "analyze_document",
            Document={"Bytes": png_bytes},
            FeatureTypes=feature_types or ["LAYOUT", "TABLES"],
        )


_clients: AwsClients | None = None


def get_clients() -> AwsClients:
    global _clients
    if _clients is None:
        _clients = AwsClients()
    return _clients
