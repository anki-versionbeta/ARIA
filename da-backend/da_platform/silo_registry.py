"""Silo discovery.

A silo is a folder under `SILOS_DIR` containing **one required file**, `silo.py`:

    LABEL   = "ISO Applicability Assessment"
    ACCEPTS = [".pdf"]
    STAGES  = ["ingest", "await_range", "generate", "build"]

    def ingest(run, ctx): ...       # one callable per name in STAGES

Everything else in the folder is that silo's own business — any modules, any names,
any count. `router.py` is optional and only needed for silo-specific endpoints.

Found by scanning, so adding a silo edits no platform file (spec section 4).
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from da_platform.settings import settings

logger = logging.getLogger(__name__)

CONTRACT_FILE = "silo.py"


class SiloContractError(RuntimeError):
    """A silo folder does not satisfy the contract."""


@dataclass(frozen=True)
class Silo:
    id: str
    label: str
    accepts: list[str]
    stages: list[str]
    module: ModuleType
    # Where this silo's objects live inside the shared bucket. Declared by the silo
    # because the apps being absorbed already own separate locations: BOP uses `BOP/`
    # and ISO uses `ISO_Doc_Generation/`. This is a location, not a credential — the
    # bucket and the credential chain stay with the platform.
    storage_prefix: str = ""
    # Optional {kind: folder} map, so a silo keeps the folder layout it already uses
    # in the bucket (e.g. input -> "uploads", output -> "downloads").
    storage_folders: dict[str, str] = field(default_factory=dict)
    # Optional `router.py` exposing a FastAPI `router`, for endpoints only this silo
    # needs (BOP's prompt config, ISO's TOC range). Discovered separately so silo.py
    # never has to import it.
    router: Any = None

    def stage_callable(self, stage: str):
        function = getattr(self.module, stage, None)
        if not callable(function):
            raise SiloContractError(
                f"Silo {self.id!r} declares stage {stage!r} but defines no such function"
            )
        return function

    def accepts_filename(self, filename: str) -> bool:
        if not self.accepts:
            return True
        lowered = filename.lower()
        return any(lowered.endswith(extension.lower()) for extension in self.accepts)


ROOT_PACKAGE = "da_silos"


def _ensure_package(name: str, path: Path) -> ModuleType:
    """Register a synthetic package so a silo's relative imports resolve.

    A silo is a real package with its own modules (`from . import editing`), so the
    parent packages must exist in `sys.modules` with a `__path__`. Building them here
    rather than putting the silos directory on `sys.path` means `SILOS_DIR` can point
    anywhere — including a test fixture directory — without name collisions.
    """
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = package
    return package


def _load_module(silo_id: str, path: Path, name: str = "silo") -> ModuleType:
    """Import one of a silo's modules by path, once.

    Cached in `sys.modules` because `discover_silos()` runs on every /api/silos
    request; re-importing would repeat module-level work and hand out a fresh module
    object each time, so module state would silently not be shared.
    """
    silo_dir = path.parent
    _ensure_package(ROOT_PACKAGE, silo_dir.parent)
    _ensure_package(f"{ROOT_PACKAGE}.{silo_id}", silo_dir)

    module_name = f"{ROOT_PACKAGE}.{silo_id}.{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SiloContractError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a silo that imports itself does not recurse.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_router(silo_id: str, folder: Path) -> Any:
    """Import the silo's optional `router.py` and return its `router` object."""
    path = folder / "router.py"
    if not path.is_file():
        return None
    try:
        module = _load_module(silo_id, path, name="router")
    except Exception as exc:
        # A broken router must not make the silo itself undiscoverable.
        logger.error("Failed to import %s: %s", path, exc)
        return None
    router = getattr(module, "router", None)
    if router is None:
        logger.error("%s defines no `router`", path)
    return router


def _build(silo_id: str, module: ModuleType) -> Silo:
    missing = [name for name in ("LABEL", "STAGES") if not hasattr(module, name)]
    if missing:
        raise SiloContractError(
            f"Silo {silo_id!r} is missing {', '.join(missing)} in {CONTRACT_FILE}"
        )
    stages = list(module.STAGES)
    if not stages:
        raise SiloContractError(f"Silo {silo_id!r} declares no stages")

    silo = Silo(
        id=silo_id,
        label=str(module.LABEL),
        accepts=[str(item) for item in getattr(module, "ACCEPTS", [])],
        stages=stages,
        module=module,
        storage_prefix=str(getattr(module, "STORAGE_PREFIX", silo_id)),
        storage_folders=dict(getattr(module, "STORAGE_FOLDERS", {}) or {}),
    )
    # Fail at discovery rather than mid-run: a typo in STAGES would otherwise only
    # surface when a user had already waited for the earlier stages.
    for stage in stages:
        silo.stage_callable(stage)
    return silo


def discover_silos() -> list[Silo]:
    root = settings.silos_dir
    if not root.is_dir():
        return []

    found: list[Silo] = []
    for entry in sorted(root.iterdir()):
        contract = entry / CONTRACT_FILE
        if not contract.is_file():
            continue
        try:
            silo = _build(entry.name, _load_module(entry.name, contract))
            found.append(
                replace(silo, router=_load_router(entry.name, entry))
            )
        except Exception as exc:
            # One broken silo must not take the whole API down.
            logger.error("Skipping silo %s: %s", entry.name, exc)
            continue

    # ENABLED_SILOS allows shipping a silo dark.
    if settings.enabled_silos:
        allowed = set(settings.enabled_silos)
        found = [silo for silo in found if silo.id in allowed]

    return found


def get_silo(silo_id: str) -> Silo | None:
    return next((silo for silo in discover_silos() if silo.id == silo_id), None)
