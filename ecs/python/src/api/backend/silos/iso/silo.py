"""ISO — the silo contract.

Two blocking Flask endpoints become four stages:

    POST /upload   (backend.py:5416-5525)  ->  ingest, outline
    <the user picks a section range>       ->  await_range
    POST /generate (backend.py:5529-5585)  ->  build

`/upload` did the ingest *and* the outline inside the HTTP request and kept the result
in a module-level `sessions[session_id]` dict (5501-5507). Those five keys —
`pdf_path`, `toc`, `doc_title`, `use_vision`, `llm_json_path` — are now the `ingest` and
`outline` checkpoints. That is the point: a restart between the upload and the user's
choice no longer loses minutes of vision extraction, and a second replica no longer
answers "Session not found" for a perfectly good upload.

Gone from here because the platform owns it: the `.pdf` check (now `ACCEPTS`), the
`session_id` uuid, writing the upload to the uploads folder as
`{session_id}_{filename}`, and deleting that object again on failure. Stages start from
`ctx.storage`.

The ported modules are imported inside the stage bodies rather than at the top of this
file, for two reasons: a silo whose heavy module fails to import must still be
discoverable, because the registry skips a silo whose `silo.py` raises and that would
take the range endpoint down with it and strand every parked run; and the contract,
router and resume tests then need neither PyMuPDF nor the extraction modules loaded.
"""

from __future__ import annotations

import json
import logging
import os

from . import workspace
from .location import STORAGE_FOLDERS, STORAGE_PREFIX  # noqa: F401 — read by the registry

LABEL = "Standards (ISO) Applicability Assessment"
ACCEPTS = [".pdf"]
STAGES = ["ingest", "outline", "await_range", "build"]

logger = logging.getLogger(__name__)

# `{session_id}_llm.json` (backend.py:5457-5464), now an object rather than a file in a
# managed folder. Vision extraction of a 200-page standard is the most expensive thing
# this silo does, so it is checkpointed by key and a resume never repeats it.
LLM_PAGES_MEDIA = "llm.json"

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

RANGE_PROMPT = "Select the sections to generate"


def _save_llm_pages(run, ctx, pages: list[dict]) -> str:
    """Persist the vision pages, minus `b64`.

    Verbatim from backend.py:5457-5464: the base64 page images are stripped before
    saving. They are the bulk of the payload and nothing downstream reads them —
    `prescan_media` re-renders from the PDF instead.
    """
    stripped = [
        {key: value for key, value in page.items() if key != "b64"} for page in pages
    ]
    key = ctx.storage.put_media(
        run, LLM_PAGES_MEDIA, json.dumps(stripped, ensure_ascii=False).encode("utf-8")
    )
    logger.info("run %s: stored %d extracted page(s) at %s", run.id, len(stripped), key)
    return key


def _load_llm_pages(ctx, ingest_output: dict) -> list[dict] | None:
    """Reload the vision pages, tolerating failure.

    backend.py:5551-5559 wrapped this in try/except and set `llm_pages = None` on any
    error, so a lost or unreadable file degraded to the text path rather than failing
    the run. Preserved exactly.
    """
    key = ingest_output.get("llm_pages_key")
    if not key:
        return None
    try:
        with ctx.storage.open_key(key) as handle:
            return json.loads(handle.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - matches the source's bare tolerance
        logger.warning("Ignoring unreadable extracted pages %s: %s", key, exc)
        return None


def ingest(run, ctx):
    """Decide text vs vision, and on the vision path extract every page.

    The first half of `POST /upload`. Silent for a long time in the source; the ported
    `_extract_all_pages_via_llm` reports per page through `ws.progress`, which doubles
    as the heartbeat.
    """
    import fitz

    from . import garble, vision

    source_name = ctx.storage.input_filename(run) or workspace.FALLBACK_PDF_NAME

    with workspace.open_run(run, ctx) as ws:
        ctx.progress("Reading the PDF", pct=5)
        with fitz.open(ws.pdf_path) as document:
            total_pages = len(document)

        use_vision = garble.is_pdf_garbled(ws.pdf_path, llm=ctx.llm, progress=ws.progress)
        logger.info("run %s: use_vision=%s", run.id, use_vision)

        llm_pages_key = None
        if use_vision:
            ctx.progress(f"Reading {total_pages} page(s) by vision", pct=8)
            pages = vision.extract_all_pages(
                ws.pdf_path, total_pages, llm=ctx.llm, progress=ws.progress
            )
            llm_pages_key = _save_llm_pages(run, ctx, pages)

    ctx.progress(
        f"Read {total_pages} page(s) "
        f"{'by vision' if use_vision else 'from the text layer'}",
        pct=20,
    )
    return {
        "use_vision": use_vision,
        "llm_pages_key": llm_pages_key,
        "total_pages": total_pages,
        "source_name": source_name,
    }


def outline(run, ctx):
    """Build the table of contents the range picker renders, and name the document.

    The second half of `POST /upload` (5466-5507). Its own stage so a failure here costs
    the outline rather than the vision extraction before it.
    """
    import fitz

    from . import rag, toc as toc_module

    ingested = ctx.checkpoint("ingest") or {}
    source_name = ingested.get("source_name") or workspace.FALLBACK_PDF_NAME
    llm_pages = _load_llm_pages(ctx, ingested)

    with workspace.open_run(run, ctx) as ws:
        ctx.progress("Reading the table of contents", pct=22)

        if ingested.get("use_vision"):
            # Only parses the real contents page(s), never body text, so it produces no
            # duplicates, dot-leader artefacts or table-row noise (5467-5470).
            entries = toc_module.build_toc_from_llm_pages(llm_pages)
            logger.info("run %s: vision outline found %d entry/entries", run.id, len(entries))
            if not entries:
                # Last resort: fitz bookmarks are sometimes present even in a garbled
                # PDF (5474-5477).
                entries = toc_module.extract_toc(ws.pdf_path, llm=ctx.llm)
        else:
            entries = toc_module.extract_toc(ws.pdf_path, llm=ctx.llm)

        # Guardrail applied to both paths (5484).
        entries = toc_module.deduplicate_toc(entries)

        with fitz.open(ws.pdf_path) as document:
            metadata_title = (document.metadata.get("title", "") or "").strip()
            total_pages = len(document)

        ctx.progress("Identifying the standard", pct=26)
        # Ask a Source first, then the PDF's own metadata, then the filename (5492-5499).
        rag_title = rag.get_iso_number(
            ctx.storage.read_input(run),
            source_name,
            llm=ctx.llm,
            progress=ws.progress,
        )

    doc_title = rag_title or metadata_title or os.path.splitext(source_name)[0]
    logger.info(
        "run %s: outline has %d section(s), title=%r", run.id, len(entries), doc_title
    )
    return {"toc": entries, "doc_title": doc_title, "total_pages": total_pages}


def await_range(run, ctx):
    """Park until the user picks a section range.

    The wait that used to sit between two HTTP requests, now persisted: the worker is
    released and `router.py` completes this stage's checkpoint.
    """
    entries = (ctx.checkpoint("outline") or {}).get("toc") or []
    ctx.progress(f"{RANGE_PROMPT} — {len(entries)} found", pct=30)
    return ctx.pause_for_user(RANGE_PROMPT)


def build(run, ctx):
    """Everything `POST /generate` did after validating its arguments (5544-5580).

    Media pre-scan and document assembly stay in **one** stage on purpose: the ported
    code writes some crops to local scratch without a durable copy and reads them back
    by path, so splitting them across stages would leave dead paths.
    """
    from . import generate

    ingested = ctx.checkpoint("ingest") or {}
    outlined = ctx.checkpoint("outline") or {}
    chosen = ctx.checkpoint("await_range") or {}

    with workspace.open_run(run, ctx) as ws:
        ctx.progress("Generating the document", pct=35)
        result = generate.build_document(
            ctx,
            ws,
            toc=outlined.get("toc") or [],
            # The RAG-derived title names the FILE. The title printed inside the
            # document is derived independently from the PDF's own metadata
            # (backend.py:5166 and 5252); conflating the two is silent output drift.
            doc_title=outlined.get("doc_title") or "",
            start_idx=int(chosen.get("start_idx", 0)),
            end_idx=int(chosen.get("end_idx", 0)),
            use_vision=bool(ingested.get("use_vision")),
            llm_pages=_load_llm_pages(ctx, ingested),
            total_pages=int(
                outlined.get("total_pages") or ingested.get("total_pages") or 0
            ),
        )

        document_bytes = result["docx"]
        ctx.storage.attach_output(
            run,
            result["filename"],
            document_bytes,
            result.get("content_type") or DOCX_CONTENT_TYPE,
        )

    ctx.progress("Document ready", pct=100)
    return {"bytes": len(document_bytes)}
