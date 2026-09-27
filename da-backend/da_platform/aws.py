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
from botocore.exceptions import ClientError

from da_platform.credentials import aws_credentials, textract_max_concurrency
from da_platform.llm.limiter import ProviderLimiter

logger = logging.getLogger(__name__)

# botocore reports an expired STS token under several codes depending on service.
EXPIRED_CREDENTIAL_CODES = frozenset(
    {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId"}
)

# Waits between attempts on an expired credential, so three attempts in all.
#
# The delays are the whole point. An instance role's credentials rotate roughly hourly,
# and a call made while botocore is fetching the replacement gets ExpiredToken; a moment
# later it succeeds. Retrying instantly — which is what this used to do — re-reads the
# credentials that were just rejected, so the one mechanism meant to absorb a transient
# expiry reliably missed it. ~2.5s total is far inside any stage timeout.
EXPIRY_RETRY_BACKOFF = (0.5, 2.0)

T = TypeVar("T")


class AwsClients:
    """Caches clients and rebuilds them when their credentials expire.

    ISO caches boto3 singletons that are never refreshed, so a run that outlives its
    STS credentials fails with an opaque error. Expiry is handled in one place here
    instead of in each silo.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._textract_limiter = ProviderLimiter(
            "textract", textract_max_concurrency()
        )

    def client(self, service: str) -> Any:
        with self._lock:
            existing = self._cache.get(service)
            if existing is not None:
                return existing
            credentials = aws_credentials()
            created = boto3.client(service, **credentials.client_kwargs())
            self._cache[service] = created
            logger.info(
                "Built AWS %s client (region=%s, static_keys=%s)",
                service,
                credentials.region,
                credentials.has_static_keys,
            )
            return created

    def invalidate(self, service: str | None = None) -> None:
        with self._lock:
            if service is None:
                self._cache.clear()
            else:
                self._cache.pop(service, None)

    def call(self, service: str, operation: str, /, **kwargs: Any) -> Any:
        """Invoke an operation, rebuilding the client once if credentials expired."""

        def invoke() -> Any:
            return getattr(self.client(service), operation)(**kwargs)

        if service == "textract":
            # Textract is charged per page and rate limited, so it gets its own
            # ceiling separate from the LLM's.
            with self._textract_limiter.slot():
                return self._with_expiry_retry(service, invoke)
        return self._with_expiry_retry(service, invoke)

    def _with_expiry_retry(self, service: str, invoke: Callable[[], T]) -> T:
        """Invoke, rebuilding the client and waiting when the credentials have expired.

        Logged at warning rather than info: a silent retry cannot be told apart from a
        clean call, and this is the failure that stopped a run in `finalize`.
        """
        attempts = len(EXPIRY_RETRY_BACKOFF) + 1
        for attempt in range(1, attempts + 1):
            try:
                return invoke()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in EXPIRED_CREDENTIAL_CODES:
                    raise
                if attempt == attempts:
                    # Not a rotation window any more. Raised rather than retried
                    # forever, because a stuck run is harder to diagnose than a failed
                    # one — and on an instance role, persistent expiry means the clock
                    # is skewed or the metadata service is unreachable.
                    logger.error(
                        "AWS %s credentials still expired (%s) after %d attempts; "
                        "check instance clock sync and metadata service reachability",
                        service,
                        code,
                        attempts,
                    )
                    raise
                delay = EXPIRY_RETRY_BACKOFF[attempt - 1]
                logger.warning(
                    "AWS %s credentials expired (%s) on attempt %d of %d; rebuilding "
                    "the client and retrying in %.1fs",
                    service,
                    code,
                    attempt,
                    attempts,
                    delay,
                )
                # Dropping the cached client forces a fresh credential resolution on the
                # next call, which is what picks up the rotated role credentials.
                self.invalidate(service)
                time.sleep(delay)

        raise AssertionError("unreachable: the loop either returns or raises")

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
