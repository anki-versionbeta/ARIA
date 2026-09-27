"""Configuration for the PSA silo, resolved at CALL time.

An ARIA silo may not read its process environment. `tests/test_silo_isolation.py` fails the build on any
silo line that even mentions the reader — it is a line-based scan that skips only `#` comment lines —
because, in the words of `da_platform/engine/context.py`, "a silo never constructs a credential, names a
bucket or picks a rate limit, so those exist in one place". Configuration arrives instead.

So this module holds the SHAPE of PSA's configuration and a set of repo-relative defaults, and nothing
else. Whoever is hosting PSA supplies the rest through `set_config()`:

    aria.py                  inside ARIA — builds one from the platform's own credential module
                             (`da_platform.credentials.smartsheet_credentials`)
    tests/conftest.py        a test that needs writable paths injects its own with `set_config`

The defaults are repo-relative and therefore WRONG in a deployed silo, which is what `installed()` exists
to let a host detect — see its docstring.

Nothing is captured at import time, which is the other half of the point. The old code read the
environment while this module was being imported, so a host had to set every `PSA_*` variable before its
first pipeline import, and `cap_colors.PALETTE_CSV` could be rebound at runtime behind a caller's back —
the root cause of the palette-edit bug fixed on 2026-08-20. Resolving on each call removes both.

Defaults, when nothing is injected: the repo layout. `Database/psa.db`, `Output_Files/`,
`Input_Data_Sources/uploads`, `.psa_store/`, no Smartsheet credentials, auto-detected ingest source.

Two variables the standalone host still honours were DROPPED rather than carried over, and are named here
instead of quietly removed:

  * ``PSA_CAP_PALETTE`` pointed at a writable copy of `cap_palette.csv`, because the DSS code directory
    was read-only. Superseded by the palette override object (`psa/palette.py`).
  * ``PSA_MULTIUSER`` / ``PSA_SESSION_ID`` gave each DSS session its own ``psa_<sid>.db``. Under ARIA each
    run rebuilds from its own stored snapshot, which is a better answer to the same problem.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

# psa/ ; the repo root is its parent. Both are read-only in a deployed environment.
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PACKAGE_DIR, "assets")
_REPO_ROOT = os.path.abspath(os.path.join(PACKAGE_DIR, os.pardir))


@dataclass(frozen=True)
class PsaConfig:
    """Where this process reads and writes, and what it may talk to.

    Frozen: a caller that wants different values builds a new one and hands it to `set_config()`,
    rather than mutating shared state — the failure mode the old module-level globals had.
    """

    root: str
    """Base for INPUT data that ships with the repo (`Input_Data_Sources/...`). Read-only."""

    work_dir: str
    db_path: str
    output_dir: str
    uploads_dir: str
    storage_dir: str
    """Object-store root. Holds the palette override; replaced by `ctx.storage` in ARIA."""

    ingest_source: str
    """'api' | 'xlsx' | '' (auto-detect)."""

    smartsheet_token: str | None
    smartsheet_sheet_id: str | None

    def __repr__(self) -> str:
        """Never print the token.

        The default dataclass repr would, and a `PsaConfig` reaches a traceback easily — it is an
        argument to `set_config` and a local in several call chains. The token authenticates as a real
        person and can read every sheet they can see, so a token in a log is a reportable incident.
        `da_platform.credentials.WarehouseCredentials` redacts for exactly this reason, and
        `SmartsheetCredentials` — the object this one is built FROM inside ARIA — does too; without this
        the value would be laundered straight back into the open one layer later.
        """
        return (
            f"PsaConfig(work_dir={self.work_dir!r}, db_path={self.db_path!r}, "
            f"output_dir={self.output_dir!r}, storage_dir={self.storage_dir!r}, "
            f"ingest_source={self.ingest_source!r}, sheet_id={self.smartsheet_sheet_id!r}, "
            f"token={'<redacted>' if self.smartsheet_token else None})"
        )

    @property
    def image_dir(self) -> str:
        """Product images pulled from Smartsheet, alongside the generated reports."""
        return os.path.join(self.output_dir, "assets")

    @property
    def smartsheet_live(self) -> bool:
        """True when the live Smartsheet is configured; False means the stale .xlsx export."""
        if self.ingest_source == "xlsx":
            return False
        return bool(self.smartsheet_token and self.smartsheet_sheet_id)


def defaults(work_dir: str | None = None) -> PsaConfig:
    """The repo layout, with no credentials. What `config()` answers when nothing is injected.

    A host that wants anything else builds on this — `dataclasses.replace(defaults(), ...)` — rather than
    reconstructing the whole dataclass, so a new field does not have to be added in three places.
    """
    work = work_dir or _REPO_ROOT
    return PsaConfig(
        root=_REPO_ROOT,
        work_dir=work,
        db_path=os.path.join(_REPO_ROOT, "Database", "psa.db"),
        output_dir=os.path.join(work, "Output_Files"),
        uploads_dir=os.path.join(work, "Input_Data_Sources", "uploads"),
        storage_dir=os.path.join(work, ".psa_store"),
        ingest_source="",
        smartsheet_token=REDACTED
        smartsheet_sheet_id=None,
    )


_LOCK = threading.Lock()
_INJECTED: PsaConfig | None = None


def config() -> PsaConfig:
    """The active configuration.

    Deliberately NOT cached: rebuilding the defaults costs a handful of `os.path.join` calls, and a cache
    would reintroduce the import-ordering trap this module exists to remove (a value read before the
    caller had finished configuring the process would win for the lifetime of the process).
    """
    with _LOCK:
        if _INJECTED is not None:
            return _INJECTED
    return defaults()


def set_config(cfg: PsaConfig | None) -> None:
    """Install an explicit configuration — the two hosts named in the module docstring.

    `set_config(None)` restores the built-in defaults — NOT the environment. Only a host reads the
    environment, and only by installing a config built from it.
    """
    global _INJECTED
    with _LOCK:
        _INJECTED = cfg


def installed() -> bool:
    """True when a host has installed a configuration, so `config()` is not answering with `defaults()`.

    Exists because the defaults are repo-relative and therefore WRONG in a deployed silo: `_REPO_ROOT` is
    the parent of this package, which inside ARIA is `da-backend/silos/`. A caller that has not been
    configured would quietly create `da-backend/silos/Database/psa.db` in the source tree. `router.py`
    uses this to install the ARIA config on the first request it serves.
    """
    with _LOCK:
        return _INJECTED is not None
