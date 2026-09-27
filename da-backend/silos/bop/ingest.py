"""Get usable text out of a manual.

Ported from the BOP app with the client calls swapped for `ctx.llm`. The extraction
logic itself is unchanged — it is the part that took tuning.
"""

from __future__ import annotations

import base64
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Below this many characters, a PDF is treated as scanned and goes to vision OCR.
MIN_PDF_TEXT_THRESHOLD = 500
VISION_PDF_DPI = 150
# Per-run fan-out. The platform's own limiter caps total LLM concurrency, so this
# many threads may queue rather than all being in flight at once.
VISION_MAX_WORKERS = 4

VISION_PAGE_PROMPT = (
    "Transcribe ALL text from this scanned manual page image exactly as it "
    "appears, in natural reading order. Preserve section headings, numbered and "
    "bulleted lists, and tables (render table rows as pipe-delimited cells). Do "
    "not summarize, paraphrase, or invent content. Return ONLY the transcribed "
    "text — no commentary, no markdown fences. If the page has no text, return "
    "an empty string."
)


class NeedsVisionFallback(Exception):
    """A PDF with no usable text layer."""


def _docx_table_data(table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


def extract_from_docx(file_bytes: bytes) -> dict[str, Any]:
    """Walk the body, rebuild the heading hierarchy, flatten to raw text."""
    from docx import Document
    from docx.oxml.ns import qn

    document = Document(io.BytesIO(file_bytes))
    elements: list[dict[str, Any]] = []
    table_index = 0
    for element in document.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            style_name = ""
            properties = element.find(qn("w:pPr"))
            if properties is not None:
                style = properties.find(qn("w:pStyle"))
                if style is not None:
                    style_name = style.get(qn("w:val"), "")
            text = "".join(node.text or "" for node in element.findall(".//" + qn("w:t")))
            elements.append({"type": "paragraph", "style": style_name, "text": text})
        elif tag == "tbl" and table_index < len(document.tables):
            elements.append(
                {"type": "table", "data": _docx_table_data(document.tables[table_index])}
            )
            table_index += 1

    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    current_sub: dict[str, Any] | None = None

    for item in elements:
        if item["type"] == "paragraph":
            style = item["style"].lower().replace(" ", "")
            text = item["text"].strip()
            if not text and "heading" not in style:
                continue
            if "heading1" in style:
                current_section = {
                    "heading": text,
                    "level": 1,
                    "content": [],
                    "tables": [],
                    "subsections": [],
                }
                sections.append(current_section)
                current_sub = None
            elif "heading2" in style or "heading3" in style:
                current_sub = {
                    "heading": text,
                    "level": 3 if "3" in style else 2,
                    "content": [],
                    "tables": [],
                }
                if current_section is not None:
                    current_section["subsections"].append(current_sub)
            else:
                target = current_sub if current_sub is not None else current_section
                if target is not None and text:
                    target["content"].append(text)
        else:
            target = current_sub if current_sub is not None else current_section
            if target is not None:
                target["tables"].append(item["data"])

    return {
        "format": "docx",
        "sections": sections,
        "raw_text": _sections_to_text(sections),
    }


def _sections_to_text(sections: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for section in sections:
        parts.append(f"# {section['heading']}")
        parts.extend(section["content"])
        for table in section["tables"]:
            parts.append("[TABLE]")
            parts.extend(" | ".join(row) for row in table)
            parts.append("[/TABLE]")
        for sub in section.get("subsections", []):
            parts.append("#" * sub["level"] + f" {sub['heading']}")
            parts.extend(sub["content"])
            for table in sub["tables"]:
                parts.append("[TABLE]")
                parts.extend(" | ".join(row) for row in table)
                parts.append("[/TABLE]")
    return "\n".join(parts)


def extract_from_pdf(file_bytes: bytes) -> dict[str, Any]:
    """Text and tables via pdfplumber. Raises `NeedsVisionFallback` if too thin."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text_parts: list[str] = []
        tables: list[list[list[str]]] = []
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            for table in page.extract_tables() or []:
                tables.append(table)

    raw_text = "\n".join(text_parts)
    if len(raw_text.strip()) < MIN_PDF_TEXT_THRESHOLD:
        raise NeedsVisionFallback(
            f"pdfplumber extracted only {len(raw_text.strip())} characters"
        )
    return {"format": "pdf", "raw_text": raw_text, "tables": tables}


def _render_page_b64(file_bytes: bytes, page_index: int, dpi: int = VISION_PDF_DPI) -> str:
    """Render one page to a base64 PNG.

    Reopens the document per call so each worker thread holds its own handle —
    PyMuPDF documents are not thread-safe.
    """
    import fitz

    with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
        page = pdf.load_page(page_index)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return base64.b64encode(pixmap.tobytes("png")).decode()


def extract_from_pdf_vision(file_bytes: bytes, ctx) -> dict[str, Any]:
    """Vision-OCR fallback for scanned PDFs: one image per call, pages in parallel."""
    import fitz

    with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
        page_count = pdf.page_count
    if page_count == 0:
        return {"format": "pdf", "raw_text": "", "tables": []}

    logger.info("Vision OCR over %d page(s) at %d dpi", page_count, VISION_PDF_DPI)

    def transcribe(index: int) -> tuple[int, str]:
        try:
            image = _render_page_b64(file_bytes, index)
            return index, ctx.llm.chat_vision(
                image_b64=image, prompt=VISION_PAGE_PROMPT
            ).strip()
        except Exception as exc:
            # One unreadable page must not lose the rest of the manual.
            logger.warning("Vision OCR failed on page %d: %s", index + 1, exc)
            return index, ""

    parts: list[str] = [""] * page_count
    workers = min(VISION_MAX_WORKERS, page_count)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bop-vision") as pool:
        for index, text in pool.map(transcribe, range(page_count)):
            parts[index] = text
            ctx.progress(
                f"Reading page {sum(1 for part in parts if part)} of {page_count}",
                pct=10 + int(10 * (index + 1) / page_count),
            )

    non_empty = sum(1 for part in parts if part.strip())
    logger.info("Vision OCR produced text for %d/%d pages", non_empty, page_count)
    return {
        "format": "pdf",
        "raw_text": "\n\n".join(part for part in parts if part),
        "tables": [],
    }


def extract(filename: str, file_bytes: bytes, ctx) -> dict[str, Any]:
    """Entry point: pick a strategy from the extension, fall back to vision."""
    if filename.lower().endswith(".docx"):
        return extract_from_docx(file_bytes)
    try:
        return extract_from_pdf(file_bytes)
    except NeedsVisionFallback as exc:
        logger.info("%s; falling back to vision OCR", exc)
        ctx.progress("No text layer found; reading pages as images", pct=10)
        return extract_from_pdf_vision(file_bytes, ctx)
