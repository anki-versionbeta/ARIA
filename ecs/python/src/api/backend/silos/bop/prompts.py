"""Prompt loading, with BOP's override precedence preserved exactly.

`memry/petra_prompt.json` wins over `prompts/petra.txt`, matching `load_petra_raw()`
in the app being replaced. That override object exists in S3 today, so reading the
default instead would silently change what the model is told.

The override is **global** — one person's edit changes everyone's output. That is
current behaviour and is kept deliberately. Spec D17 wanted per-user overrides for
exactly that reason, and `silo_prompt_overrides` is ready if that is ever adopted.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PETRA_DEFAULT = "prompts/petra.txt"
REVIEWER_DEFAULT = "prompts/reviewer_default.txt"
PETRA_OVERRIDE = "memry/petra_prompt.json"
REVIEWER_OVERRIDE = "memry/reviewer_prompt.json"


def _load_override(ctx, relative: str) -> str | None:
    """Read `{"prompt": "..."}` if the override object exists."""
    try:
        if not ctx.assets.exists(relative):
            return None
        payload = json.loads(ctx.assets.read_text(relative, cache=False))
        prompt = payload.get("prompt")
        return prompt if isinstance(prompt, str) and prompt.strip() else None
    except Exception as exc:
        # A malformed override must not stop generation; fall back to the default.
        logger.warning("Ignoring unreadable override %s: %s", relative, exc)
        return None


def load_petra(ctx) -> str:
    override = _load_override(ctx, PETRA_OVERRIDE)
    if override is not None:
        logger.info("Using the saved PETRA override (%d chars)", len(override))
        return override
    return ctx.assets.read_text(PETRA_DEFAULT)


def load_petra_default(ctx) -> str:
    return ctx.assets.read_text(PETRA_DEFAULT)


def load_reviewer(ctx) -> str:
    override = _load_override(ctx, REVIEWER_OVERRIDE)
    if override is not None:
        logger.info("Using the saved reviewer override (%d chars)", len(override))
        return override
    return ctx.assets.read_text(REVIEWER_DEFAULT)


def load_reviewer_default(ctx) -> str:
    return ctx.assets.read_text(REVIEWER_DEFAULT)
