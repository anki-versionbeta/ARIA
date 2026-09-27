"""Silos must not reach for credentials themselves.

Sharing the LLM client and the AWS session is pointless if a silo can quietly read
`ILIAD_API_KEY` or build its own `boto3` client — the global rate limiter stops being
global and the key exists in two places again. This test runs against whatever silos
happen to be registered, so a silo added later is checked by a test nobody wrote for
it (spec section 13).

It is a source scan rather than a runtime check because the failure it prevents is
someone reintroducing the pattern, not a specific call failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from da_platform.settings import REPO_ROOT, settings

# The real shipped silos, NOT `settings.silos_dir` — tests point that at a fixture
# directory, so using it here would quietly police nothing.
SILOS_DIR = REPO_ROOT / "silos"
FIXTURE_SILOS_DIR = settings.silos_dir

# Each pattern is paired with the capability the silo should use instead.
FORBIDDEN = {
    r"\bos\.environ\b": "receive configuration through the stage context",
    r"\bos\.getenv\b": "receive configuration through the stage context",
    r"\bboto3\b": "use ctx.storage or da_platform.aws.get_clients()",
    r"ILIAD_API_KEY": "use ctx.llm",
    r"ILIAD_USER_TOKEN": "use ctx.llm",
    r"AWS_SECRET_ACCESS_KEY": "use ctx.storage",
    r"iliad-emerging-api": "use ctx.llm",
    r"\bverify\s*=\s*False\b": "TLS verification must stay enabled",
    r"disable_warnings": "TLS warnings must stay enabled",
}


def silo_sources() -> list[Path]:
    """Every silo source file, real and fixture."""
    paths: list[Path] = []
    for root in {SILOS_DIR, FIXTURE_SILOS_DIR}:
        if root.is_dir():
            paths.extend(
                path for path in root.rglob("*.py") if "__pycache__" not in path.parts
            )
    return sorted(paths)


def test_the_scan_actually_finds_silo_files():
    """Guards against the check passing because it looked in the wrong place — the
    failure mode that would make this whole test file worthless."""
    found = silo_sources()
    assert found, "no silo sources found; the scan roots are wrong"
    assert any(path.name == "silo.py" for path in found)


@pytest.mark.parametrize("pattern,guidance", sorted(FORBIDDEN.items()))
def test_no_silo_reads_credentials_directly(pattern: str, guidance: str):
    compiled = re.compile(pattern)
    offenders: list[str] = []

    for path in silo_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if compiled.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert not offenders, (
        f"Silo code must not match {pattern!r} — {guidance}.\n"
        + "\n".join(offenders)
    )
