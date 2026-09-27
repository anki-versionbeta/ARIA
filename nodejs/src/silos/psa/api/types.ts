/**
 * Response shapes of the PSA API (`api/models.py` in this repo).
 *
 * Two tiers, mirroring the backend's own split:
 *
 *  - Pickers, palette and tables are STRICT on the server, so they are exact here too.
 *  - `RecommendResult` / `ReportResult` come from `cap_recommend` and `workflow` through
 *    permissive (`extra="allow"`) models. Those dicts are the engines' published, deliberately
 *    pluggable contract — `GRADE_THRESHOLDS`, `CLOSE_DELTA_E`, `COMPARE_BY_STATE` and the pending SME
 *    Risk Index all change what comes back. Everything optional-typed accordingly: the screen must
 *    treat a missing key as "not applicable", never assume it.
 */

export type HealthResult = {
  ok: boolean;
  /** False means the pipeline fell back to the stale .xlsx export — the screen warns about it. */
  smartsheet_live: boolean;
  /** False until the analysis database has been built. */
  db_built: boolean;
  /** True when the cap palette carries local edits on top of the shipped catalogue. */
  palette_is_override: boolean;
};

export type MessageResult = {
  ok: boolean;
  message: string;
};

export type Program = {
  program_no: string;
  program_name: string;
  label: string;
};

/** One presentation of a product, keyed by its Smartsheet `source_row` (stable across rebuilds). */
export type Presentation = {
  source_row: number | string;
  label: string;
  vial_size: string;
  batch_type: string;
  strength: string;
  list_number: string;
};

export type PaletteColor = {
  vendor: string;
  vendor_color_name: string;
  vendor_code: string;
  canonical_color: string;
  hue_group: string;
  /** Approximate representative sRGB — the palette CSV is pending measured swatches. */
  hex: string;
  sizes_mm: string;
  sizes: string[];
  finish: string;
  off_the_shelf: boolean;
  component: string;
  notes: string;
};

/**
 * GET /palette. An object rather than a bare array because the screen has to be able to say WHICH
 * catalogue it is showing: the palette is a shipped default plus an override object, so "this has been
 * edited" and "put it back" are both part of the answer.
 */
export type PaletteResult = {
  colors: PaletteColor[];
  is_override: boolean;
  /** Colours added or replaced by the override. */
  added: number;
  /** Shipped colours hidden by the override. */
  removed: number;
  /** A stored override could not be read, so the shipped catalogue is in force. */
  override_invalid: boolean;
};

/** Body of POST /palette. Matches `PaletteAddIn`; `sizes_mm` defaults to "13;20" server-side. */
export type PaletteAddBody = {
  vendor: string;
  vendor_color_name: string;
  canonical_color: string;
  vendor_code?: string;
  hue_group?: string;
  hex?: string;
  sizes_mm?: string;
  finish?: string;
  off_the_shelf?: boolean;
  component?: string;
  notes?: string;
};

export type ProductByColor = {
  product: string;
  contact: string;
  strength: string;
  vial_size: string;
  form: string;
  mfr_sites: string;
  cap_color_name: string;
};

export type SiteCount = {
  site_code: string;
  n_products: number;
};

// ---------------------------------------------------------------- recommendation
/** The subject presentation's own cap, when one is already chosen. */
export type SubjectCap = {
  raw?: string | null;
  canonical?: string | null;
  hex?: string | null;
  vendor_color_name?: string | null;
  /** True when the recorded text ("TBD"/"TBC") matched no palette colour — no hex to show. */
  is_unknown?: boolean | null;
};

export type RecommendSubject = {
  /** Rebuilt on every ingest — never persist it; `source_row` is the stable key. */
  product_id?: number | null;
  label?: string | null;
  program_no?: string | null;
  vial_size?: string | null;
  /** `_vial_key` normalisation of `vial_size` (`2R (2.00 mL) vial` → `2R`) — the uniqueness key. */
  vial_key?: string | null;
  /** Smartsheet "State" (`product.modality`) — comparison is scoped to the same state. */
  state?: string | null;
  /** Raw `cap_color` text, unnormalised; `caps_display` is the same list resolved for rendering. */
  current_caps?: string[] | null;
  caps_display?: SubjectCap[] | null;
};

export type RecommendedColor = {
  vendor_color_name: string;
  hex?: string | null;
  /** Canonical hue (`Green`), as against `vendor_color_name`'s `Green 6007`. */
  color?: string | null;
  canonical_color?: string | null;
  vendor?: string | null;
  vendor_code?: string | null;
  off_the_shelf?: boolean | null;
  /**
   * Distance to the nearest comparator cap. `null` means no same-state comparator has a recorded
   * cap, so there is NO ranking signal — the list is then just palette order and the UI must not
   * claim "most visually distinct first".
   */
  min_delta_e?: number | null;
  /** Which taken cap that distance is to ("Orange on Botox @ 10 mL vial"); `null` with no signal. */
  nearest_taken?: string | null;
  /** The engine's own one-line justification, already phrased for display. */
  rationale?: string | null;
};

/**
 * A cap already in use by a co-located, same-state presentation — i.e. one of the presentations this
 * one could be mixed up with. `cap_recommend._grade_taken` grades and sorts these highest-risk first.
 */
export type TakenColor = {
  color: string;
  vial_key: string;
  product: string;
  hex?: string | null;
  state?: string | null;
  program_no?: string | null;
  /** The Smartsheet row behind this comparator — its stable identity across DB rebuilds. */
  source_row?: number | string | null;
  vial_size?: string | null;
  /** The Smartsheet text behind `color`; `is_custom` ⇒ it is off-palette (e.g. BoNT/E's magenta). */
  raw?: string | null;
  is_custom?: boolean | null;
  /** Same vial size as the subject ⇒ the container alone does not distinguish the two. */
  same_vial?: boolean | null;
  /**
   * ΔE from this cap to the subject's OWN cap. `null` when the subject has no cap with a known swatch
   * yet, or this cap has none — there is then no shade comparison and the UI must not imply one.
   */
  delta_e_to_subject?: number | null;
  /** ΔE below `cap_recommend.SAME_SHADE_DELTA_E` ⇒ reads as the same shade. */
  close_shade?: boolean | null;
  /** Provisional grade: same vial AND close shade ⇒ High, either one ⇒ Medium, neither ⇒ Low. */
  risk?: "High" | "Medium" | "Low" | string | null;
  /** The engine's own explanation of that grade, already phrased for display. */
  risk_reason?: string | null;
};

/**
 * A palette colour the engine ruled OUT: the same colour is already used at the subject's vial size at a
 * shared manufacturing site. Carries the same descriptive fields as a recommendation, minus `rationale`
 * (which is replaced by `reason`), so a screen can show a swatch for it if it wants one.
 */
export type DiscouragedColor = {
  vendor_color_name: string;
  reason: string;
  hex?: string | null;
  color?: string | null;
  vendor?: string | null;
  vendor_code?: string | null;
  off_the_shelf?: boolean | null;
  min_delta_e?: number | null;
  nearest_taken?: string | null;
};

export type RecommendResult = {
  /** Set instead of a recommendation when the presentation is not in the analysis DB. */
  error?: string | null;
  subject?: RecommendSubject | null;
  subject_selected?: SubjectCap | null;
  /**
   * The supplier RESTRICTION that was applied, or `null` for none — which is what the screen sends.
   * Do not read this to label a colour: with no restriction it is null while every suggestion still
   * names its own supplier in `RecommendedColor.vendor`.
   */
  vendor?: string | null;
  /** Every supplier whose catalogue was searched, e.g. `["Datwyler", "West"]`. */
  vendors_considered?: string[] | null;
  /** Manufacturing sites only. Empty ⇒ nothing to compare against; `note` explains. */
  sites?: string[] | null;
  n_colocated?: number | null;
  /** Colour reserved as this presentation's unique first choice across the program. */
  first_unique?: string | null;
  taken?: TakenColor[] | null;
  recommended?: RecommendedColor[] | null;
  discouraged?: DiscouragedColor[] | null;
  note?: string | null;
  program_label?: string | null;
  presentation_label?: string | null;
};

export type ExportResult = {
  ok: boolean;
  message: string;
  download_id?: string | null;
  filename?: string | null;
};

// ---------------------------------------------------------------- report
/** One attribute of one subject↔comparator comparison, as persisted to `risk_attribute_cmp`. */
export type RiskAttributeCmp = {
  attribute: string;
  subject_value: string;
  comparator_value: string;
  is_distinguishing: boolean;
  /** From `ATTRIBUTE_WEIGHTS` — all 1 until the SME Risk Index supplies real weights. */
  weight: number;
};

export type RiskComparator = {
  label: string;
  risk_level: string;
  n_distinguishing: number;
  shared_sites: string[];
  distinguishing: string[];
  program_no?: string | null;
  batch_type?: string | null;
  /** The complement of `distinguishing` — attributes that look the same on both products. */
  shared_attributes?: string[] | null;
  /** Every attribute compared, distinguishing or not. The screen shows the summary, not this. */
  attributes?: RiskAttributeCmp[] | null;
};

export type RiskSubject = {
  product_id?: number | null;
  program_no?: string | null;
  program_name?: string | null;
  batch_type?: string | null;
  label?: string | null;
};

export type RiskResult = {
  overall_risk?: string | null;
  n_comparators?: number | null;
  rationale?: string | null;
  subject?: RiskSubject | null;
  /** The single most confusable comparator — the one the grade came from. */
  closest?: RiskComparator | null;
  comparators?: RiskComparator[] | null;
  /** Packaging-site / other-family co-location: shown for context, never in the report. */
  informational?: RiskComparator[] | null;
  note?: string | null;
};

/** A presentation as echoed back by the report run — fewer fields than the picker's `Presentation`. */
export type ReportPresentation = {
  source_row: number | string;
  label: string;
  list_number?: string | null;
};

export type ReportResult = {
  /** 'ok' carries a report; 'message' is a validation answer; 'needs_override' failed to identify. */
  status?: string | null;
  message?: string | null;
  program?: string | null;
  identified_name?: string | null;
  /** `workflow.identify()`'s route: 'smartsheet' | 'new_product' | 'undetermined'. */
  decision?: string | null;
  reason?: string | null;
  /** The code the document was identified by, before the Smartsheet row supplied the name. */
  subject_code?: string | null;
  presentation_used?: number | string | null;
  presentations?: ReportPresentation[] | null;
  verify_ok?: boolean | null;
  risk?: RiskResult | null;
  /** Engine tuples: [score, program_no, program_name, …]. */
  alternatives?: unknown[][] | null;
  log?: string | null;
  output_exists?: boolean | null;
  /** Replaces the engine's `output_path`; the filesystem path never reaches the client. */
  download_id?: string | null;
  filename?: string | null;
};

export const VENDORS = ["Datwyler", "West"] as const;
export type Vendor = (typeof VENDORS)[number];

/**
 * ---------------------------------------------------------------- runs (ARIA only)
 *
 * A PSA job is `fetch -> report`, straight through: clicking "Generate PSA report" is what puts an entry in
 * ARIA's history. These endpoints exist only inside ARIA — `psa/runs.py` imports platform types, so the
 * standalone shim has no run routes and a call to one 404s, which the screen handles by falling back to the
 * synchronous report route.
 */

/** `POST /silos/psa/runs` -> the queued run's id. */
export type CreatedRun = { id: string };

/**
 * `GET /api/documents/{id}/status` — the PLATFORM's endpoint, not PSA's, polled every 2 seconds.
 * Only the fields the screen uses are declared; the response carries more.
 */
export type RunStatus = {
  id: string;
  status: string;
  stage?: string | null;
  progress_pct?: number | null;
  progress_message?: string | null;
};

/** One file the run produced, downloadable through the platform's file endpoint. */
export type RunFile = {
  id: string;
  filename: string;
  size_bytes?: number | null;
};

/**
 * `GET /silos/psa/runs/{id}/result` — the `report` stage's own output, so a finished run shows the same
 * detail the synchronous route always did. It IS a `ReportResult` plus the run's identity and files, which
 * is what lets one rendering path serve both.
 */
export type RunResult = ReportResult & {
  run_id: string;
  run_status: string;
  stage?: string | null;
  files: RunFile[];
};
