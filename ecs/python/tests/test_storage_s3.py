"""S3 object store, proved against a hand-rolled fake AWS client.

Hand-rolled because `moto` cannot be installed: the corporate proxy denies pypi
(the same reason `tests/iso_fakes.py` exists). `boto3` itself *is* installed, so
`botocore.exceptions.ClientError` is real here and missing keys, access denial and
transient 5xx are simulated the way botocore actually reports them.

The fake is patched in at `s3.aws` — the module-level name `S3ObjectStore` reaches
`get_clients()` through — rather than inside boto3, so no test can reach the network
even if the credential chain happens to resolve on the machine running the suite.

What is asserted is the *wire contract*: the exact bucket, the exact key and the exact
bytes handed to `put_object`/`get_object`/`head_object`/`delete_object`. Everything
downstream (`run_key`, `asset_key`, the run-file rows, the download route) depends on
those keys being byte-identical to what `LocalObjectStore` writes on disk, because
`tests/test_storage.py` pins the local half of the same contract.

Two classes here are characterization tests of suspected defects and are marked as
such: they lock in today's behaviour so a fix is a deliberate, visible change. They
do not endorse it.
"""

from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError

from api.backend.da_platform.storage import LocalObjectStore
from api.backend.da_platform.storage import s3 as s3_module
from api.backend.da_platform.storage.base import (
    ObjectStore,
    PayloadTooLarge,
    StorageError,
    asset_key,
    run_key,
)
from api.backend.da_platform.storage.s3 import S3ObjectStore

BUCKET = "da-platform-artifacts"


def client_error(code: str, operation: str, status: int = 400) -> ClientError:
    """A ClientError shaped the way botocore shapes one, so the code under test
    reads `response["Error"]["Code"]` for real rather than a convenient stub."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"simulated {code}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeAwsClients:
    """Stands in for `aws.AwsClients`, recording every call verbatim.

    Only `call()` is implemented, because that is the entire surface `S3ObjectStore`
    uses — going through the shared layer is what gives it the credential-expiry retry,
    so a store that reached for `boto3.client` directly would be the bug, and this fake
    would not catch it. `_no_network` below covers that gap.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._scripted: dict[str, list[object]] = {}

    def script(self, operation: str, *outcomes: object) -> None:
        """Queue results for `operation`. An exception instance is raised, not returned."""
        self._scripted.setdefault(operation, []).extend(outcomes)

    def call(self, service: str, operation: str, /, **kwargs: object) -> object:
        assert service == "s3", f"the S3 store must only talk to s3, not {service!r}"
        self.calls.append((service, operation, dict(kwargs)))
        queued = self._scripted.get(operation)
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if operation == "get_object":
            raise AssertionError(
                "get_object was called without a scripted body; script one in the test"
            )
        # put/head/delete return a metadata dict the store ignores.
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    # ── assertion helpers ─────────────────────────────────────────────────────

    def only(self, operation: str) -> dict:
        matching = [kwargs for _, name, kwargs in self.calls if name == operation]
        assert len(matching) == 1, f"expected one {operation}, got {len(matching)}"
        return matching[0]

    def operations(self) -> list[str]:
        return [name for _, name, _ in self.calls]


@pytest.fixture
def fake(monkeypatch) -> FakeAwsClients:
    clients = FakeAwsClients()
    # Patched on the name inside `s3`, so the shared `aws` module is left untouched for
    # any other test in the session.
    monkeypatch.setattr(s3_module, "aws", _AwsNamespace(clients))
    return clients


class _AwsNamespace:
    def __init__(self, clients: FakeAwsClients) -> None:
        self._clients = clients

    def get_clients(self) -> FakeAwsClients:
        return self._clients


@pytest.fixture(autouse=True)
def _bucket_env(monkeypatch):
    """A bucket and no prefix, which is the deployed default: the silo prefix is
    already inside the key, so `S3_PREFIX` is normally empty."""
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.delenv("S3_PREFIX", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this module may open a socket. If a future change makes the store
    build its own client instead of going through `aws.get_clients()`, this fails
    loudly rather than quietly hitting a real bucket with real credentials."""
    import boto3

    def refuse(*_args, **_kwargs):
        raise AssertionError("a real boto3 client was built inside a unit test")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3.Session, "client", refuse)


@pytest.fixture
def store(fake) -> S3ObjectStore:
    return S3ObjectStore()


class TestConfiguration:
    def test_a_missing_bucket_is_refused_with_actionable_advice(self, fake, monkeypatch):
        """A silo never names a bucket, so an unset `S3_BUCKET` is a deployment
        mistake and must say which of the two fixes applies."""
        monkeypatch.delenv("S3_BUCKET", raising=False)
        with pytest.raises(StorageError) as caught:
            S3ObjectStore()
        assert "S3_BUCKET" in str(caught.value)
        assert "STORAGE_BACKEND=local" in str(caught.value)

    def test_an_explicit_bucket_overrides_the_environment(self, fake, monkeypatch):
        S3ObjectStore("other-bucket").put("k.txt", b"x")
        assert fake.only("put_object")["Bucket"] == "other-bucket"

    def test_an_explicit_bucket_is_enough_without_the_environment(self, fake, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        S3ObjectStore("explicit").put("k.txt", b"x")
        assert fake.only("put_object")["Bucket"] == "explicit"

    def test_the_store_satisfies_the_object_store_protocol(self, store):
        """Local and S3 must be interchangeable at the type level, because
        `get_object_store()` returns either one behind the same annotation."""
        assert isinstance(store, ObjectStore)


class TestKeys:
    def test_platform_keys_reach_s3_unaltered(self, store, fake):
        """`run_key` output is the storage_key persisted on the run_files row, so any
        rewriting here would orphan every existing object."""
        key = run_key("BOP", "run-1", "output", "report.docx")
        store.put(key, b"docx")
        assert fake.only("put_object")["Key"] == "BOP/runs/run-1/output/report.docx"

    def test_a_silo_asset_key_reaches_s3_unaltered(self, store, fake):
        """Asset keys mirror the Dataiku managed-folder names, so the same objects
        serve the old app during migration."""
        store.put(asset_key("BOP", "prompts/petra.txt"), b"prompt")
        assert fake.only("put_object")["Key"] == "BOP/prompts/petra.txt"

    def test_a_leading_slash_never_becomes_an_empty_first_segment(self, store, fake):
        """S3 accepts `//a/b` as a distinct key from `/a/b`, which would silently
        split one silo's objects across two locations."""
        store.put("/BOP/runs/r1/input/a.pdf", b"x")
        assert fake.only("put_object")["Key"] == "BOP/runs/r1/input/a.pdf"

    def test_an_optional_extra_base_is_prepended(self, fake, monkeypatch):
        """`S3_PREFIX` exists for a bucket the platform shares with something else;
        normally it is empty."""
        monkeypatch.setenv("S3_PREFIX", "da-platform")
        S3ObjectStore().put("BOP/runs/r1/output/x.docx", b"x")
        assert fake.only("put_object")["Key"] == "da-platform/BOP/runs/r1/output/x.docx"

    def test_the_extra_base_is_normalised_so_slashes_never_double(self, fake, monkeypatch):
        monkeypatch.setenv("S3_PREFIX", "/da-platform/")
        S3ObjectStore().put("/BOP/x.txt", b"x")
        assert fake.only("put_object")["Key"] == "da-platform/BOP/x.txt"

    def test_every_operation_uses_the_same_resolved_key(self, fake, monkeypatch):
        """put, open, exists and delete must agree, or an object is written where
        nothing later looks for it."""
        monkeypatch.setenv("S3_PREFIX", "base")
        store = S3ObjectStore()
        fake.script("get_object", {"Body": io.BytesIO(b"body")})
        store.put("/k.txt", b"x")
        store.open("/k.txt")
        store.exists("/k.txt")
        store.delete("/k.txt")
        assert {kwargs["Key"] for _, _, kwargs in fake.calls} == {"base/k.txt"}
        assert {kwargs["Bucket"] for _, _, kwargs in fake.calls} == {BUCKET}


class TestPut:
    def test_put_sends_the_exact_bytes_and_returns_the_count(self, store, fake):
        assert store.put("runs/r1/output/report.docx", b"docx bytes") == 10
        kwargs = fake.only("put_object")
        assert kwargs["Body"] == b"docx bytes"
        assert kwargs["Bucket"] == BUCKET
        assert kwargs["Key"] == "runs/r1/output/report.docx"

    def test_put_of_empty_bytes_still_creates_the_object(self, store, fake):
        """A zero-byte output is a real result the download route must be able to
        serve, not something to skip."""
        assert store.put("runs/r1/output/empty.txt", b"") == 0
        assert fake.only("put_object")["Body"] == b""

    def test_a_transient_failure_is_not_swallowed(self, store, fake):
        """The shared layer retries expired credentials; everything else must surface
        so the run fails visibly instead of appearing to have stored the file."""
        fake.script("put_object", client_error("InternalError", "PutObject", 500))
        with pytest.raises(ClientError):
            store.put("k.txt", b"x")

    def test_access_denial_is_not_swallowed(self, store, fake):
        fake.script("put_object", client_error("AccessDenied", "PutObject", 403))
        with pytest.raises(ClientError):
            store.put("k.txt", b"x")


class TestPutStream:
    def test_a_stream_is_stored_and_the_written_count_returned(self, store, fake):
        written = store.put_stream("a/b.bin", iter([b"12345", b"67890"]))
        assert written == 10
        assert fake.only("put_object")["Body"] == b"1234567890"

    def test_an_empty_stream_produces_an_empty_object(self, store, fake):
        assert store.put_stream("a/b.bin", iter([])) == 0
        assert fake.only("put_object")["Body"] == b""

    def test_an_oversized_stream_is_rejected_before_anything_is_stored(self, store, fake):
        """The ceiling must fire *before* `put_object`, so a rejected upload leaves no
        partial object — the S3 equivalent of the local store's unlink-on-reject."""
        with pytest.raises(PayloadTooLarge) as caught:
            store.put_stream("a/big.bin", iter([b"x" * 10] * 10), max_bytes=25)
        assert "25" in str(caught.value)
        assert fake.operations() == [], "a rejected upload must not reach S3 at all"

    def test_the_ceiling_is_exclusive_so_an_exactly_sized_upload_is_accepted(
        self, store, fake
    ):
        """`MAX_UPLOAD_MB` is a documented limit; a file of exactly that size must
        pass, matching `LocalObjectStore`."""
        assert store.put_stream("a/b.bin", iter([b"x" * 25]), max_bytes=25) == 25
        assert len(fake.only("put_object")["Body"]) == 25

    def test_the_ceiling_fires_mid_stream_and_stops_consuming(self, store, fake):
        """Proves the check is per-chunk rather than after the generator drains: an
        attacker-supplied endless stream must not be read to completion."""
        consumed = 0

        def endless():
            nonlocal consumed
            while True:
                consumed += 1
                assert consumed < 1000, "the ceiling never fired"
                yield b"x" * 10

        with pytest.raises(PayloadTooLarge):
            store.put_stream("a/big.bin", endless(), max_bytes=25)
        assert consumed == 3, "should stop at the first chunk that crosses the ceiling"

    def test_no_ceiling_means_no_limit(self, store, fake):
        assert store.put_stream("a/b.bin", iter([b"x" * 100]), max_bytes=None) == 100

    # ── characterization: suspected defect ────────────────────────────────────

    def test_put_stream_buffers_the_whole_payload_contrary_to_its_contract(
        self, store, fake
    ):
        """CHARACTERIZATION of a suspected defect at storage/s3.py:51 — not endorsed.

        `ObjectStore.put_stream` (storage/base.py:72) says "Store a stream without
        buffering it whole", and `LocalObjectStore` honours it by writing each chunk
        straight to the file handle. This implementation copies every chunk into one
        `io.BytesIO` and issues a single `put_object`, so peak memory equals the whole
        payload; the only guard is `MAX_UPLOAD_MB` being small. The module docstring
        concedes this and says it must become a multipart upload if the limit rises.

        Locked in by asserting exactly ONE `put_object` carrying the concatenation of
        all four chunks: a genuinely streaming implementation would show a multipart
        sequence instead, and this test would then need rewriting deliberately.
        """
        store.put_stream("a/b.bin", iter([b"aa", b"bb", b"cc", b"dd"]))
        assert fake.operations() == ["put_object"]
        assert fake.only("put_object")["Body"] == b"aabbccdd"


class TestOpen:
    def test_open_returns_a_readable_handle_over_the_object_bytes(self, store, fake):
        fake.script("get_object", {"Body": io.BytesIO(b"docx bytes")})
        with store.open("runs/r1/output/report.docx") as handle:
            assert handle.read() == b"docx bytes"
        assert fake.only("get_object")["Key"] == "runs/r1/output/report.docx"

    def test_the_handle_is_seekable_because_callers_re_read_it(self, store, fake):
        """The streaming response and PyMuPDF both expect a real file-like object,
        not botocore's one-shot StreamingBody."""
        fake.script("get_object", {"Body": io.BytesIO(b"abcdef")})
        handle = store.open("k.bin")
        assert handle.read() == b"abcdef"
        handle.seek(0)
        assert handle.read(3) == b"abc"

    def test_the_response_body_is_drained_so_the_connection_is_released(self, store, fake):
        """botocore leaks a connection if the body is left unread; the store must
        consume it eagerly rather than hand the socket to the caller."""

        class RecordingBody:
            def __init__(self) -> None:
                self.reads = 0

            def read(self) -> bytes:
                self.reads += 1
                return b"payload"

        body = RecordingBody()
        fake.script("get_object", {"Body": body})
        assert store.open("k.bin").read() == b"payload"
        assert body.reads == 1

    def test_access_denial_on_read_is_not_mistaken_for_a_missing_object(self, store, fake):
        fake.script("get_object", client_error("AccessDenied", "GetObject", 403))
        with pytest.raises(ClientError):
            store.open("k.bin")

    # ── characterization: suspected defect ────────────────────────────────────

    @pytest.mark.parametrize("code", ["NoSuchKey", "404"])
    def test_a_missing_object_raises_client_error_not_file_not_found(
        self, store, fake, code
    ):
        """CHARACTERIZATION of a suspected defect at storage/s3.py:68 — not endorsed.

        `LocalObjectStore.open` raises `FileNotFoundError` for a missing key, and
        `routers/documents.py:178` catches exactly that to answer 410 Gone ("The stored
        file is missing"). This backend lets botocore's `NoSuchKey` escape untranslated,
        so the identical missing object is a clean 410 on local and an unhandled 500 on
        S3. The two backends are therefore not substitutable behind `ObjectStore`,
        despite `get_object_store()` returning them interchangeably.
        """
        fake.script("get_object", client_error(code, "GetObject", 404))
        with pytest.raises(ClientError):
            store.open("runs/r1/output/gone.docx")

        # The exception the download route actually looks for is not raised, and it is
        # not a StorageError either, so no platform-level handler catches it.
        fake.script("get_object", client_error(code, "GetObject", 404))
        with pytest.raises(ClientError) as caught:
            store.open("runs/r1/output/gone.docx")
        assert not isinstance(caught.value, (FileNotFoundError, StorageError))

    def test_the_local_backend_does_translate_the_same_condition(self, tmp_path):
        """The other half of the defect above, kept adjacent so the divergence is
        visible in one place. Mirrors test_storage.py's local assertion."""
        with pytest.raises(FileNotFoundError):
            LocalObjectStore(tmp_path).open("runs/r1/output/gone.docx")


class TestExists:
    def test_exists_head_requests_the_key_and_reports_true(self, store, fake):
        assert store.exists("runs/r1/output/report.docx") is True
        assert fake.operations() == ["head_object"]
        assert fake.only("head_object")["Key"] == "runs/r1/output/report.docx"

    @pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
    def test_exists_is_false_for_every_code_s3_uses_for_absence(self, store, fake, code):
        """head_object reports absence as bare `404`/`NotFound` while get_object says
        `NoSuchKey`; all three must mean the same thing here."""
        fake.script("head_object", client_error(code, "HeadObject", 404))
        assert store.exists("runs/r1/output/gone.docx") is False

    def test_access_denial_is_not_reported_as_a_missing_object(self, store, fake):
        """A bucket-policy mistake must not read as "not generated yet" — that is how
        a broken deployment looks healthy."""
        fake.script("head_object", client_error("AccessDenied", "HeadObject", 403))
        with pytest.raises(ClientError):
            store.exists("runs/r1/output/report.docx")

    def test_a_transient_server_error_is_not_reported_as_a_missing_object(
        self, store, fake
    ):
        fake.script("head_object", client_error("InternalError", "HeadObject", 500))
        with pytest.raises(ClientError):
            store.exists("k.txt")

    def test_an_error_with_no_code_at_all_is_re_raised(self, store, fake):
        """Defensive: the `.get("Error", {})` walk must not turn a malformed error
        response into a confident "the object is missing"."""
        fake.script("head_object", ClientError({}, "HeadObject"))
        with pytest.raises(ClientError):
            store.exists("k.txt")


class TestDelete:
    def test_delete_targets_the_exact_key(self, store, fake):
        assert store.delete("runs/r1/input/source.pdf") is None
        assert fake.operations() == ["delete_object"]
        kwargs = fake.only("delete_object")
        assert kwargs["Bucket"] == BUCKET
        assert kwargs["Key"] == "runs/r1/input/source.pdf"

    def test_delete_of_a_missing_key_is_silent(self, store, fake):
        """S3 delete is already idempotent, which is what `LocalObjectStore`'s
        `missing_ok=True` reproduces locally."""
        assert store.delete("nope/missing.txt") is None

    def test_a_failed_delete_surfaces(self, store, fake):
        fake.script("delete_object", client_error("AccessDenied", "DeleteObject", 403))
        with pytest.raises(ClientError):
            store.delete("k.txt")


class TestRoundTrip:
    def test_put_then_open_returns_the_same_bytes_through_the_fake_bucket(
        self, store, fake
    ):
        """Mirrors test_storage.py's local round trip, so both backends are shown to
        satisfy the same observable sequence."""
        payload = b"\x89PNG\r\n\x1a\n binary payload"
        key = run_key("ISO_Doc_Generation", "run-9", "media", "page-1.png")

        assert store.put(key, payload) == len(payload)
        stored = fake.only("put_object")["Body"]
        # Feed exactly what was stored back, which is what a real bucket would do.
        fake.script("get_object", {"Body": io.BytesIO(stored)})
        assert store.open(key).read() == payload
