from __future__ import annotations

from fastapi import APIRouter

from da_platform.api.schemas import SiloOut
from da_platform.auth.access import accessible_module_ids
from da_platform.auth.deps import CurrentUser, DbSession
from da_platform.silo_registry import discover_silos

router = APIRouter(tags=["silos"])


@router.get("/silos", response_model=list[SiloOut])
def list_silos(user: CurrentUser, session: DbSession) -> list[SiloOut]:
    """Drives the document-type dropdown (D1), narrowed to the caller's modules.

    Supersedes D12 ("every silo is visible to every authenticated user"): module access is
    per-user now, so this returns only what the caller holds. Admins and super users hold
    every module by role, so nothing changes for them. A user with no grants gets an empty
    list, and the UI shows an empty state rather than a dropdown that leads nowhere.

    Filtering here rather than in the frontend is deliberate: this response is the only
    thing telling the UI which silos exist, so a module the caller cannot use never
    reaches the browser at all.
    """
    allowed = accessible_module_ids(session, user)
    return [
        SiloOut(
            id=silo.id,
            label=silo.label,
            accepts=list(silo.accepts),
            stages=list(silo.stages),
        )
        for silo in discover_silos()
        if silo.id in allowed
    ]
