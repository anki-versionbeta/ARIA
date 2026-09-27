"""Few-shot examples taken from the SOP template.

The template already contains human-authored content for each section. Feeding that
back to the model is what sets the register — without it, output drifts longer and
more verbose than the template it is meant to match.

Ported verbatim. Reuses the same docx walker as manual extraction, and falls back to
an empty mapping on any failure so generation still runs (just without few-shot).
"""

from __future__ import annotations

import logging

from . import ingest

logger = logging.getLogger(__name__)

TEMPLATE_ASSET = "templates/SOP template.docx"

# The template's H1 heading (upper-cased, stripped) to the BOP JSON top-level key.
# Keep in sync with SECTION_SCHEMAS.
TEMPLATE_H1_TO_KEY: dict[str, str] = {
    "PURPOSE": "purpose",
    "SCOPE": "scope",
    "DESCRIPTION": "description",
    "SAFETY": "safety",
    "OPERATING PROCEDURE": "operating_procedure",
    "ABBREVIATIONS AND DEFINITIONS": "abbreviations",
    "RELATED DOCUMENTS": "related_documents",
}

_cache: dict[str, str] | None = None


def _build(ctx) -> dict[str, str]:
    try:
        parsed = ingest.extract_from_docx(ctx.assets.read(TEMPLATE_ASSET))
    except Exception as exc:
        logger.warning("Could not parse the SOP template for examples: %s", exc)
        return {}

    examples: dict[str, str] = {}
    for section in parsed.get("sections", []):
        heading = (section.get("heading") or "").strip().upper()
        key = TEMPLATE_H1_TO_KEY.get(heading)
        if not key:
            continue

        lines: list[str] = []
        for line in section.get("content") or []:
            text = (line or "").strip()
            if text:
                lines.append(text)
        for sub in section.get("subsections") or []:
            sub_heading = (sub.get("heading") or "").strip()
            prefix = "##" if (sub.get("level") or 2) <= 2 else "###"
            if sub_heading:
                lines.append(f"{prefix} {sub_heading}")
            for line in sub.get("content") or []:
                text = (line or "").strip()
                if text:
                    lines.append(f"  {text}")

        body = "\n".join(lines).strip()
        if body:
            examples[key] = body
    return examples


def get_template_examples(ctx) -> dict[str, str]:
    """Lazy and cached per process: the template is read once, not once per call."""
    global _cache
    if _cache is None:
        _cache = _build(ctx)
        logger.info(
            "Loaded %d template example(s): %s", len(_cache), sorted(_cache)
        )
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
