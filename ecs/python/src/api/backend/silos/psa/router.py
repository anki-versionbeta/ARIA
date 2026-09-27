"""PSA endpoints — the Cap Colour Selection screen's data contract.

Every path here is relative to the mount prefix, so this router serves `/api/silos/psa/*` from
`dev/main.py` today and the identical paths when ARIA's platform mounts it (the platform discovers
`router.py` inside a silo folder — see `da_platform/silo_registry.py:_load_router`).

`CurrentUser` comes from the platform's own auth dependency; the local `deps.py` stub it replaced
does not travel, and neither does `dev/main.py` or its CORS middleware. No endpoint signature
changed at the port.

Do NOT add a `/documents` route: the platform registers `POST /api/silos/{silo_id}/documents` first and
would shadow it. PSA starts from an identifier rather than an upload, so it has no need for one — the
same reason `mfg_atr` uses `/runs`.

Endpoints are sync `def` on purpose. Every call below is blocking sqlite / Smartsheet / python-docx
work, so Starlette runs them in its threadpool; `async def` would hold the event loop for the whole
run (~0.6-2 s for a Smartsheet re-ingest, longer for a report).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from . import cap_colors, config as psa_config, downloads, engines, palette
from api.backend.da_platform.auth.deps import CurrentUser
from .models import (
    ExportOut,
    HealthOut,
    MessageOut,
    PaletteAddIn,
    PaletteColorOut,
    PaletteOut,
    PaletteRemoveIn,
    PresentationOut,
    ProductByColorOut,
    ProgramOut,
    RecommendIn,
    RecommendOut,
    ReportIn,
    ReportOut,
    SiteCountOut,
)

logger = logging.getLogger(__name__)


def _ensure_configured() -> None:
    """Guarantee a host has configured this process before any endpoint reads a path or a credential.

    Stages call `aria.install()` themselves, but **a stage and an endpoint do not share a process** —
    ARIA's API serves the router while a separate `python -m worker` runs the stages. So nothing was
    configuring the API process, and every endpoint answered from `config.defaults()`, which is
    repo-relative: inside ARIA `_REPO_ROOT` is `da-backend/silos/`, so a single request would have created
    `da-backend/silos/Database/psa.db` **in the source tree** and reported `smartsheet_live: false`
    forever, leaving the Product picker permanently empty.

    Written to be byte-identical in the standalone and ported copies:

    * standalone — `dev/environment.py` (or `tests/conftest.py`) already installed a config, so the first
      branch returns and nothing else here runs.
    * inside ARIA — nothing has, so the ARIA config is installed on the first request. `require_credentials
      =False` because `/health` must be able to REPORT a missing token rather than 500 on it.
    * neither — `da_platform` is absent, the ImportError is swallowed, and the repo defaults stand, which
      is the correct answer for someone importing this router directly.

    Deliberately NOT done at import time: `smartsheet_credentials()` raises when the token is absent, and
    `silo_registry._load_router` *catches* an import error and returns None — so an import-time install
    would make every PSA endpoint silently vanish while `GET /api/silos` still advertised the silo. That is
    the same failure shape the lazy openpyxl import exists to avoid.
    """
    if psa_config.installed():
        return
    from . import aria
    try:
        aria.install(require_credentials=False)
    except ImportError:
        pass


router = APIRouter(tags=["psa"], dependencies=[Depends(_ensure_configured)])

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# --- run endpoints, when there is a platform to run on ------------------------------------------------
# `runs.py` imports `Run`, `DbSession` and the queue, so it exists only under ARIA. Including it here
# rather than registering it separately keeps the mount prefix and the config dependency identical for
# every PSA path. Standalone, the three run routes are simply absent — and that is the honest answer,
# because a run needs a platform to be queued on. The screen falls back to the instant preview, which is
# what it uses locally anyway.
#
# The `except ImportError` is narrow on purpose: it must swallow "no da_platform", not a genuine error
# inside runs.py. Anything else propagates, because `silo_registry._load_router` catches import failures
# and returns None — so a real bug hidden here would make EVERY PSA endpoint vanish silently, which is the
# failure shape the lazy openpyxl import exists to avoid.
try:
    from .runs import router as _runs_router
except ImportError:                       # pragma: no cover — exercised by the standalone suite itself
    logger.debug("psa: run endpoints not mounted (no da_platform); the instant preview still works")
else:
    router.include_router(_runs_router)


@router.get("/health", response_model=HealthOut)
def health(_user: CurrentUser) -> HealthOut:
    """Liveness plus the two facts that change how every other answer should be read: whether the data
    is live Smartsheet or the stale .xlsx export, and whether the palette carries local edits."""
    state = palette.load()
    return HealthOut(
        ok=True,
        smartsheet_live=engines.smartsheet_live(),
        db_built=engines.db_exists(),
        palette_is_override=state.is_override,
    )


@router.post("/refresh", response_model=MessageOut)
def refresh(_user: CurrentUser) -> MessageOut:
    """Rebuild psa.db from the live Smartsheet (images skipped — the screen does not show them)."""
    ok, message = engines.refresh(skip_images=True)
    return MessageOut(ok=ok, message=message)


# ---------------------------------------------------------------- pickers
@router.get("/programs", response_model=list[ProgramOut])
def list_programs(_user: CurrentUser) -> list[ProgramOut]:
    """In-scope products. Clinical-stage presentations are excluded by `scope`."""
    return [ProgramOut(**p) for p in engines.programs()]


@router.get("/presentations", response_model=list[PresentationOut])
def list_presentations(_user: CurrentUser,
                       program: str = Query(..., description="Program code, e.g. AGN-151586")
                       ) -> list[PresentationOut]:
    """A product's in-scope presentations, each keyed by the Smartsheet `source_row`."""
    return [PresentationOut(**p) for p in engines.presentations(program)]


# ---------------------------------------------------------------- palette
def _palette_out(row: dict) -> PaletteColorOut:
    """Project a palette row onto the response model, dropping `cap_colors`' internal `_code_norm`."""
    return PaletteColorOut(
        vendor=row.get("vendor") or "",
        vendor_color_name=row.get("vendor_color_name") or "",
        vendor_code=row.get("vendor_code") or "",
        canonical_color=row.get("canonical_color") or "",
        hue_group=row.get("hue_group") or "",
        hex=row.get("hex") or "",
        sizes_mm=row.get("sizes_mm") or "",
        sizes=list(row.get("sizes") or []),
        finish=row.get("finish") or "",
        off_the_shelf=bool(row.get("off_the_shelf")),
        component=row.get("component") or "pp_disc",
        notes=row.get("notes") or "",
    )


@router.get("/palette", response_model=PaletteOut)
def get_palette(_user: CurrentUser,
                vendor: str = Query("Datwyler", description="Seal manufacturer: Datwyler or West"),
                off_the_shelf_only: bool = True) -> PaletteOut:
    """The seal manufacturer's cap-colour palette (PP discs), as shown in the swatch grid.

    `is_override` and the two counts describe the WHOLE palette, not just the vendor asked for: they
    answer "has this catalogue been edited", which is a property of the catalogue.
    """
    state = palette.load()
    rows = cap_colors.palette(vendor, component="pp_disc", off_the_shelf_only=off_the_shelf_only)
    return PaletteOut(colors=[_palette_out(r) for r in rows], is_override=state.is_override,
                      added=state.added, removed=state.removed,
                      override_invalid=state.override_invalid)


@router.post("/palette", response_model=MessageOut)
def add_palette_color(payload: PaletteAddIn, user: CurrentUser) -> MessageOut:
    """Add (or update) a colour in the supplier palette.

    Palette-only: this changes the list the tool recommends FROM and never touches any product's
    assigned `cap_color`, nor the shipped catalogue. Re-adding an existing (vendor, name) updates it.
    """
    ok, message = cap_colors.add_color(
        payload.vendor, payload.vendor_color_name, payload.canonical_color,
        hex=payload.hex, vendor_code=payload.vendor_code, hue_group=payload.hue_group,
        sizes_mm=payload.sizes_mm, finish=payload.finish,
        off_the_shelf=1 if payload.off_the_shelf else 0,
        component=payload.component, notes=payload.notes)
    if ok:
        # The override is global, so every mutation names who made it — as ARIA's prompt editor does.
        logger.info("%s added cap palette colour %s %r",
                    user.username, payload.vendor, payload.vendor_color_name)
    return MessageOut(ok=ok, message=message)


def _remove_palette_color(vendor: str, vendor_color_name: str, username: str) -> MessageOut:
    ok, message = cap_colors.remove_color(vendor, vendor_color_name)
    if ok:
        logger.info("%s removed cap palette colour %s %r", username, vendor, vendor_color_name)
    return MessageOut(ok=ok, message=message)


@router.delete("/palette", response_model=MessageOut)
def remove_palette_color(user: CurrentUser,
                         vendor: str = Query(..., description="Seal manufacturer"),
                         vendor_color_name: str = Query(..., description="Exact palette colour name")
                         ) -> MessageOut:
    """Remove a colour from the supplier palette. Palette-only, as with the add."""
    return _remove_palette_color(vendor, vendor_color_name, user.username)


@router.post("/palette/remove", response_model=MessageOut)
def remove_palette_color_post(payload: PaletteRemoveIn, user: CurrentUser) -> MessageOut:
    """POST alias for the delete above — same engine call, same answer.

    It exists because ARIA's shared HTTP client exports only `get`/`post`/`put`
    (`da-frontend/src/api/http.ts`); issuing a DELETE from the React feature would mean editing an
    ARIA file, which is out of scope. The DELETE stays for CLI/OpenAPI use.
    """
    return _remove_palette_color(payload.vendor, payload.vendor_color_name, user.username)


# ---------------------------------------------------------------- tables
@router.get("/products-by-color", response_model=list[ProductByColorOut])
def products_by_color(_user: CurrentUser,
                      vendor: str = Query(..., description="Seal manufacturer"),
                      color: str = Query(..., description="Exact palette colour name, e.g. 'Blue 6043'")
                      ) -> list[ProductByColorOut]:
    """In-scope products using THAT exact palette colour.

    Coded colours match on the vendor code, so 'Blue 6043' does not pull in every blue.
    """
    return [ProductByColorOut(**r) for r in engines.products_by_color(vendor, color)]


@router.get("/site-product-counts", response_model=list[SiteCountOut])
def site_product_counts(_user: CurrentUser) -> list[SiteCountOut]:
    """In-scope product count per manufacturing site — the bar chart's numbers."""
    return [SiteCountOut(**r) for r in engines.site_product_counts()]


# ---------------------------------------------------------------- recommendation
@router.post("/recommend", response_model=RecommendOut)
def recommend(payload: RecommendIn, _user: CurrentUser) -> RecommendOut:
    """Recommend cap colours for one presentation, across EVERY supplier.

    Re-ingests the live Smartsheet first, so a click always reflects the current sheet — the same
    per-click freshness the screen has always had. Computed program-wide so `first_unique` is real.

    `vendor` is an optional restriction and the screen leaves it unset: the best colour names its own
    supplier, because at assessment time the cap is often not yet tooled (see `RecommendIn.vendor`). Each
    entry in `recommended` therefore carries its own `vendor`, and the same hue can appear twice — once per
    supplier — which is what lets an assessor compare the two catalogues' takes on it.
    """
    res, program_label, presentation_label = engines.recommendation(
        payload.program, payload.source_row, payload.vendor)
    if res is None:
        return RecommendOut(
            error=("That presentation is not in the analysis database yet — refresh from Smartsheet, "
                   "then try again."),
            program_label=program_label, presentation_label=presentation_label)
    return RecommendOut(**res, program_label=program_label,
                        presentation_label=presentation_label)


@router.post("/cap-export", response_model=ExportOut)
def cap_export(payload: RecommendIn, _user: CurrentUser) -> ExportOut:
    """Export the recommendation to a .docx mirroring the on-screen view.

    Recomputed through the same path the screen uses, so the file cannot disagree with the display.
    """
    path, message = engines.export_recommendation(
        payload.program, payload.source_row, payload.vendor)
    if path is None:
        return ExportOut(ok=False, message=message)
    return ExportOut(ok=True, message=message, download_id=downloads.register(path),
                     filename=os.path.basename(path))


# ---------------------------------------------------------------- PSA report
@router.post("/report", response_model=ReportOut)
def generate_report(payload: ReportIn, _user: CurrentUser) -> ReportOut:
    """Generate the PSA similarity report for the selected product + presentation.

    Fully automated — no upload. `status` is 'ok', 'message' (a validation answer) or
    'needs_override'; `verify_ok` reports the post-generation checks, and a false value still leaves a
    downloadable report whose run log explains what failed.
    """
    result = dict(engines.generate_report(payload.program, payload.presentation))
    # The engine returns an absolute path; swap it for an opaque id (see downloads.py).
    path = result.pop("output_path", None)
    if result.get("output_exists") and path:
        result["download_id"] = downloads.register(path)
        result["filename"] = os.path.basename(path)
    return ReportOut(**result)


# ---------------------------------------------------------------- downloads
@router.get("/download/{download_id}")
def download(download_id: str, _user: CurrentUser) -> FileResponse:
    """Fetch a generated .docx by the id returned with it. Serves both report and cap-export files."""
    path = downloads.resolve(download_id)
    if path is None:
        raise HTTPException(status_code=404, detail="That download has expired — generate it again.")
    return FileResponse(path, media_type=DOCX_MIME, filename=os.path.basename(path))
