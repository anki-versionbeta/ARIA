"""The shared AWS client layer, and specifically its behaviour on expired credentials.

This path had no tests, which is why it took a failed run in production to notice that
the retry fired instantly and therefore almost never helped. An instance role's
credentials rotate roughly hourly; a call made while botocore fetches the replacement
gets ExpiredToken and the next one succeeds. Retrying in the same millisecond re-reads
the credentials that were just rejected.

No network and no boto3 client: the operation is a stub, so what is under test is the
retry policy itself.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from da_platform import aws


def expired(code: str = "ExpiredToken") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "The provided token has expired."}}, "GetObject")


def other_error() -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")


@pytest.fixture
def clients(monkeypatch):
    """An AwsClients whose clients are stubs and whose sleeps are recorded, not taken."""
    slept: list[float] = []
    monkeypatch.setattr(aws.time, "sleep", slept.append)

    instance = aws.AwsClients()
    built: list[int] = []

    def fake_client(service: str):
        built.append(1)
        return object()

    monkeypatch.setattr(instance, "client", fake_client)
    instance.slept = slept  # type: ignore[attr-defined]
    instance.built = built  # type: ignore[attr-defined]
    return instance


def test_a_call_that_succeeds_is_not_retried(clients):
    calls = []

    def invoke():
        calls.append(1)
        return "ok"

    assert clients._with_expiry_retry("s3", invoke) == "ok"
    assert len(calls) == 1
    assert clients.slept == []


def test_an_expired_credential_is_retried_after_a_wait(clients):
    """The wait is the fix. Without it the retry lands in the same rotation window."""
    calls = []

    def invoke():
        calls.append(1)
        if len(calls) == 1:
            raise expired()
        return "ok"

    assert clients._with_expiry_retry("s3", invoke) == "ok"
    assert len(calls) == 2
    assert clients.slept == [0.5], "must pause before retrying, not retry instantly"


def test_it_keeps_trying_across_the_whole_backoff(clients):
    """Two windows in a row still recover — three attempts, waits of 0.5s then 2.0s."""
    calls = []

    def invoke():
        calls.append(1)
        if len(calls) < 3:
            raise expired()
        return "ok"

    assert clients._with_expiry_retry("s3", invoke) == "ok"
    assert len(calls) == 3
    assert clients.slept == [0.5, 2.0]


def test_persistent_expiry_raises_rather_than_retrying_forever(clients):
    """A stuck run is harder to diagnose than a failed one, so it gives up and says so."""
    calls = []

    def invoke():
        calls.append(1)
        raise expired()

    with pytest.raises(ClientError) as caught:
        clients._with_expiry_retry("s3", invoke)

    assert caught.value.response["Error"]["Code"] == "ExpiredToken"
    assert len(calls) == len(aws.EXPIRY_RETRY_BACKOFF) + 1 == 3
    assert clients.slept == [0.5, 2.0], "no wait after the final attempt"


def test_the_client_is_rebuilt_between_attempts(clients):
    """Dropping the cached client is what forces a fresh credential resolution; without
    it the retry would reuse the client that already failed."""
    calls = []
    invalidated: list[str | None] = []
    original = clients.invalidate
    clients.invalidate = lambda service=None: (invalidated.append(service), original(service))[1]  # type: ignore[method-assign]

    def invoke():
        calls.append(1)
        if len(calls) == 1:
            raise expired()
        return "ok"

    clients._with_expiry_retry("s3", invoke)

    assert invalidated == ["s3"]


def test_a_non_credential_error_is_raised_immediately(clients):
    """A missing key must not be retried three times over 2.5 seconds."""
    calls = []

    def invoke():
        calls.append(1)
        raise other_error()

    with pytest.raises(ClientError) as caught:
        clients._with_expiry_retry("s3", invoke)

    assert caught.value.response["Error"]["Code"] == "NoSuchKey"
    assert len(calls) == 1
    assert clients.slept == []


@pytest.mark.parametrize("code", sorted(aws.EXPIRED_CREDENTIAL_CODES))
def test_every_expiry_code_botocore_uses_is_retried(clients, code):
    """The code differs by service, which is why the set exists rather than one string."""
    calls = []

    def invoke():
        calls.append(1)
        if len(calls) == 1:
            raise expired(code)
        return "ok"

    assert clients._with_expiry_retry("s3", invoke) == "ok"
    assert len(calls) == 2


def test_the_backoff_is_short_enough_not_to_stall_a_stage():
    """The worker's stale-claim reaper is minutes away; this must stay far inside it."""
    assert sum(aws.EXPIRY_RETRY_BACKOFF) < 5.0
