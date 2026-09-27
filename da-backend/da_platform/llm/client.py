"""The shared Iliad client (D8).

Both silos use an identical chat contract, so one client serves both:

    POST {base}/api/v1/chat/{model}   ->  {"completion": {"content": "..."}}

ISO additionally uses the RAG endpoints under `/api/v1/sources/...`, whose response
puts the answer at the **top level** (`{"content": ...}`) rather than under
`completion`. That asymmetry is handled here so no silo has to remember it.

Silos receive an instance as `ctx.llm` and never construct one, so the API key, the
CA bundle and the concurrency ceiling exist in exactly one place.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests

from da_platform.credentials import (
    IliadCredentials,
    iliad_credentials,
    llm_max_concurrency,
    refresh_iliad_token,
)
from da_platform.llm.limiter import ProviderLimiter

logger = logging.getLogger(__name__)

# Matches the timeouts the migrated apps settled on: one quick attempt, then a
# patient one, because Iliad is slow rather than flaky under load.
DEFAULT_TIMEOUTS = (120, 240)
RAG_TIMEOUTS = (60, 120)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 30.0


class LlmError(RuntimeError):
    """A provider call that could not be completed."""


class EmptyCompletion(LlmError):
    """The gateway returned 200 with no usable content. Worth retrying."""


def _backoff(attempt: int) -> float:
    # Jitter matters: without it, 15 parallel calls that all get 429 would retry in
    # lockstep and produce the same burst again.
    return min(2.0**attempt, MAX_BACKOFF_SECONDS) + random.uniform(0, 1)


def _retry_after(response: requests.Response) -> float | None:
    """Prefer the gateway's own backoff hint over our guess."""
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return min(float(header), MAX_BACKOFF_SECONDS)
    except ValueError:
        # The header may carry an HTTP date; fall back to our own backoff.
        return None


class IliadClient:
    def __init__(
        self,
        *,
        credentials: Callable[[], IliadCredentials] = iliad_credentials,
        limiter: ProviderLimiter | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._credentials = credentials
        self._limiter = limiter or ProviderLimiter("iliad", llm_max_concurrency())
        # A Session gives connection pooling, which matters when a single run fires
        # 15 calls at once.
        self._session = session or requests.Session()

    # ── chat ──────────────────────────────────────────────────────────────────

    def chat(
        self,
        *,
        user: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 8000,
        temperature: float | None = None,
        timeouts: tuple[int, ...] = DEFAULT_TIMEOUTS,
    ) -> str:
        credentials = self._credentials()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        return self._call(
            f"/api/v1/chat/{model or credentials.text_model}",
            json_body=_body(messages, max_tokens, temperature),
            timeouts=timeouts,
            parse=_completion_content,
        )

    def chat_vision(
        self,
        *,
        image_b64: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 4096,
        media_type: str = "image/png",
        temperature: float | None = None,
        timeouts: tuple[int, ...] = DEFAULT_TIMEOUTS,
    ) -> str:
        """One image per call.

        Two contract details that are easy to get wrong and that both silos had to
        discover: the text block must come **before** the image block, and the image
        is a *flat* block carrying `encoding`/`media_type`/`data` — not the
        Anthropic-style nested `source`. A non-vision model rejects image content.
        """
        return self._vision(
            [image_b64],
            prompt=prompt,
            model=model,
            max_tokens=REDACTED
            media_type=media_type,
            temperature=temperature,
            timeouts=timeouts,
        )

    def chat_vision_multi(
        self,
        *,
        images: list[str],
        prompt: str,
        model: str | None = None,
        max_tokens: int = 4096,
        media_type: str = "image/png",
        temperature: float | None = None,
        timeouts: tuple[int, ...] = DEFAULT_TIMEOUTS,
    ) -> str:
        """Several images in ONE request, which is not the same as several requests.

        ISO asks the model to compare the images against each other: its
        table-of-contents fallback sends up to ten rendered pages and picks the
        contents page out of them, and its formula triage sends a tight crop plus a
        wider context crop, using the wider one to decide whether the tight one is an
        equation. Sending them separately would change the question being asked, not
        just the transport — which is why this exists rather than a loop at the call
        site.
        """
        if not images:
            raise ValueError("chat_vision_multi needs at least one image")
        return self._vision(
            images,
            prompt=prompt,
            model=model,
            max_tokens=REDACTED
            media_type=media_type,
            temperature=temperature,
            timeouts=timeouts,
        )

    def _vision(
        self,
        images: list[str],
        *,
        prompt: str,
        model: str | None,
        max_tokens: int,
        media_type: str,
        temperature: float | None,
        timeouts: tuple[int, ...],
    ) -> str:
        """Shared payload construction, so one image and many cannot drift apart."""
        credentials = self._credentials()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image",
                "encoding": "base64",
                "media_type": media_type,
                "data": image,
            }
            for image in images
        )
        return self._call(
            f"/api/v1/chat/{model or credentials.vision_model}",
            json_body=_body(
                [{"role": "user", "content": content}], max_tokens, temperature
            ),
            timeouts=timeouts,
            parse=_completion_content,
        )

    # ── RAG (used by ISO) ─────────────────────────────────────────────────────

    def list_sources(self, scope: str = "all") -> list[str]:
        """List RAG source names.

        The gateway returns `{"global_sources": [...], "private_sources": [...],
        "shared_sources": [...]}` — flat lists of strings, with **no** top-level
        `sources` key. Verified against the live API.
        """
        data = self._call(
            "/api/v1/sources", method="GET", timeouts=RAG_TIMEOUTS, parse=_identity
        )
        if not isinstance(data, dict):
            raise LlmError(
                f"Unexpected Iliad sources response ({type(data).__name__})"
            )

        keys = (
            [f"{scope}_sources"]
            if scope in {"global", "private", "shared"}
            else ["private_sources", "shared_sources", "global_sources"]
        )
        names: list[str] = []
        for key in keys:
            for item in data.get(key) or []:
                # Strings today; tolerate dicts in case the shape gains detail.
                name = item.get("source", "") if isinstance(item, dict) else str(item)
                if name and name not in names:
                    names.append(name)
        return names

    def ensure_source(self, source: str, description: str = "") -> None:
        """Create the source if the caller does not already own it.

        Checks **private** sources only: a global source of the same name is
        readable but not writable by us, so its presence must not suppress creation.
        """
        if source in self.list_sources(scope="private"):
            return
        self._call(
            "/api/v1/sources",
            json_body={"source": source, "description": description},
            timeouts=RAG_TIMEOUTS,
            parse=_identity,
        )

    def upload_document(
        self,
        source: str,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
        timeouts: tuple[int, ...] = (120, 240),
    ) -> Any:
        """Upload a document to a RAG source.

        `timeouts` is exposed because retrying is not free here: an upload that times
        out on our side but succeeded on the gateway's leaves the document indexed
        twice, and ISO's cleanup deletes only the first filename match — so the second
        copy is orphaned in a source shared by everyone. A caller that would rather
        fail than duplicate passes a single-element tuple.
        """
        return self._call(
            f"/api/v1/sources/{source}/documents",
            files={"file": (filename, content, content_type)},
            timeouts=timeouts,
            parse=_identity,
        )

    def rag_query(self, source: str, question: str, k: int = 3) -> str:
        """RAG answers arrive at the top level, not under `completion`."""
        return self._call(
            f"/api/v1/sources/{source}/rag",
            json_body={"messages": [{"role": "user", "content": question}], "k": k},
            timeouts=RAG_TIMEOUTS,
            parse=_rag_content,
        )

    def list_documents(self, source: str) -> Any:
        return self._call(
            f"/api/v1/sources/{source}/documents",
            method="GET",
            timeouts=RAG_TIMEOUTS,
            parse=_identity,
        )

    def delete_document(self, source: str, document_id: str) -> None:
        self._call(
            f"/api/v1/sources/{source}/documents/{document_id}",
            method="DELETE",
            timeouts=RAG_TIMEOUTS,
            parse=_identity,
        )

    # ── transport ─────────────────────────────────────────────────────────────

    def _call(
        self,
        path: str,
        *,
        method: str = "POST",
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeouts: tuple[int, ...],
        parse: Callable[[Any], Any],
    ) -> Any:
        last_error: Exception | None = None
        token_refreshed = REDACTED

        for attempt, timeout in enumerate(timeouts, start=1):
            credentials = self._credentials()
            headers = credentials.headers()
            if files is not None:
                # requests must set the multipart boundary itself; leaving the JSON
                # content type in place produces a silently malformed upload.
                headers.pop("Content-Type", None)

            url = f"{credentials.base_url}{path}"
            try:
                with self._limiter.slot():
                    response = self._session.request(
                        method,
                        url,
                        headers=headers,
                        json=json_body,
                        files=files,
                        timeout=timeout,
                        verify=credentials.verify,
                    )
            except requests.exceptions.Timeout as exc:
                last_error = exc
                logger.info("Iliad %s %s timed out after %ss", method, path, timeout)
                continue
            except requests.exceptions.ConnectionError:
                # Not retried: a connection failure is an environment problem, and
                # retrying only delays a clear error.
                raise

            if response.status_code == 401 and not token_refreshed:
                # The AD token expiring mid-run is exactly what broke the app being
                # replaced, which cached it once and then failed silently.
                logger.info("Iliad returned 401; refreshing the user token and retrying")
                refresh_iliad_token()
                token_refreshed = REDACTED
                last_error = LlmError("401 from Iliad")
                continue

            if response.status_code in RETRYABLE_STATUSES:
                last_error = LlmError(
                    f"{response.status_code} from Iliad: {response.text[:200]}"
                )
                delay = _retry_after(response) or _backoff(attempt)
                logger.info(
                    "Iliad %s returned %s; retrying in %.1fs",
                    path,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                raise LlmError(
                    f"Iliad {method} {path} failed: {response.status_code} "
                    f"{response.text[:200]}"
                ) from exc

            try:
                return parse(response.json())
            except EmptyCompletion as exc:
                last_error = exc
                logger.info("Iliad %s returned empty content; retrying", path)
                continue
            except ValueError as exc:  # not JSON
                raise LlmError(
                    f"Iliad {method} {path} returned non-JSON: {response.text[:200]}"
                ) from exc

        raise LlmError(
            f"Iliad {method} {path} failed after {len(timeouts)} attempts: {last_error}"
        )


def _body(
    messages: list[dict[str, Any]], max_tokens: int, temperature: float | None
) -> dict[str, Any]:
    """The chat request body, with `temperature` present only when asked for.

    Omitted rather than defaulted so the gateway applies its own default and BOP's
    requests are byte-identical to before this parameter existed.
    """
    payload: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def _identity(data: Any) -> Any:
    return data


def _completion_content(data: Any) -> str:
    try:
        content = data["completion"]["content"]
    except (KeyError, TypeError) as exc:
        keys = list(data) if isinstance(data, dict) else type(data).__name__
        raise LlmError(f"Unexpected Iliad chat response shape (got {keys})") from exc
    if not content or not str(content).strip():
        raise EmptyCompletion("Iliad returned an empty completion")
    return str(content)


def _rag_content(data: Any) -> str:
    if not isinstance(data, dict):
        raise LlmError(f"Unexpected Iliad RAG response shape ({type(data).__name__})")
    content = data.get("content", "")
    if not content or not str(content).strip():
        raise EmptyCompletion("Iliad RAG returned no content")
    return str(content).strip()


_client: IliadClient | None = None


def get_client() -> IliadClient:
    """Process-wide client, so the connection pool and the concurrency ceiling are
    shared by every stage running in this worker."""
    global _client
    if _client is None:
        _client = IliadClient()
    return _client
