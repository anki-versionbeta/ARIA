"""A synthetic silo, used only to prove the engine.

It exists so resume, pausing and crash recovery can be tested without real domain
code in the way. It is a fixture rather than a folder in `silos/`, so nothing fake
ships in the deployed image — tests point `SILOS_DIR` here.

`CALLS` records which stages ran, which is how a test asserts that a completed stage
was skipped rather than repeated.
"""

from __future__ import annotations

LABEL = "Engine Probe"
ACCEPTS = [".txt", ".pdf"]
STAGES = ["prepare", "await_input", "finish"]

# Module-level so a test can inspect it. Reset between tests via reset().
CALLS: list[str] = []

# Test switches.
FAIL_IN: set[str] = set()
BLOCK_IN: dict[str, float] = {}


def reset() -> None:
    CALLS.clear()
    FAIL_IN.clear()
    BLOCK_IN.clear()


def _enter(stage: str) -> None:
    CALLS.append(stage)
    if stage in FAIL_IN:
        raise RuntimeError(f"deliberate failure in {stage}")
    delay = BLOCK_IN.get(stage)
    if delay:
        import time

        time.sleep(delay)


def prepare(run, ctx):
    _enter("prepare")
    ctx.progress("Prepared", pct=30)
    return {"prepared": True, "input_name": ctx.storage.input_filename(run)}


def await_input(run, ctx):
    _enter("await_input")
    return ctx.pause_for_user("Waiting for the reviewer")


def finish(run, ctx):
    _enter("finish")
    answer = ctx.checkpoint("await_input") or {}
    prepared = ctx.checkpoint("prepare") or {}
    ctx.progress("Building output", pct=90)
    body = f"choice={answer.get('choice', 'none')};input={prepared.get('input_name')}"
    ctx.storage.attach_output(run, "probe-output.txt", body.encode("utf-8"), "text/plain")
    return {"built": True}
