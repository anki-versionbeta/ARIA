"""BOP — the silo contract.

Orchestration only. The stages mirror the pipeline of the app being replaced:

    Extracting -> Generating -> Reviewing -> Review (human) -> Building

The human review is a real pause: the run is parked in the database and the worker is
released, rather than a thread waiting on an event for up to an hour.
"""

from __future__ import annotations

import logging
import os

from . import editing, emit, generate as generation, ingest, review as reviewing
from .location import STORAGE_PREFIX  # noqa: F401 — read by the silo registry

LABEL = "Equipment BOP"
ACCEPTS = [".pdf", ".docx"]
STAGES = ["extract", "generate", "review", "await_review", "build"]

# Read by the platform to list sections the way the built document reads, rather than the
# alphabetical order the rows come back in.
SECTION_ORDER = editing.SECTION_ORDER

# The Dataiku managed-folder names this silo already uses, now as S3 folders under
# STORAGE_PREFIX, so one layout serves the old app and this platform:
#   BOP/uploads/<run_id>_manual.pdf
#   BOP/downloads/<run_id>_BOP_manual.docx
#   BOP/memry/<run_id>_extracted.txt
# The run id stays in the name because these folders are shared. The old app wrote the
# bare basename, so two people uploading the same filename overwrote each other; ISO
# already prefixes the id for exactly this reason.
STORAGE_FOLDERS = {
    "input": "uploads",
    "output": "downloads",
    "media": "memry",
}

logger = logging.getLogger(__name__)

# The extracted manual can be 180k characters. It goes to object storage rather than
# into a checkpoint row, for the same reason spec section 10 moves ISO's page images
# there: checkpoints stay small and nothing durable lands on local disk.
EXTRACTED_TEXT_MEDIA = "extracted.txt"


def extract(run, ctx):
    filename = ctx.storage.input_filename(run) or "manual.pdf"
    ctx.progress("Reading the manual", pct=5)
    extracted = ingest.extract(filename, ctx.storage.read_input(run), ctx)

    raw_text = extracted.get("raw_text", "") or ""
    if not raw_text.strip():
        raise ValueError(
            "No text could be extracted from this manual, including by vision OCR"
        )

    key = ctx.storage.put_media(run, EXTRACTED_TEXT_MEDIA, raw_text.encode("utf-8"))
    ctx.progress(f"Extracted {len(raw_text):,} characters", pct=25)
    return {
        "text_key": key,
        "format": extracted.get("format"),
        "chars": len(raw_text),
        "source_name": filename,
    }


def _manual_text(ctx, extract_output: dict) -> str:
    with ctx.storage.open_key(extract_output["text_key"]) as handle:
        return handle.read().decode("utf-8", errors="replace")


def generate(run, ctx):
    extract_output = ctx.checkpoint("extract") or {}
    raw_text = _manual_text(ctx, extract_output)

    ctx.progress("Generating sections", pct=30)
    bop_json = generation.generate(ctx, {"raw_text": raw_text})

    # Written now, so the generated document is durable and reviewable before the
    # user touches anything.
    written = ctx.sections.write(editing.flatten(bop_json))
    logger.info("Stored %d editable section(s) for run %s", written, run.id)

    ctx.progress("Sections generated", pct=75)
    return {"bop_json": bop_json}


def review(run, ctx):
    extract_output = ctx.checkpoint("extract") or {}
    generated = (ctx.checkpoint("generate") or {}).get("bop_json") or {}

    ctx.progress("Reviewing the draft", pct=80)
    outcome = reviewing.review(ctx, _manual_text(ctx, extract_output), generated)

    issues = outcome.get("issues") or []
    logger.info("Reviewer reported status=%s with %d issue(s)", outcome.get("status"), len(issues))
    return outcome


def await_review(run, ctx):
    issues = (ctx.checkpoint("review") or {}).get("issues") or []
    message = (
        f"Ready for your review — {len(issues)} point(s) flagged"
        if issues
        else "Ready for your review"
    )
    ctx.progress(message, pct=85)
    return ctx.pause_for_user(message)


def build(run, ctx):
    extract_output = ctx.checkpoint("extract") or {}
    generated = (ctx.checkpoint("generate") or {}).get("bop_json") or {}

    # Edits win over the generated draft; untouched sections keep their structure.
    document = editing.apply_edits(generated, ctx.sections.rows())

    ctx.progress("Building the document", pct=90)
    template = ctx.assets.read(emit.TEMPLATE_ASSET)
    source_name = extract_output.get("source_name") or "manual"
    docx_bytes = emit.build_docx(document, template, source_name=source_name)

    stem = os.path.splitext(os.path.basename(source_name))[0] or "manual"
    ctx.storage.attach_output(
        run,
        f"BOP_{stem}.docx",
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    ctx.progress("Document ready", pct=100)
    return {"bytes": len(docx_bytes)}
