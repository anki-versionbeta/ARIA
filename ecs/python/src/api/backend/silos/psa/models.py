"""Request/response models for the PSA API.

Two tiers, deliberately:

* The pickers, palette, and tables get STRICT models — their shapes are stable and a typo should fail
  loudly.
* `recommend` and `report` return the engines' own dicts through PERMISSIVE models
  (`extra="allow"`, every field optional). Those dicts are the engines' published contract and are
  expected to grow: `GRADE_THRESHOLDS`, `CLOSE_DELTA_E`, `COMPARE_BY_STATE`/`STATE_GROUPS` and the
  pending SME Risk Index all change what comes back. A strict model would silently drop new keys and
  the front-end would never see them; a permissive one still documents the known shape in OpenAPI.
  They are also shape-shifting today — `recommend` omits `n_colocated` when the subject has no
  manufacturing site, and returns only `error` for an unknown product — so nothing can be required.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- common
class HealthOut(BaseModel):
    """Liveness plus the facts that change how every other answer should be read.

    No filesystem paths. The previous version returned `db_path`, `output_dir` and `palette_csv`, which
    told a client where the server keeps its files — useful while everything ran on one machine, and
    wrong for a deployed silo. What a caller actually needs to know is whether the data is live and
    whether the palette has been edited, so those are what it reports.
    """

    ok: bool
    smartsheet_live: bool = Field(
        description="False means the pipeline falls back to the stale .xlsx export.")
    db_built: bool = Field(
        description="False until the analysis database has been built — refresh from Smartsheet.")
    palette_is_override: bool = Field(
        description="True when the cap palette has local edits on top of the shipped catalogue.")


class MessageOut(BaseModel):
    ok: bool
    message: str


# ---------------------------------------------------------------- pickers
class ProgramOut(BaseModel):
    program_no: str
    program_name: str = ""
    label: str


class PresentationOut(BaseModel):
    source_row: int | str
    label: str
    vial_size: str = ""
    batch_type: str = ""
    strength: str = ""
    list_number: str = ""


# ---------------------------------------------------------------- palette
class PaletteColorOut(BaseModel):
    vendor: str
    vendor_color_name: str
    vendor_code: str = ""
    canonical_color: str = ""
    hue_group: str = ""
    hex: str = Field("", description="Approximate representative sRGB — pending measured swatches.")
    sizes_mm: str = ""
    sizes: list[str] = []
    finish: str = ""
    off_the_shelf: bool = True
    component: str = "pp_disc"
    notes: str = ""


class PaletteOut(BaseModel):
    """The effective palette and its provenance.

    An object rather than a bare list, so the screen can say WHICH catalogue it is showing. This mirrors
    ARIA's prompt editor, where the response carries `petra_is_override` alongside the text: an editable
    default is only safe to edit if "this has been changed" and "put it back" are both visible.
    """

    colors: list[PaletteColorOut]
    is_override: bool = Field(
        description="True when local edits are applied on top of the shipped catalogue.")
    added: int = Field(0, description="Colours added or replaced by the override.")
    removed: int = Field(0, description="Shipped colours hidden by the override.")
    override_invalid: bool = Field(
        False,
        description=("True when a stored override could not be read, so the shipped catalogue is in "
                     "force. A malformed override never blocks a recommendation."))


class PaletteRemoveIn(BaseModel):
    """Body for the POST alias of the palette delete — see `router.remove_palette_color_post`."""
    vendor: str
    vendor_color_name: str


class PaletteAddIn(BaseModel):
    vendor: str
    vendor_color_name: str
    canonical_color: str
    vendor_code: str = ""
    hue_group: str = ""
    hex: str = ""
    sizes_mm: str = "13;20"
    finish: str = ""
    off_the_shelf: bool = True
    component: str = "pp_disc"
    notes: str = ""


# ---------------------------------------------------------------- tables
class ProductByColorOut(BaseModel):
    product: str
    contact: str = ""
    strength: str = ""
    vial_size: str = ""
    form: str = ""
    mfr_sites: str = ""
    cap_color_name: str = ""


class SiteCountOut(BaseModel):
    site_code: str
    n_products: int


# ---------------------------------------------------------------- recommendation
class RecommendIn(BaseModel):
    program: str
    source_row: int | str
    vendor: str | None = None
    """Optional restriction to ONE supplier. Omit it — which the screen now does — to consider every
    supplier and let the best colour name its own supplier (decided 2026-08-21). It is not the screen's
    Datwyler/West toggle any more: that toggle chooses which catalogue the swatch GRID displays, because a
    cap that is not yet tooled or contracted has no supplier to be constrained to. Kept as a real parameter
    rather than a silently-ignored one so a caller who genuinely is committed to a supplier can say so."""


class RecommendOut(BaseModel):
    """`cap_recommend` output, passed through. See the module docstring for why nothing is required."""
    model_config = ConfigDict(extra="allow")

    error: str | None = None
    subject: dict[str, Any] | None = None
    subject_selected: dict[str, Any] | None = None
    vendor: str | None = None
    sites: list[str] | None = None
    n_colocated: int | None = None
    first_unique: str | None = None
    taken: list[dict[str, Any]] | None = None
    recommended: list[dict[str, Any]] | None = None
    discouraged: list[dict[str, Any]] | None = None
    note: str | None = None
    # Context the screen shows around the engine result.
    program_label: str | None = None
    presentation_label: str | None = None


# ---------------------------------------------------------------- report
class ReportIn(BaseModel):
    program: str
    presentation: int | str = Field(
        description="A presentation's source_row (preferred) or its list_number.")


class ReportOut(BaseModel):
    """`workflow.process_documents` output, passed through. Status is 'ok', 'message' or
    'needs_override'; only the first carries a report."""
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    message: str | None = None
    program: str | None = None
    identified_name: str | None = None
    decision: str | None = None
    reason: str | None = None
    presentation_used: int | str | None = None
    presentations: list[dict[str, Any]] | None = None
    verify_ok: bool | None = None
    risk: dict[str, Any] | None = None
    alternatives: list[Any] | None = None
    log: str | None = None
    output_exists: bool | None = None
    # Replaces the engine's `output_path`: the filesystem path never goes to the client.
    download_id: str | None = None
    filename: str | None = None


class ExportOut(BaseModel):
    ok: bool
    message: str
    download_id: str | None = None
    filename: str | None = None
