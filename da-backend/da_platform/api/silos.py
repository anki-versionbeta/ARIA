from __future__ import annotations

from fastapi import APIRouter

from da_platform.api.schemas import SiloOut
from da_platform.auth.deps import CurrentUser
from da_platform.silo_registry import discover_silos

router = APIRouter(tags=["silos"])


@router.get("/silos", response_model=list[SiloOut])
def list_silos(_user: CurrentUser) -> list[SiloOut]:
    """Drives the document-type dropdown (D1). Every silo is visible to every
    authenticated user (D12)."""
    return [
        SiloOut(
            id=silo.id,
            label=silo.label,
            accepts=list(silo.accepts),
            stages=list(silo.stages),
        )
        for silo in discover_silos()
    ]
