from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from da_platform.credentials import IliadCredentials
from da_platform.llm.client import IliadClient, LlmError
from da_platform.llm.limiter import ProviderLimiter


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        json_valid: bool = True,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.headers = headers or {}
        self._json_valid = json_valid

    def json(self) -> Any:
        if not self._json_valid:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


CREDENTIALS = IliadCredentials(
    base_url="https://iliad.test",
    api_key=REDACTED
    user_token=REDACTED
    text_model="claude-4.5-sonnet",
    vision_model="gpt-5.2-global",
    ca_bundle_path=Path("/tmp/ca-bundle.pem"),
)


def build(responses: list[Any], credentials: IliadCredentials = CREDENTIALS):
    session = FakeSession(responses)
    client = IliadClient(
        credentials=lambda: credentials,
        limiter=ProviderLimiter("test", 4),
        session=session,
    )
    return client, session


def completion(text: str) -> FakeResponse:
    return FakeResponse(payload={"completion": {"content": text}})


@pytest.fixture(autouse=True)
def sleeps(monkeypatch) -> list[float]:
    """Records backoff delays instead of waiting them out."""
    recorded: list[float] = []
    monkeypatch.setattr("da_platform.llm.client.time.sleep", recorded.append)
    return recorded


def test_chat_posts_the_expected_payload_and_returns_content():
    client, session = build([completion("hello")])

    assert client.chat(user="hi", system="be brief") == "hello"

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://iliad.test/api/v1/chat/claude-4.5-sonnet"
    assert call["json"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert call["headers"]["x-api-key"] == "key-123"
    # ISO needs the AD token; BOP does not send one. Including it when present keeps
    # one client valid for both.
    assert call["headers"]["x-user-token"] == "token-abc"
    # Never False. Compared via str() so the assertion holds on Windows too.
    assert call["verify"] == str(CREDENTIALS.ca_bundle_path)


def test_chat_omits_the_system_message_when_not_given():
    client, session = build([completion("ok")])
    client.chat(user="hi")
    assert session.calls[0]["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_user_token_header_is_absent_when_unset():
    credentials = IliadCredentials(
        base_url="https://iliad.test",
        api_key=REDACTED
        user_token=REDACTED
        text_model="m",
        vision_model="v",
        ca_bundle_path=None,
    )
    client, session = build([completion("ok")], credentials)
    client.chat(user="hi")
    assert "x-user-token" not in session.calls[0]["headers"]
    assert session.calls[0]["verify"] is True


def test_model_can_be_overridden_per_call():
    """ISO uses gpt-5.2 and BOP claude-4.5-sonnet, so the model cannot be global."""
    client, session = build([completion("ok")])
    client.chat(user="hi", model="gpt-5.2")
    assert session.calls[0]["url"].endswith("/chat/gpt-5.2")


def test_vision_puts_text_before_the_image_and_uses_a_flat_image_block():
    client, session = build([completion("page text")])

    assert client.chat_vision(image_b64="BASE64", prompt="read this") == "page text"

    content = session.calls[0]["json"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    # Flat block, not the Anthropic-style nested `source`.
    assert content[1] == {
        "type": "image",
        "encoding": "base64",
        "media_type": "image/png",
        "data": "BASE64",
    }
    assert "source" not in content[1]
    assert session.calls[0]["url"].endswith("/chat/gpt-5.2-global")


def test_empty_completion_is_retried_then_reported():
    client, session = build([completion("   "), completion("")])
    with pytest.raises(LlmError, match="failed after 2 attempts"):
        client.chat(user="hi")
    assert len(session.calls) == 2


def test_empty_completion_succeeds_on_retry():
    client, session = build([completion(""), completion("second try")])
    assert client.chat(user="hi") == "second try"
    assert len(session.calls) == 2


def test_401_refreshes_the_token_and_retries_once(monkeypatch):
    refreshed: list[bool] = []
    monkeypatch.setattr(
        "da_platform.llm.client.refresh_iliad_token",
        lambda: refreshed.append(True),
    )
    client, session = build([FakeResponse(status_code=401), completion("after refresh")])

    assert client.chat(user="hi") == "after refresh"
    assert refreshed == [True]
    assert len(session.calls) == 2


def test_retryable_status_is_retried_and_honours_retry_after(sleeps):
    client, session = build(
        [FakeResponse(status_code=429, headers={"Retry-After": "7"}), completion("ok")]
    )

    assert client.chat(user="hi") == "ok"
    assert len(session.calls) == 2
    # The gateway's own hint wins over our computed backoff.
    assert sleeps == [7.0]


def test_backoff_is_used_when_no_retry_after_header(sleeps):
    client, _ = build([FakeResponse(status_code=503), completion("ok")])

    assert client.chat(user="hi") == "ok"
    assert len(sleeps) == 1
    # Jittered, so assert the range rather than an exact value.
    assert 2.0 <= sleeps[0] <= 3.0


def test_timeout_is_retried():
    client, session = build(
        [requests.exceptions.Timeout("too slow"), completion("eventually")]
    )
    assert client.chat(user="hi") == "eventually"
    assert len(session.calls) == 2


# ── multi-image vision (needed by ISO) ────────────────────────────────────────


def test_vision_multi_sends_several_images_in_one_request():
    """ISO's LLM table-of-contents fallback renders up to ten pages and sends them as
    a single request, and its formula triage sends a tight crop plus a wider context
    crop. Both ask the model to compare the images against each other, so they cannot
    be split into one call per image without changing the question being asked."""
    client, session = build([completion("[]")])

    client.chat_vision_multi(images=["FIRST", "SECOND", "THIRD"], prompt="which of these")

    assert len(session.calls) == 1, "one request, not one per image"
    content = session.calls[0]["json"]["messages"][0]["content"]
    # Text first, then one flat image block per entry, in the order given.
    assert content[0] == {"type": "text", "text": "which of these"}
    assert [block["data"] for block in content[1:]] == ["FIRST", "SECOND", "THIRD"]
    assert all(block["type"] == "image" for block in content[1:])
    assert all(block["encoding"] == "base64" for block in content[1:])


def test_one_image_produces_the_same_payload_through_either_method():
    """Guards the reason both share a private helper: BOP calls `chat_vision` and a
    drift between the two payload builders would be invisible until output changed."""
    single, single_session = build([completion("a")])
    multi, multi_session = build([completion("a")])

    single.chat_vision(image_b64="ONLY", prompt="read this")
    multi.chat_vision_multi(images=["ONLY"], prompt="read this")

    assert single_session.calls[0]["json"] == multi_session.calls[0]["json"]


def test_vision_multi_rejects_an_empty_image_list():
    client, _ = build([completion("a")])
    with pytest.raises(ValueError, match="at least one image"):
        client.chat_vision_multi(images=[], prompt="p")


# ── temperature (needed by ISO's garble classifier) ───────────────────────────


def test_temperature_is_sent_when_given():
    """ISO's garbled-page classifier pins temperature to 0. Its verdict decides
    whether a document goes down the text path or the far more expensive vision path,
    so a non-zero temperature makes that routing non-deterministic for one document."""
    client, session = build([completion("ok")])
    client.chat(user="hi", temperature=0)
    assert session.calls[0]["json"]["temperature"] == 0


def test_temperature_is_omitted_when_not_given():
    """Absent rather than defaulted, so BOP's requests are unchanged."""
    client, session = build([completion("ok")])
    client.chat(user="hi")
    assert "temperature" not in session.calls[0]["json"]


def test_vision_also_accepts_a_temperature():
    client, session = build([completion("ok")])
    client.chat_vision(image_b64="X", prompt="p", temperature=0)
    assert session.calls[0]["json"]["temperature"] == 0


# ── per-call timeouts on the RAG upload ───────────────────────────────────────


def test_upload_document_timeouts_are_configurable():
    """ISO uploads with a single 120s attempt and no retry. Retrying a multipart
    upload that timed out client-side but succeeded server-side leaves two copies
    indexed in a shared RAG source, and ISO's cleanup deletes only the first match."""
    client, session = build([FakeResponse(payload={"id": "doc-1"})])

    client.upload_document("src", "f.pdf", b"%PDF-1.4", timeouts=(120,))

    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 120


def test_upload_document_defaults_to_two_attempts():
    client, session = build(
        [requests.exceptions.Timeout("slow"), FakeResponse(payload={"id": "doc-1"})]
    )
    client.upload_document("src", "f.pdf", b"%PDF-1.4")
    assert [call["timeout"] for call in session.calls] == [120, 240]


def test_connection_error_is_not_retried():
    """An unreachable gateway is an environment problem; retrying hides it."""
    client, session = build([requests.exceptions.ConnectionError("no route")])
    with pytest.raises(requests.exceptions.ConnectionError):
        client.chat(user="hi")
    assert len(session.calls) == 1


def test_non_retryable_http_error_raises_immediately():
    client, session = build([FakeResponse(status_code=400, text="bad request")])
    with pytest.raises(LlmError, match="400"):
        client.chat(user="hi")
    assert len(session.calls) == 1


def test_unexpected_response_shape_is_reported_clearly():
    client, _ = build([FakeResponse(payload={"unexpected": True})])
    with pytest.raises(LlmError, match="Unexpected Iliad chat response shape"):
        client.chat(user="hi")


def test_non_json_response_is_reported_clearly():
    client, _ = build([FakeResponse(text="<html>gateway</html>", json_valid=False)])
    with pytest.raises(LlmError, match="non-JSON"):
        client.chat(user="hi")


def test_rag_query_reads_content_from_the_top_level():
    """RAG replies with {"content": ...}; chat replies with {"completion": {...}}."""
    client, session = build([FakeResponse(payload={"content": " ISO 11608-4:2022 "})])

    assert client.rag_query("iso-docs", "which standard?", k=5) == "ISO 11608-4:2022"

    call = session.calls[0]
    assert call["url"] == "https://iliad.test/api/v1/sources/iso-docs/rag"
    assert call["json"] == {
        "messages": [{"role": "user", "content": "which standard?"}],
        "k": 5,
    }


def test_document_upload_drops_the_json_content_type():
    """requests must set the multipart boundary itself; leaving application/json in
    place produces a malformed upload that the gateway rejects opaquely."""
    client, session = build([FakeResponse(payload={"ok": True})])

    client.upload_document("iso-docs", "std.pdf", b"%PDF-1.7", "application/pdf")

    call = session.calls[0]
    assert "Content-Type" not in call["headers"]
    assert call["files"]["file"][0] == "std.pdf"


# The live gateway's actual shape: three flat lists of strings, no "sources" key.
SOURCES_PAYLOAD = {
    "global_sources": ["c4c_test", "shared_corpus"],
    "private_sources": ["iso-docs-extraction"],
    "shared_sources": [],
    "created_by": {},
}


def test_list_sources_merges_the_three_scopes():
    client, _ = build([FakeResponse(payload=SOURCES_PAYLOAD)])
    assert client.list_sources() == [
        "iso-docs-extraction",
        "c4c_test",
        "shared_corpus",
    ]


def test_list_sources_can_be_narrowed_to_one_scope():
    client, _ = build([FakeResponse(payload=SOURCES_PAYLOAD)])
    assert client.list_sources(scope="private") == ["iso-docs-extraction"]


def test_ensure_source_skips_creation_when_already_owned():
    client, session = build([FakeResponse(payload=SOURCES_PAYLOAD)])
    client.ensure_source("iso-docs-extraction", "desc")
    # Only the list call; no POST to create.
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "GET"


def test_ensure_source_creates_when_missing():
    client, session = build(
        [FakeResponse(payload=SOURCES_PAYLOAD), FakeResponse(payload={"ok": True})]
    )
    client.ensure_source("new-source", "desc")
    assert [call["method"] for call in session.calls] == ["GET", "POST"]
    assert session.calls[1]["json"] == {"source": "new-source", "description": "desc"}


def test_ensure_source_creates_even_when_a_global_source_shares_the_name():
    """A global source is readable but not ours to write to, so its presence must not
    suppress creating our own."""
    client, session = build(
        [FakeResponse(payload=SOURCES_PAYLOAD), FakeResponse(payload={"ok": True})]
    )
    client.ensure_source("c4c_test", "desc")
    assert [call["method"] for call in session.calls] == ["GET", "POST"]


def test_unexpected_sources_shape_is_reported():
    client, _ = build([FakeResponse(payload=["not", "a", "dict"])])
    with pytest.raises(LlmError, match="Unexpected Iliad sources response"):
        client.list_sources()
