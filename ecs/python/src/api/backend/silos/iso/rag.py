"""Naming the standard, via Ask a Source. Ported from backend.py:134-258.

The document's title is the ISO number and full title, and the most reliable place to
get it is the standard's own text — so the PDF is uploaded to a RAG source, asked what
it is, and deleted again. The answer names the output file; the PDF's own metadata title
is used for the title printed *inside* the document, and the two are different values.

What the port changed, and nothing else:

* `_rag_headers()` (154-158) is gone. It read the gateway key and user token straight
  out of the environment, which is precisely what a silo may no longer do; the shared
  client owns the credentials, the base URL and the CA bundle.
* The five `requests` calls (196, 213, 225, 242, 253) become client methods. The upload
  passes a single-attempt timeout on purpose — see the note on it below.
* The 60-second indexing wait now reports progress while it waits, because the reaper
  re-queues a run whose heartbeat is older than 900 seconds and this function is
  otherwise silent.

Everything else is as it was, including both query strings, both `k` values, the
swallow-everything-and-return-None contract, and the best-effort cleanup.
"""

from __future__ import annotations

import logging
import time

from .workspace import ProgressFn, no_progress

logger = logging.getLogger(__name__)

# ISO's own RAG source name (backend.py:139). Shared with the app being replaced, which
# is why the cleanup below matters: documents left behind accumulate for everyone.
_ILIAD_SOURCE = "iso-docs-extraction"
_SOURCE_DESCRIPTION = "ISO standard documents for number extraction"

# backend.py:206. No status poll exists, so a slow index still yields an empty answer.
# Split into steps only so the wait can report progress; the total is unchanged.
_INDEX_WAIT_SECONDS = 60
_INDEX_WAIT_STEP = 10

# The two queries, verbatim from backend.py:209-212 and 224.
_PRIMARY_QUESTION = (
    "What is the ISO standard number and full title of this document? "
    "Return only the identifier and title, e.g. 'ISO 11608-4:2022 Needle-based ...'"
)
_FALLBACK_QUESTION = (
    "What ISO number is this document? Give just the number, e.g. ISO 11608-4:2022"
)
_PRIMARY_K = 3
_FALLBACK_K = 5


def _ask(llm, question: str, k: int) -> str:
    """One RAG query, reduced to 'the answer or empty'.

    ISO read `r.json().get("content", "").strip() if r.ok else ""`, so an empty answer
    and a failed response were the same thing: fall through to the next query. The
    shared client raises instead — `EmptyCompletion` for empty content and `LlmError`
    for anything else — so both are caught here to restore the original control flow.

    One deliberate difference: ISO let a *raising* request escape to the outer handler
    and return None without trying the fallback. Here the fallback is tried, costing one
    extra query in a failure case that was already going to produce nothing.
    """
    try:
        return (llm.rag_query(_ILIAD_SOURCE, question, k=k) or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unanswerable query is not fatal
        logger.info("RAG query (k=%d) returned nothing: %s", k, exc)
        return ""


def _wait_for_indexing(progress: ProgressFn) -> None:
    """backend.py:206 — `time.sleep(60)`, with a heartbeat."""
    waited = 0
    while waited < _INDEX_WAIT_SECONDS:
        step = min(_INDEX_WAIT_STEP, _INDEX_WAIT_SECONDS - waited)
        time.sleep(step)
        waited += step
        progress(f"Waiting for the standard to be indexed ({waited}s of {_INDEX_WAIT_SECONDS}s)")


def _delete_uploaded(llm, filename: str) -> None:
    """Best-effort cleanup (backend.py:239-258).

    Every failure is swallowed, as in the source. The `break` on the first filename
    match is also the source's: if an upload were somehow indexed twice, the second copy
    would be orphaned — which is why the upload itself does not retry.
    """
    try:
        listed = llm.list_documents(_ILIAD_SOURCE)
        documents = listed if isinstance(listed, list) else listed.get("documents", [])
        document_id = None
        for document in documents:
            if document.get("filename") == filename or document.get("name") == filename:
                document_id = document.get("id") or document.get("document_id")
                break
        if document_id:
            llm.delete_document(_ILIAD_SOURCE, document_id)
            logger.info("Removed %s from the %s source", filename, _ILIAD_SOURCE)
    except Exception as exc:  # noqa: BLE001 - matches the source's bare tolerance
        logger.info("Could not clean up %s from the RAG source: %s", filename, exc)


def get_iso_number(
    pdf_bytes: bytes,
    filename: str,
    *,
    llm,
    progress: ProgressFn = no_progress,
) -> str | None:
    """Return the ISO number and title, or None if any part of the flow fails.

    Flow, unchanged from backend.py:177-258:
      1. ensure the source exists
      2. upload the document
      3. wait 60s for indexing
      4. query, then a simpler query if the first answered nothing
      5. delete the document again, whatever happened
    """
    try:
        # Checks private sources only: a global source of the same name is readable but
        # not writable by us, so its presence must not suppress creation.
        llm.ensure_source(_ILIAD_SOURCE, _SOURCE_DESCRIPTION)

        progress("Uploading the standard for identification")
        # A single attempt. Retrying a multipart upload that timed out locally but
        # succeeded remotely would index the document twice, and the cleanup above
        # deletes only the first match — leaving a copy behind in a shared source.
        llm.upload_document(_ILIAD_SOURCE, filename, pdf_bytes, timeouts=(120,))

        _wait_for_indexing(progress)

        progress("Asking which standard this is")
        title = _ask(llm, _PRIMARY_QUESTION, _PRIMARY_K)
        if not title:
            title = _ask(llm, _FALLBACK_QUESTION, _FALLBACK_K)

        logger.info("RAG identified the document as %r", title)
        return title or None

    except Exception as exc:  # noqa: BLE001 - the caller falls back to PDF metadata
        # Verbatim contract from backend.py:236-237. Worth knowing when reading logs:
        # a document whose title ends up being its filename has usually failed here,
        # not been given a bad PDF.
        logger.warning("Could not identify the standard via RAG: %s", exc)
        return None

    finally:
        _delete_uploaded(llm, filename)
