"""BOP's own endpoints: the prompt configuration screen.

Mounted by the platform under `/api/silos/bop`. The override objects are the same S3
objects the old app reads, so a prompt saved here is picked up by both.

The override is **global** — one person's edit changes everyone's generated output.
That is current behaviour, preserved deliberately. Spec D17 wanted per-user overrides
for exactly that reason, and `silo_prompt_overrides` exists if that is ever adopted.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from da_platform.auth.deps import CurrentUser
from da_platform.storage import get_object_store
from da_platform.storage.base import asset_key

from .location import STORAGE_PREFIX
from .prompts import (
    PETRA_DEFAULT,
    PETRA_OVERRIDE,
    REVIEWER_DEFAULT,
    REVIEWER_OVERRIDE,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bop"])


class PromptIn(BaseModel):
    prompt: str


class PromptsOut(BaseModel):
    petra: str
    reviewer: str
    petra_is_override: bool
    reviewer_is_override: bool


def _key(relative: str) -> str:
    return asset_key(STORAGE_PREFIX, relative)


def _read_text(relative: str) -> str:
    with get_object_store().open(_key(relative)) as handle:
        return handle.read().decode("utf-8", errors="replace")


def _read_override(relative: str) -> str | None:
    store = get_object_store()
    if not store.exists(_key(relative)):
        return None
    try:
        payload = json.loads(_read_text(relative))
        prompt = payload.get("prompt")
        return prompt if isinstance(prompt, str) and prompt.strip() else None
    except Exception as exc:
        logger.warning("Ignoring unreadable override %s: %s", relative, exc)
        return None


def _write_override(relative: str, prompt: str) -> None:
    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    get_object_store().put(_key(relative), body)


def _clear_override(relative: str) -> None:
    # Best effort: an already-absent override is not an error.
    get_object_store().delete(_key(relative))


@router.get("/config/prompts", response_model=PromptsOut)
def get_prompts(_user: CurrentUser) -> PromptsOut:
    petra_override = _read_override(PETRA_OVERRIDE)
    reviewer_override = _read_override(REVIEWER_OVERRIDE)
    return PromptsOut(
        petra=petra_override if petra_override is not None else _read_text(PETRA_DEFAULT),
        reviewer=(
            reviewer_override
            if reviewer_override is not None
            else _read_text(REVIEWER_DEFAULT)
        ),
        petra_is_override=petra_override is not None,
        reviewer_is_override=reviewer_override is not None,
    )


@router.post("/config/prompts/petra")
def save_petra(payload: PromptIn, user: CurrentUser) -> dict[str, bool]:
    _write_override(PETRA_OVERRIDE, payload.prompt)
    logger.info("%s saved the PETRA prompt override", user.username)
    return {"ok": True}


@router.post("/config/prompts/petra/reset", response_model=PromptIn)
def reset_petra(user: CurrentUser) -> PromptIn:
    _clear_override(PETRA_OVERRIDE)
    logger.info("%s reset the PETRA prompt to the default", user.username)
    return PromptIn(prompt=_read_text(PETRA_DEFAULT))


@router.post("/config/prompts/reviewer")
def save_reviewer(payload: PromptIn, user: CurrentUser) -> dict[str, bool]:
    _write_override(REVIEWER_OVERRIDE, payload.prompt)
    logger.info("%s saved the reviewer prompt override", user.username)
    return {"ok": True}


@router.post("/config/prompts/reviewer/reset", response_model=PromptIn)
def reset_reviewer(user: CurrentUser) -> PromptIn:
    _clear_override(REVIEWER_OVERRIDE)
    logger.info("%s reset the reviewer prompt to the default", user.username)
    return PromptIn(prompt=_read_text(REVIEWER_DEFAULT))
