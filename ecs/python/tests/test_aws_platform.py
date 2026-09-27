"""The shared AWS layer: client caching, credential-expiry retry, and notifications.

Covers `da_platform.aws`, `da_platform.credentials` and `da_platform.notify`.

Hand-rolled boto3 fakes, no `moto` and no `responses`: the corporate proxy denies
pypi (see tests/requirements.txt and tests/iso_fakes.py), so nothing can be
installed. `boto3` itself is present, so `botocore.exceptions.ClientError` is real
and an expired STS token is simulated the way botocore actually reports one.

Nothing here may touch the network. The seam is always the *module's own* reference
(`aws.boto3`, `aws.time`, `notify.smtplib`), never boto3 or smtplib internals: patch
the reference and there is no code path left that can open a socket.

Why these three modules get their own file: Textract is billed per page analysis and
ISO calls it once per page of a garbled scan, so "how many calls did that make" is a
money question, not a style question. The expiry retry, the concurrency ceiling and
the lazy client build are all counted explicitly below.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from api.backend.da_platform import aws, credentials, notify
from api.backend.da_platform.db.models import User

# ── boto3 stand-ins ───────────────────────────────────────────────────────────


def expired(code: str = "ExpiredToken") -> ClientError:
    """The error botocore raises when an STS token has run out."""
    return ClientError(
        {"Error": {"Code": code, "Message": "The security token has expired"}},
        "AnalyzeDocument",
    )


def denied() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetObject"
    )


class Recorder:
    """Shared call log for every fake client, across session rebuilds.

    Shared on purpose: the expiry path throws the session away and builds a new
    client, so a per-client log would lose the first attempt and undercount the
    calls that a real account would be billed for.
    """

    def __init__(self, script: dict | None = None, default: object = None) -> None:
        self.script = {name: list(items) for name, items in (script or {}).items()}
        self.default = default if default is not None else {"ok": True}
        self.calls: list[tuple[str, str, dict]] = []
        self.clients: list[FakeClient] = []
        self.sessions: list[FakeSession] = []
        self.sleeps: list[float] = []

    def invoke(self, service: str, operation: str, kwargs: dict) -> object:
        self.calls.append((service, operation, kwargs))
        queue = self.script.get(operation)
        if queue is None:
            return self.default
        if not queue:
            # Running past the script is how an unbounded retry loop announces
            # itself; a silent extra success would hide it.
            raise RuntimeError(f"{operation} called more times than the test scripted")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def operations(self, operation: str) -> list[dict]:
        return [kw for _svc, op, kw in self.calls if op == operation]


class FakeClient:
    def __init__(self, service: str, config: object, kwargs: dict, recorder: Recorder):
        self.service = service
        self.config = config
        self.kwargs = kwargs
        self._recorder = recorder

    def __getattr__(self, operation: str):
        if operation.startswith("_"):
            raise AttributeError(operation)

        def call(**kwargs):
            return self._recorder.invoke(self.service, operation, kwargs)

        return call


class FakeSession:
    def __init__(self, recorder: Recorder, resolved: object) -> None:
        self._recorder = recorder
        self._resolved = resolved

    def client(self, service: str, config: object = None, **kwargs):
        client = FakeClient(service, config, kwargs, self._recorder)
        self._recorder.clients.append(client)
        return client

    def get_credentials(self):
        if isinstance(self._resolved, BaseException):
            raise self._resolved
        return self._resolved


class FakeCredentialsObject:
    """Enough of botocore's RefreshableCredentials for the diagnostic log line."""

    def __init__(self, method: str = "iam-role", expiry=None) -> None:
        self.method = method
        self._expiry_time = expiry


def install(monkeypatch, *, script=None, default=None, resolved=None) -> Recorder:
    """Replace the aws module's boto3 and time with fakes; return the call log."""
    recorder = Recorder(script, default)

    def session_factory():
        session = FakeSession(recorder, resolved)
        recorder.sessions.append(session)
        return session

    monkeypatch.setattr(aws, "boto3", SimpleNamespace(Session=session_factory))
    # Real sleeps would make the expiry test take five seconds; recording them
    # also proves the backoff is applied rather than hammered.
    monkeypatch.setattr(
        aws, "time", SimpleNamespace(sleep=lambda s: recorder.sleeps.append(s))
    )
    return recorder


@pytest.fixture
def clients(monkeypatch):
    """A fresh AwsClients with a known concurrency ceiling and no static keys."""
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("TEXTRACT_MAX_CONCURRENCY", "4")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    return aws.AwsClients()


# ── building clients ──────────────────────────────────────────────────────────


def test_constructing_the_client_holder_resolves_no_credentials_and_builds_no_session(
    monkeypatch,
):
    """Every run builds a StageContext, and BOP never touches AWS, so construction
    must not need credentials — `ctx.textract` resolves on first use instead."""
    recorder = install(monkeypatch)

    def explode():  # noqa: ANN202 - a tripwire, never called
        raise AssertionError("credentials must not be resolved at construction")

    monkeypatch.setattr(aws, "aws_credentials", explode)

    holder = aws.AwsClients()

    assert recorder.sessions == []
    assert holder._cache == {}


def test_the_same_service_hands_back_one_cached_client_rather_than_rebuilding(
    monkeypatch, clients
):
    recorder = install(monkeypatch)

    first = clients.s3()
    second = clients.client("s3")

    assert first is second
    assert len(recorder.clients) == 1
    # One session serves every service; a second service reuses it.
    clients.textract()
    assert len(recorder.sessions) == 1
    assert len(recorder.clients) == 2


def test_static_keys_and_the_session_token_reach_the_client_when_they_are_set(
    monkeypatch, clients
):
    """Temporary STS keys begin with ASIA and are useless without the token; omitting
    it produces an opaque InvalidAccessKeyId, so it must be passed through."""
    recorder = install(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "temporary")

    clients.s3()

    kwargs = recorder.clients[0].kwargs
    assert kwargs["aws_access_key_id"] == "ASIAEXAMPLE"
    assert kwargs["aws_secret_access_key"] == "shhh"
    assert kwargs["aws_session_token"] == "temporary"
    assert kwargs["region_name"] == "us-west-2"


def test_no_static_keys_means_no_key_arguments_so_the_default_chain_is_used(
    monkeypatch, clients
):
    """An instance or workload identity is the intended path in every deployed
    environment; passing empty keys would defeat the default chain."""
    recorder = install(monkeypatch)

    clients.s3()

    kwargs = recorder.clients[0].kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_session_token" not in kwargs


def test_every_client_is_built_with_the_shared_retry_configuration(monkeypatch, clients):
    """botocore absorbs throttling and transient 5xx before our own retry sees them."""
    recorder = install(monkeypatch)

    clients.s3()

    assert recorder.clients[0].config is aws.CLIENT_CONFIG
    assert aws.CLIENT_CONFIG.retries["max_attempts"] == 5


def test_invalidating_one_service_leaves_the_others_cached(monkeypatch, clients):
    install(monkeypatch)
    s3_client = clients.s3()
    textract_client = clients.textract()

    clients.invalidate("s3")

    assert clients.s3() is not s3_client
    assert clients.textract() is textract_client


def test_invalidating_everything_drops_every_cached_client(monkeypatch, clients):
    install(monkeypatch)
    s3_client = clients.s3()
    textract_client = clients.textract()

    clients.invalidate()

    assert clients.s3() is not s3_client
    assert clients.textract() is not textract_client


def test_the_process_wide_holder_is_a_singleton(monkeypatch):
    monkeypatch.setattr(aws, "_clients", None)

    assert aws.get_clients() is aws.get_clients()


# ── calling operations ────────────────────────────────────────────────────────


def test_call_forwards_the_operation_and_its_arguments_and_returns_the_response(
    monkeypatch, clients
):
    recorder = install(monkeypatch, script={"get_object": [{"Body": b"x"}]})

    result = clients.call("s3", "get_object", Bucket="b", Key="k")

    assert result == {"Body": b"x"}
    assert recorder.calls == [("s3", "get_object", {"Bucket": "b", "Key": "k"})]


def test_one_page_analysis_is_exactly_one_textract_call_with_layout_and_tables(
    monkeypatch, clients
):
    """Textract is billed per page analysis, so the default feature set and the call
    count are both part of the contract."""
    recorder = install(monkeypatch, script={"analyze_document": [{"Blocks": []}]})

    clients.analyze_document(b"png-bytes")

    assert recorder.operations("analyze_document") == [
        {"Document": {"Bytes": b"png-bytes"}, "FeatureTypes": ["LAYOUT", "TABLES"]}
    ]


def test_explicit_feature_types_replace_the_default_pair(monkeypatch, clients):
    recorder = install(monkeypatch, script={"analyze_document": [{"Blocks": []}]})

    clients.analyze_document(b"png", feature_types=["FORMS"])

    assert recorder.operations("analyze_document")[0]["FeatureTypes"] == ["FORMS"]


def test_a_non_credential_error_is_raised_immediately_without_a_retry(
    monkeypatch, clients
):
    """AccessDenied is not going to fix itself, and a retry would double the bill."""
    recorder = install(monkeypatch, script={"get_object": [denied()]})

    with pytest.raises(ClientError) as caught:
        clients.call("s3", "get_object", Bucket="b", Key="k")

    assert caught.value.response["Error"]["Code"] == "AccessDenied"
    assert len(recorder.calls) == 1
    assert recorder.sleeps == []


def test_a_connection_failure_is_not_treated_as_an_expired_credential(
    monkeypatch, clients
):
    """Only ClientError carries an error code; anything else must propagate as-is."""
    boom = EndpointConnectionError(endpoint_url="https://s3.example")
    recorder = install(monkeypatch, script={"get_object": [boom]})

    with pytest.raises(EndpointConnectionError):
        clients.call("s3", "get_object", Bucket="b")

    assert len(recorder.calls) == 1


# ── credential expiry ─────────────────────────────────────────────────────────


def test_an_expired_token_triggers_one_credential_refresh_and_one_retry(
    monkeypatch, clients
):
    """The failure this exists for: a run that outlives its STS credentials. One
    expiry must cost exactly one rebuild and one extra call, not a loop."""
    recorder = install(
        monkeypatch, script={"analyze_document": [expired(), {"Blocks": []}]}
    )

    result = clients.analyze_document(b"png")

    assert result == {"Blocks": []}
    assert len(recorder.operations("analyze_document")) == 2
    # A brand-new session, because rebuilding a client on the process-global
    # session hands back the same already-resolved (dead) credential object.
    assert len(recorder.sessions) == 2
    assert recorder.sleeps == [1.0]


def test_the_retry_discards_every_cached_client_not_just_the_failing_one(
    monkeypatch, clients
):
    """The credentials are shared: if S3's have expired, Textract's have too."""
    recorder = install(monkeypatch, script={"get_object": [expired(), {"ok": True}]})
    stale_textract = clients.textract()

    clients.call("s3", "get_object", Bucket="b")

    assert clients.textract() is not stale_textract
    assert len(recorder.sessions) == 2


@pytest.mark.parametrize("code", sorted(aws.EXPIRED_CREDENTIAL_CODES))
def test_every_expiry_code_botocore_uses_is_recognised_as_expiry(
    monkeypatch, clients, code
):
    """botocore reports the same condition under four names depending on service."""
    recorder = install(monkeypatch, script={"get_object": [expired(code), {"ok": True}]})

    clients.call("s3", "get_object", Bucket="b")

    assert len(recorder.calls) == 2


def test_a_permanently_expired_credential_stops_after_three_attempts_and_reraises(
    monkeypatch, clients
):
    """Static STS keys cannot be refreshed, so retrying forever would hang the run.
    The scripted queue holds exactly three errors: a fourth call raises RuntimeError
    instead, which is how an unbounded loop would fail this test."""
    recorder = install(
        monkeypatch, script={"analyze_document": [expired(), expired(), expired()]}
    )

    with pytest.raises(ClientError) as caught:
        clients.analyze_document(b"png")

    assert caught.value.response["Error"]["Code"] == "ExpiredToken"
    assert len(recorder.operations("analyze_document")) == len(aws.EXPIRY_RETRY_DELAYS_S)
    # First attempt is immediate; the delays escalate rather than repeat.
    assert recorder.sleeps == [1.0, 4.0]


def test_the_final_failure_says_static_keys_are_the_cause_when_they_are_set(
    monkeypatch, clients, caplog
):
    """The botocore error alone sends people to the bucket policy; naming the real
    cause is the whole reason this log line exists."""
    install(monkeypatch, script={"get_object": [expired(), expired(), expired()]})
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")

    with caplog.at_level(logging.ERROR, logger=aws.__name__):
        with pytest.raises(ClientError):
            clients.call("s3", "get_object", Bucket="b")

    assert any(
        "static keys are set in the environment" in r.getMessage()
        for r in caplog.records
    )


def test_the_final_failure_points_at_the_instance_role_when_no_static_keys_are_set(
    monkeypatch, clients, caplog
):
    install(monkeypatch, script={"get_object": [expired(), expired(), expired()]})

    with caplog.at_level(logging.ERROR, logger=aws.__name__):
        with pytest.raises(ClientError):
            clients.call("s3", "get_object", Bucket="b")

    assert any(
        "check the instance role or SSO cache" in r.getMessage() for r in caplog.records
    )


def test_an_expiry_from_a_cached_client_logs_that_no_session_was_built_yet(
    monkeypatch, clients, caplog
):
    """A client can be cached while `_session` is still unset (another thread reset
    it), and the diagnostic must not blow up on the None."""
    install(monkeypatch, script={"get_object": [expired(), {"ok": True}]})
    clients._cache["s3"] = FakeClient("s3", None, {}, clients_recorder(clients))

    with caplog.at_level(logging.WARNING, logger=aws.__name__):
        clients.call("s3", "get_object", Bucket="b")

    assert any("provider=unbuilt" in r.getMessage() for r in caplog.records)


def clients_recorder(holder) -> Recorder:
    """The recorder installed on `holder`'s fake boto3, for pre-seeding the cache."""
    session = holder._session or aws.boto3.Session()
    return session._recorder


# ── the diagnostic log line ───────────────────────────────────────────────────


def test_the_client_log_names_the_provider_and_the_expiry(monkeypatch, clients, caplog):
    import datetime

    expiry = datetime.datetime(2026, 8, 27, 12, 0, tzinfo=datetime.timezone.utc)
    install(monkeypatch, resolved=FakeCredentialsObject("iam-role", expiry))

    with caplog.at_level(logging.INFO, logger=aws.__name__):
        clients.s3()

    message = next(r.getMessage() for r in caplog.records if "Built AWS" in r.getMessage())
    assert "provider=iam-role" in message
    assert "refreshable=True" in message
    assert expiry.isoformat() in message


def test_unresolvable_credentials_are_reported_rather_than_crashing_the_build(
    monkeypatch, clients, caplog
):
    install(monkeypatch, resolved=None)

    with caplog.at_level(logging.INFO, logger=aws.__name__):
        clients.s3()

    assert any("provider=none expiry=unknown" in r.getMessage() for r in caplog.records)


def test_a_failure_inside_the_diagnostic_never_breaks_the_client_build(
    monkeypatch, clients, caplog
):
    """Diagnostics are not worth a failed request, so the describe helper swallows."""
    install(monkeypatch, resolved=RuntimeError("IMDS unreachable"))

    with caplog.at_level(logging.INFO, logger=aws.__name__):
        assert clients.s3() is not None

    assert any("provider=unavailable" in r.getMessage() for r in caplog.records)


# ── the concurrency ceiling ───────────────────────────────────────────────────


def test_the_textract_ceiling_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TEXTRACT_MAX_CONCURRENCY", "2")

    assert aws.AwsClients()._textract_limiter.max_concurrency == 2


def test_page_analyses_never_exceed_the_textract_ceiling(monkeypatch):
    """A 200-page scan is 200 billed analyses; the ceiling is what stops them all
    leaving at once and being throttled."""
    monkeypatch.setenv("TEXTRACT_MAX_CONCURRENCY", "2")
    holder = aws.AwsClients()
    install(monkeypatch)

    lock = threading.Lock()
    live = 0
    peak = 0
    released = threading.Event()

    def slow_analyze(**_kwargs):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        released.wait(2.0)
        with lock:
            live -= 1
        return {"Blocks": []}

    monkeypatch.setattr(
        holder, "client", lambda service: SimpleNamespace(analyze_document=slow_analyze)
    )

    threads = [
        threading.Thread(target=holder.analyze_document, args=(b"png",))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    # Let the admitted callers pile up before letting any of them finish.
    threading.Event().wait(0.2)
    released.set()
    for thread in threads:
        thread.join(5.0)

    assert peak <= 2


def test_s3_calls_are_not_charged_against_the_textract_ceiling(monkeypatch, clients):
    """S3 is neither billed per page nor rate limited the same way, so it must not
    queue behind Textract."""
    install(monkeypatch)
    entered: list[str] = []
    original = clients._textract_limiter.slot

    def watched():
        entered.append("textract")
        return original()

    monkeypatch.setattr(clients._textract_limiter, "slot", watched)

    clients.call("s3", "get_object", Bucket="b")
    assert entered == []

    clients.analyze_document(b"png")
    assert entered == ["textract"]


# ── credential resolution ─────────────────────────────────────────────────────


def test_the_aws_region_wins_over_the_default_region_and_falls_back_last(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    assert credentials.aws_credentials().region == "eu-west-1"

    monkeypatch.delenv("AWS_REGION")
    assert credentials.aws_credentials().region == "ap-south-1"

    monkeypatch.delenv("AWS_DEFAULT_REGION")
    assert credentials.aws_credentials().region == "us-east-1"


def test_a_blank_bucket_or_key_reads_as_absent_rather_than_as_an_empty_string(
    monkeypatch,
):
    """An empty variable in a .env file is a variable that was never filled in."""
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")

    resolved = credentials.aws_credentials()

    assert resolved.s3_bucket is None
    assert resolved.access_key_id is None
    assert resolved.has_static_keys is False


def test_the_s3_prefix_is_stored_without_surrounding_slashes(monkeypatch):
    monkeypatch.setenv("S3_PREFIX", "/aria/runs/")

    assert credentials.aws_credentials().s3_prefix == "aria/runs"


def test_a_key_without_its_secret_does_not_count_as_static_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    assert credentials.aws_credentials().has_static_keys is False


def test_client_arguments_carry_the_ca_bundle_when_one_is_configured(monkeypatch):
    """The corporate proxy re-signs TLS, so verification needs the bundle; `verify`
    is never False, unlike the app being replaced."""
    monkeypatch.setattr(
        credentials, "settings", SimpleNamespace(ca_bundle_path=Path("/certs/ca.pem"))
    )

    resolved = credentials.aws_credentials()

    assert resolved.verify == str(Path("/certs/ca.pem"))
    assert resolved.client_kwargs()["verify"] == str(Path("/certs/ca.pem"))


def test_client_arguments_fall_back_to_default_verification_without_a_bundle(
    monkeypatch,
):
    monkeypatch.setattr(credentials, "settings", SimpleNamespace(ca_bundle_path=None))

    assert credentials.aws_credentials().verify is True


def test_a_static_key_without_a_session_token_omits_the_token_argument(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

    kwargs = credentials.aws_credentials().client_kwargs()

    assert kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert "aws_session_token" not in kwargs


def test_the_concurrency_ceilings_have_defaults_and_are_overridable(monkeypatch):
    monkeypatch.delenv("LLM_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("TEXTRACT_MAX_CONCURRENCY", raising=False)
    assert credentials.llm_max_concurrency() == 8
    assert credentials.textract_max_concurrency() == 4

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("TEXTRACT_MAX_CONCURRENCY", "1")
    assert credentials.llm_max_concurrency() == 3
    assert credentials.textract_max_concurrency() == 1


# ── Iliad credentials ─────────────────────────────────────────────────────────


def test_a_missing_iliad_api_key_fails_loudly_and_names_the_variable(monkeypatch):
    """A missing key must fail the run rather than produce a confusing gateway 401."""
    monkeypatch.setenv("ILIAD_API_KEY", "   ")

    with pytest.raises(credentials.MissingCredential, match="ILIAD_API_KEY"):
        credentials.iliad_credentials()


def test_the_key_check_can_be_waived_for_callers_that_only_want_the_configuration(
    monkeypatch,
):
    monkeypatch.setenv("ILIAD_API_KEY", "")

    assert credentials.iliad_credentials(require_key=False).api_key == ""


def test_iliad_defaults_are_used_when_nothing_is_configured(monkeypatch):
    monkeypatch.setenv("ILIAD_API_KEY", "k")
    for name in ("ILIAD_BASE_URL", "LLM_TEXT_MODEL", "LLM_VISION_MODEL"):
        monkeypatch.delenv(name, raising=False)

    resolved = credentials.iliad_credentials()

    assert resolved.base_url == credentials.DEFAULT_ILIAD_BASE_URL
    assert resolved.text_model == credentials.DEFAULT_TEXT_MODEL
    assert resolved.vision_model == credentials.DEFAULT_VISION_MODEL


def test_a_trailing_slash_on_the_base_url_is_removed_so_paths_do_not_double_up(
    monkeypatch,
):
    monkeypatch.setenv("ILIAD_API_KEY", "k")
    monkeypatch.setenv("ILIAD_BASE_URL", "https://gateway.example/v1/")

    assert credentials.iliad_credentials().base_url == "https://gateway.example/v1"


def test_the_user_token_header_is_sent_only_when_a_token_is_present(monkeypatch):
    """ISO sends an AD token alongside the API key and BOP does not; one client has
    to serve both."""
    monkeypatch.setenv("ILIAD_API_KEY", "k")
    monkeypatch.setenv("ILIAD_USER_TOKEN", "ad-token")
    assert credentials.iliad_credentials().headers()["x-user-token"] == "ad-token"

    monkeypatch.setenv("ILIAD_USER_TOKEN", "  ")
    resolved = credentials.iliad_credentials()
    assert resolved.user_token is None
    assert "x-user-token" not in resolved.headers()
    assert resolved.headers()["x-api-key"] == "k"


def test_iliad_verification_uses_the_bundle_and_is_never_disabled(monkeypatch):
    monkeypatch.setenv("ILIAD_API_KEY", "k")
    monkeypatch.setattr(
        credentials, "settings", SimpleNamespace(ca_bundle_path=Path("/certs/ca.pem"))
    )
    assert credentials.iliad_credentials().verify == str(Path("/certs/ca.pem"))

    monkeypatch.setattr(credentials, "settings", SimpleNamespace(ca_bundle_path=None))
    assert credentials.iliad_credentials().verify is True


# ── the token refresher seam ──────────────────────────────────────────────────


def test_without_a_registered_refresher_the_token_is_re_read_from_the_environment(
    monkeypatch,
):
    """Re-reading rather than snapshotting is the point: the app being replaced
    cached an AD token once and then failed silently after it expired."""
    monkeypatch.setattr(credentials, "_token_refresher", None)
    monkeypatch.setenv("ILIAD_USER_TOKEN", "from-env")
    assert credentials.refresh_iliad_token() == "from-env"

    monkeypatch.setenv("ILIAD_USER_TOKEN", "")
    assert credentials.refresh_iliad_token() is None


def test_a_registered_refresher_supplies_the_token_and_publishes_it_to_the_environment(
    monkeypatch,
):
    monkeypatch.setattr(credentials, "_token_refresher", None)
    monkeypatch.setenv("ILIAD_USER_TOKEN", "stale")
    credentials.set_iliad_token_refresher(lambda: "fresh")
    try:
        assert credentials.refresh_iliad_token() == "fresh"
        # Published so anything still reading the variable sees the new value.
        assert credentials.iliad_credentials(require_key=False).user_token == "fresh"
    finally:
        credentials.set_iliad_token_refresher(None)


def test_a_refresher_that_returns_nothing_leaves_the_existing_token_untouched(
    monkeypatch,
):
    monkeypatch.setattr(credentials, "_token_refresher", None)
    monkeypatch.setenv("ILIAD_USER_TOKEN", "keep-me")
    credentials.set_iliad_token_refresher(lambda: None)
    try:
        assert credentials.refresh_iliad_token() is None
    finally:
        credentials.set_iliad_token_refresher(None)

    import os

    assert os.environ["ILIAD_USER_TOKEN"] == "keep-me"


# ── warehouse credentials ─────────────────────────────────────────────────────


@pytest.fixture
def warehouse_env(monkeypatch):
    monkeypatch.setenv("CMCDW_USER", "reader")
    monkeypatch.setenv("CMCDW_PASSWORD", "s3cret")
    monkeypatch.setenv("CMCDW_DSN", "host:1521/svc")
    monkeypatch.delenv("CMCDW_SCHEMA", raising=False)
    monkeypatch.delenv("CMCDW_RESULTS_OBJECT", raising=False)


def test_the_warehouse_view_defaults_to_the_dev_materialised_view(warehouse_env):
    resolved = credentials.warehouse_credentials()

    assert resolved.user == "reader"
    assert resolved.dsn == "host:1521/svc"
    assert resolved.results_view == (
        f"{credentials.DEFAULT_CMCDW_SCHEMA}.{credentials.DEFAULT_CMCDW_RESULTS_OBJECT}"
    )


def test_the_schema_and_view_can_be_repointed_at_prod_without_a_code_change(
    warehouse_env, monkeypatch
):
    monkeypatch.setenv("CMCDW_SCHEMA", "PRODSCI_DM")
    monkeypatch.setenv("CMCDW_RESULTS_OBJECT", "v_combined_results")

    assert credentials.warehouse_credentials().results_view == (
        "PRODSCI_DM.v_combined_results"
    )


@pytest.mark.parametrize("missing", ["CMCDW_USER", "CMCDW_PASSWORD", "CMCDW_DSN"])
def test_each_absent_warehouse_variable_is_named_in_the_failure(
    warehouse_env, monkeypatch, missing
):
    """Naming the variable is the difference between a five-second fix and a 500."""
    monkeypatch.setenv(missing, "   ")

    with pytest.raises(credentials.MissingCredential, match=missing):
        credentials.warehouse_credentials()


def test_the_warehouse_password_is_never_printed_in_a_traceback(warehouse_env):
    """These objects end up in tracebacks, and a DWH credential in a log is a
    reportable incident."""
    text = repr(credentials.warehouse_credentials())

    assert "s3cret" not in text
    assert "password=<redacted>" in text
    assert "reader" in text


# ── notifications ─────────────────────────────────────────────────────────────


class FakeSmtp:
    """A stand-in for `smtplib.SMTP` that cannot reach a network."""

    def __init__(self, log: list, fail: BaseException | None = None) -> None:
        self.log = log
        self.fail = fail

    def __call__(self, host, port, timeout=None):
        self.log.append({"host": host, "port": port, "timeout": timeout})
        if self.fail is not None:
            raise self.fail
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def send_message(self, message):
        self.log[-1]["message"] = message


@pytest.fixture
def smtp(monkeypatch):
    """Captures sends; installed so no test can open a socket to the relay."""
    log: list = []
    monkeypatch.setattr(notify, "smtplib", SimpleNamespace(SMTP=FakeSmtp(log)))
    return log


def make_user(session, username: str, *, email: str | None, name: str = "Owner") -> User:
    user = User(username=username, display_name=name, email=email)
    session.add(user)
    session.commit()
    return user


def test_the_owners_are_emailed_the_request_with_a_link_to_act_on_it(session, smtp):
    make_user(session, "bapatar", email="owner.one@abbvie.com")
    make_user(session, "mohanax25", email="owner.two@abbvie.com", name="Second")
    requester = make_user(session, "asha.rao", email="asha@abbvie.com", name="Asha Rao")

    notify.access_request_raised(session, requester, ["iso", "bop"], "Joining the team")

    assert len(smtp) == 1
    sent = smtp[0]
    assert sent["host"] == notify.settings.smtp_host
    assert sent["port"] == notify.settings.smtp_port
    # A hung relay must not hold a request open; the timeout is part of the contract.
    assert sent["timeout"] == 10
    message = sent["message"]
    assert message["From"] == notify.settings.mail_from
    assert "owner.one@abbvie.com" in message["To"]
    assert "owner.two@abbvie.com" in message["To"]
    assert "Asha Rao" in message["Subject"]
    assert "asha.rao" in message["Subject"]
    body = message.get_content()
    assert "Modules: iso, bop" in body
    assert "Reason given: Joining the team" in body
    assert f"{notify.settings.app_base_url}/users" in body


def test_a_request_with_no_reason_omits_the_reason_line(session, smtp):
    make_user(session, "bapatar", email="owner@abbvie.com")
    requester = make_user(session, "ben.carter", email="ben@abbvie.com", name="Ben")

    notify.access_request_raised(session, requester, ["iso"], None)

    assert "Reason given" not in smtp[0]["message"].get_content()


def test_only_the_fixed_administrators_are_notified_and_case_does_not_matter(
    session, smtp
):
    """The same constant that makes the owners undemotable decides who is told, so
    "who owns access" is stated once."""
    make_user(session, "BAPATAR", email="owner@abbvie.com")
    make_user(session, "someone.else", email="nosy@abbvie.com")
    requester = make_user(session, "mei.lin", email="mei@abbvie.com", name="Mei")

    notify.access_request_raised(session, requester, ["iso"], None)

    recipients = smtp[0]["message"]["To"]
    assert recipients == "owner@abbvie.com"


def test_an_owner_without_an_address_on_file_is_skipped(session, smtp):
    make_user(session, "bapatar", email=None)
    make_user(session, "mohanax25", email="reachable@abbvie.com")
    requester = make_user(session, "mei.lin", email="mei@abbvie.com", name="Mei")

    notify.access_request_raised(session, requester, ["iso"], None)

    assert smtp[0]["message"]["To"] == "reachable@abbvie.com"


def test_no_owner_address_at_all_is_logged_and_opens_no_connection(
    session, smtp, caplog
):
    requester = make_user(session, "mei.lin", email="mei@abbvie.com", name="Mei")

    with caplog.at_level(logging.WARNING, logger=notify.__name__):
        notify.access_request_raised(session, requester, ["iso"], None)

    assert smtp == []
    assert any("No owner email addresses" in r.getMessage() for r in caplog.records)


def test_a_relay_that_refuses_this_host_degrades_to_a_warning(session, monkeypatch, caplog):
    """The relay does not yet permit this host, and a queued request must survive
    that: losing the notification is a nuisance, losing the request is a bug."""
    make_user(session, "bapatar", email="owner@abbvie.com")
    requester = make_user(session, "mei.lin", email="mei@abbvie.com", name="Mei")
    refused = OSError("554 ux00759p.abbvienet.com  IP Not Permitted")
    monkeypatch.setattr(
        notify, "smtplib", SimpleNamespace(SMTP=FakeSmtp([], fail=refused))
    )

    with caplog.at_level(logging.WARNING, logger=notify.__name__):
        assert notify.access_request_raised(session, requester, ["iso"], None) is None

    warning = next(r.getMessage() for r in caplog.records if "Could not email" in r.getMessage())
    assert "mei.lin" in warning
    assert notify.settings.smtp_host in warning
    assert "Mail Relay Authorized Sender" in warning


def test_a_failure_looking_up_the_owners_also_degrades_rather_than_raising(caplog):
    """The lookup is inside the same try as the send: a dead database connection
    after the commit must not turn a saved request into a 500."""

    class BrokenSession:
        def scalars(self, *_args, **_kwargs):
            raise RuntimeError("connection reset")

    requester = User(username="mei.lin", display_name="Mei", email="mei@abbvie.com")

    with caplog.at_level(logging.WARNING, logger=notify.__name__):
        assert (
            notify.access_request_raised(BrokenSession(), requester, ["iso"], None)
            is None
        )

    assert any("Could not email" in r.getMessage() for r in caplog.records)


def test_a_successful_send_is_logged_with_the_recipients(session, smtp, caplog):
    make_user(session, "bapatar", email="owner@abbvie.com")
    requester = make_user(session, "mei.lin", email="mei@abbvie.com", name="Mei")

    with caplog.at_level(logging.INFO, logger=notify.__name__):
        notify.access_request_raised(session, requester, ["iso"], None)

    assert any(
        "Notified owner@abbvie.com" in r.getMessage() for r in caplog.records
    )
