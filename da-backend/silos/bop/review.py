"""The reviewer pass: a single critique call over the generated document.

Ported verbatim, including the 8000-character manual excerpt, the 2000-token budget
and the fail-soft behaviour — a reviewer that errors or returns an unexpected shape
reports "ok" rather than blocking the run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import prompts
from .generate import parse_json_response

logger = logging.getLogger(__name__)

MANUAL_EXCERPT_CHARS = 8000
REVIEW_MAX_TOKENS = REDACTED


def review(ctx, manual_text: str, bop_json: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "manual_excerpt": manual_text[:MANUAL_EXCERPT_CHARS],
            "generated": bop_json,
        },
        ensure_ascii=False,
    )
    try:
        response = ctx.llm.chat(
            system=prompts.load_reviewer(ctx),
            user=payload,
            max_tokens=REDACTED
        )
        parsed = parse_json_response(response)
        if isinstance(parsed, dict) and "status" in parsed:
            parsed.setdefault("issues", [])
            return parsed
        logger.info("Reviewer returned an unexpected shape; treating as ok")
    except Exception as exc:
        # The review is advisory. Failing it must not fail the document.
        logger.warning("Reviewer call failed (%s); treating as ok", exc)
    return {"status": "ok", "issues": []}
