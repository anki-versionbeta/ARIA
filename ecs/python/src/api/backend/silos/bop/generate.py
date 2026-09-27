"""Section-by-section generation: one LLM call per section, run concurrently.

Every call gets the full PETRA prompt and the full (capped) manual, so nothing is
lost by splitting. Sections are independent, so wall-clock time is max-of-sections
rather than sum-of-sections.

Ported verbatim, including the constants — 14 tasks, 7 workers, the 4000/16000 token
budgets, the truncation heuristic and the per-section empty fallbacks.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from . import examples, gold, prompts
from .schema import (
    OP_PHASE_EMPTY,
    OP_PHASE_LABELS,
    OP_PHASE_SCHEMAS,
    SECTION_EMPTY,
    SECTION_SCHEMAS,
    SECTION_STYLE_NOTES,
)

logger = logging.getLogger(__name__)

MAX_MANUAL_CHARS = 180_000  # keep well under the model's input context
SECTION_MAX_TOKENS = REDACTED
# Retry budget when a section looks truncated. Gold-calibrated operating_procedure
# can run 6-8k tokens of JSON, and 8000 sometimes truncated mid-JSON.
LARGE_SECTION_MAX_TOKENS = REDACTED
GENERATE_MAX_WORKERS = 7

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_json_response(text: str) -> dict | list:
    """Tolerant JSON parser that strips ```json fences."""
    if text is None:
        raise ValueError("parse_json_response: text is None")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    if not stripped:
        raise ValueError("parse_json_response: empty after stripping")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = min(
            (index for index in (stripped.find("{"), stripped.find("[")) if index >= 0),
            default=-1,
        )
        end = max(stripped.rfind("}"), stripped.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def looks_truncated(text: str) -> bool:
    """A complete JSON response should end with `}` or `]`."""
    if not text:
        return True
    return text.rstrip()[-1:] not in ("}", "]")


def _section_system_prompt(ctx, petra_raw: str, section_key: str, schema: str) -> str:
    """PETRA + template few-shot + length/style bound + style note + JSON footer."""
    parts: list[str] = [petra_raw.rstrip()]

    example = (examples.get_template_examples(ctx).get(section_key) or "").strip()
    if example:
        parts.append(
            "\n\nFor reference, here is the BOP template's existing example "
            "content for this section. Match this register and length closely. "
            'The "…" markers in the example are PLACEHOLDERS for the actual '
            "system name, site, value, etc. — substitute with concrete details "
            'from the source manual. Do NOT echo the "…" character.\n'
            "--- TEMPLATE EXAMPLE ---\n"
            f"{example}\n"
            "--- END TEMPLATE EXAMPLE ---"
        )

    bound = gold.get_section_bounds(ctx).get(section_key, "").strip()
    if bound:
        parts.append(
            "\n\nLENGTH AND STYLE RULES for this section (treat as hard "
            f"constraints):\n{bound}"
        )

    style_note = SECTION_STYLE_NOTES.get(section_key, "").strip()
    if style_note:
        parts.append(
            "\n\nSTRUCTURE AND STYLE for this section (authoritative — follow "
            "this exactly, overriding any differing template example above):\n"
            f"{style_note}"
        )

    parts.append(
        f'\n\nFor THIS call, generate ONLY the "{section_key}" section of '
        "the BOP JSON. Return a single JSON object with exactly one top-level "
        "key, matching the shape below. No prose outside the JSON, no "
        "markdown fences.\n\n"
        f"{{\n  {schema}\n}}\n\n"
        "If the source manual lacks information for this section, return "
        "empty strings or empty lists — do NOT fabricate."
    )
    return "".join(parts)


def _op_phase_system_prompt(ctx, petra_raw: str, phase: str, schema: str) -> str:
    parts: list[str] = [petra_raw.rstrip()]

    example = (
        examples.get_template_examples(ctx).get("operating_procedure") or ""
    ).strip()
    if example:
        parts.append(
            "\n\nFor reference, here is the BOP template's existing example "
            "content for the operating procedure. Match this register and "
            'level of detail. The "…" markers are PLACEHOLDERS — substitute '
            'concrete details from the source manual. Do NOT echo the "…" '
            "character.\n"
            "--- TEMPLATE EXAMPLE ---\n"
            f"{example}\n"
            "--- END TEMPLATE EXAMPLE ---"
        )

    # The equipment/software bound applies to the paired phases only;
    # troubleshooting has a different {issue, resolution} shape.
    bound = gold.get_section_bounds(ctx).get("operating_procedure", "").strip()
    if bound and phase != "troubleshooting":
        parts.append(
            "\n\nLENGTH AND STYLE RULES for this phase (treat as hard "
            f"constraints):\n{bound}"
        )

    label = OP_PHASE_LABELS.get(phase, phase)
    parts.append(
        f'\n\nFor THIS call, generate ONLY the "{phase}" phase '
        f'("{label}") of the BOP operating procedure. Return a single JSON '
        "object with exactly one top-level key, matching the shape below. No "
        "prose outside the JSON, no markdown fences.\n\n"
        f"{{\n  {schema}\n}}\n\n"
        "If the source manual lacks information for this phase, return empty "
        "lists — do NOT fabricate."
    )
    return "".join(parts)


def _unwrap(parsed: Any, key: str) -> Any:
    """Take the value out of `{"<key>": value}`, tolerating a bare value."""
    if isinstance(parsed, dict) and key in parsed:
        return parsed[key]
    if isinstance(parsed, dict) and len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed


def _call_with_truncation_retry(ctx, system: str, user_text: str, label: str) -> str:
    response = ctx.llm.chat(system=system, user=user_text, max_tokens=SECTION_MAX_TOKENS)
    if looks_truncated(response):
        logger.info(
            "%s looks truncated; retrying with max_tokens=%d",
            label,
            LARGE_SECTION_MAX_TOKENS,
        )
        response = ctx.llm.chat(
            system=system, user=user_text, max_tokens=LARGE_SECTION_MAX_TOKENS
        )
    return response


def generate_one_section(ctx, petra_raw: str, user_text: str, key: str, schema: str) -> Any:
    system = _section_system_prompt(ctx, petra_raw, key, schema)
    response = _call_with_truncation_retry(ctx, system, user_text, f"section {key}")
    try:
        return _unwrap(parse_json_response(response), key)
    except Exception as exc:
        # Logged with enough of the response that debugging is not inferential.
        logger.warning(
            "section %s failed to parse (%s); head=%r",
            key,
            exc,
            (response or "")[:400],
        )
        raise


def generate_one_op_phase(ctx, petra_raw: str, user_text: str, phase: str, schema: str) -> Any:
    system = _op_phase_system_prompt(ctx, petra_raw, phase, schema)
    response = _call_with_truncation_retry(ctx, system, user_text, f"phase {phase}")
    try:
        return _unwrap(parse_json_response(response), phase)
    except Exception as exc:
        logger.warning(
            "phase %s failed to parse (%s); head=%r",
            phase,
            exc,
            (response or "")[:400],
        )
        raise


def normalize_op_phase(phase: str, value: Any) -> Any:
    """Coerce one phase result into its canonical shape.

    Paired phases become {"equipment": [...], "software": [...]}; a bare list is
    treated as equipment, a string as one equipment step, anything else as empty.
    """
    if phase == "troubleshooting":
        return value if isinstance(value, list) else []
    if isinstance(value, dict):
        equipment = value.get("equipment") or []
        software = value.get("software") or []
        return {
            "equipment": equipment if isinstance(equipment, list) else [equipment],
            "software": software if isinstance(software, list) else [software],
        }
    if isinstance(value, list):
        return {"equipment": value, "software": []}
    if isinstance(value, str) and value.strip():
        return {"equipment": [value], "software": []}
    return {"equipment": [], "software": []}


def generate(ctx, extracted: dict[str, Any]) -> dict[str, Any]:
    user_text = extracted.get("raw_text", "") or ""
    if len(user_text) > MAX_MANUAL_CHARS:
        logger.info(
            "Manual is %d chars, capping at %d to stay inside the context limit",
            len(user_text),
            MAX_MANUAL_CHARS,
        )
        user_text = user_text[:MAX_MANUAL_CHARS] + "\n\n[MANUAL TRUNCATED]"

    petra_raw = prompts.load_petra(ctx)

    tasks: list[tuple[str, str, str]] = [
        ("section", key, schema) for key, schema in SECTION_SCHEMAS.items()
    ]
    tasks += [("op_phase", phase, schema) for phase, schema in OP_PHASE_SCHEMAS.items()]
    total = len(tasks)
    logger.info(
        "Generating %d task(s) with %d workers (%d chars of manual)",
        total,
        GENERATE_MAX_WORKERS,
        len(user_text),
    )

    completed = 0

    def run(task: tuple[str, str, str]) -> tuple[str, str, Any]:
        kind, key, schema = task
        try:
            if kind == "section":
                value = generate_one_section(ctx, petra_raw, user_text, key, schema)
            else:
                value = generate_one_op_phase(ctx, petra_raw, user_text, key, schema)
            return kind, key, value
        except Exception as exc:
            # One failed section must not lose the other thirteen.
            logger.warning("%s:%s failed (%s); using the empty default", kind, key, exc)
            if kind == "section":
                return kind, key, deepcopy(SECTION_EMPTY[key])
            return kind, key, deepcopy(OP_PHASE_EMPTY[key])

    result: dict[str, Any] = {}
    op_result: dict[str, Any] = {}
    with ThreadPoolExecutor(
        max_workers=GENERATE_MAX_WORKERS, thread_name_prefix="bop-petra"
    ) as pool:
        for kind, key, value in pool.map(run, tasks):
            if kind == "section":
                result[key] = value
            else:
                op_result[key] = normalize_op_phase(key, value)
            completed += 1
            ctx.progress(
                f"Generated {completed} of {total} sections",
                pct=30 + int(45 * completed / total),
            )

    result["operating_procedure"] = op_result
    return result
