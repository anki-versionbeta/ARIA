"""Configuration for PSA when it is running as an ARIA silo.

The ARIA host: the only module here that knows ARIA exists. It imports `da_platform`, so it is imported
LAZILY from inside a stage — importing it outside ARIA raises, and the registry must still be able to
discover the silo (`silo.py` says the same about its own pipeline imports).

There is a precedent for a non-router silo module reaching for a platform provider:
`silos/mfg_atr/archive.py:48` imports `da_platform.storage.get_object_store` inside a function.

**Why the credential cannot come from anywhere else.** PSA reads Smartsheet — that is the whole silo — and
a silo may not read its process environment. `ctx` carries a ready-made client for every source ARIA
already talks to (`ctx.llm`, `ctx.warehouse`, `ctx.storage`, `ctx.textract`) and no silo anywhere obtains a
credential itself. So the Smartsheet token needs the same home the warehouse DSN has: the platform's
`credentials.py`, whose `ctx.warehouse` docstring states the identical case — "A silo may not open that
connection itself ... so it arrives here."

That is the ONE additive change PSA needs in the platform:

    da_platform/credentials.py  +=  SmartsheetCredentials  +  smartsheet_credentials()

Everything else about the port is two new folders.

**Where psa.db goes.** A per-process scratch directory, not a durable location. It is a derived cache that
each run recreates from its own stored snapshot (`psa/snapshot.py`), so it needs to be writable and
nothing more; the durable artefacts — the snapshot, the product photo, the generated report — go to the
object store through `ctx.storage`. That is also why `storage_dir` is left at its default and ignored:
`storage.get_object_store()` is repointed at the platform's store at Stage C.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import threading

from .config import PsaConfig, config, defaults, set_config
from .config import installed as config_installed

_LOCK = threading.Lock()
_SCRATCH: str | None = None
# No "already installed" flag: `install()` keys idempotence on the installed config itself, so a lenient
# install cannot mask a later strict one. A boolean here would have to be kept in sync with that and would
# be the only place the two could disagree.


def _scratch_dir() -> str:
    """One writable directory per process for the derived database and the generated .docx.

    Per process rather than per run: the worker serialises PSA's stages on `engines.RUN_LOCK`, and each
    stage rebuilds the database from its own run's snapshot before reading it, so two runs cannot see each
    other's rows. A second worker process gets its own directory.
    """
    global _SCRATCH
    with _LOCK:
        if _SCRATCH is None:
            _SCRATCH = tempfile.mkdtemp(prefix="psa-run-")
        return _SCRATCH


def build(*, require_credentials: bool = True) -> PsaConfig:
    """A config for the ARIA host: platform credentials, scratch paths, live ingest.

    `require_credentials=False` keeps the scratch PATHS — which are the part that must never be wrong —
    while tolerating an absent token. That distinction matters because the two callers want opposite
    things from a missing credential:

    * a **stage** must fail loudly and early. `fetch` exists to read the Smartsheet; running it without a
      token can only produce a broken run, so `MissingCredential` is allowed to propagate.
    * the **router** must still answer. `GET /health` reporting `smartsheet_live: false` is exactly how an
      operator diagnoses the missing token, and `GET /palette` never touches Smartsheet at all — a 500
      from either would hide the problem instead of showing it.

    What is NOT negotiable either way is the paths: `defaults()` is repo-relative, and inside ARIA that
    means `da-backend/silos/`, so a lenient build must still redirect writes to the scratch directory.
    """
    from api.backend.da_platform.credentials import MissingCredential, smartsheet_credentials

    try:
        creds = smartsheet_credentials()
        token, sheet_id = creds.token, creds.sheet_id
    except MissingCredential:
        if require_credentials:
            raise
        token, sheet_id = None, None

    scratch = _scratch_dir()
    return dataclasses.replace(
        defaults(),
        work_dir=scratch,
        db_path=os.path.join(scratch, "psa.db"),
        output_dir=os.path.join(scratch, "output"),
        uploads_dir=os.path.join(scratch, "uploads"),
        # 'api' even in the lenient case: the .xlsx fallback lives under the repo root, which does not
        # exist in a deployed silo, so pretending it might is worse than reporting no live source.
        ingest_source="api",
        smartsheet_token=REDACTED
        smartsheet_sheet_id=sheet_id,
    )


def install(*, require_credentials: bool = True) -> PsaConfig:
    """Install the ARIA-hosted config once per process. Idempotent; safe at the top of every stage.

    Idempotence is keyed on whether the installed config actually HAS credentials, not on a plain "have I
    run" flag. Otherwise an earlier lenient install (from a router request) would satisfy a later strict
    one (from a stage) and the stage would run tokenless against a cached config — the failure being
    silent, and in the one place that must be loud.
    """
    current = config() if config_installed() else None
    if current is not None and (current.smartsheet_token or not require_credentials):
        return current
    cfg = build(require_credentials=require_credentials)
    set_config(cfg)
    return cfg
