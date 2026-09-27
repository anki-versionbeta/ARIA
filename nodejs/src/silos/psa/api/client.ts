/**
 * PSA endpoint wrappers over the shared HTTP client.
 *
 * Every path is built from `SILO_BASE`, which is the platform's silo mount prefix
 * (`da_platform/main.py` mounts each silo router at `/api/silos/<id>`). The standalone FastAPI shim
 * mounts at the same prefix, so these paths are byte-identical in both environments.
 */
import { api, buildQuery } from "@/api/http";
// Direct from the config module, not re-exported through `@/api/http` — that file is a verbatim copy
// of ARIA's and must stay one, so the binary download below resolves the base URL the same way it does.
import { getRuntimeConfig } from "@/config/runtime";
import type {
  CreatedRun,
  ExportResult,
  HealthResult,
  MessageResult,
  PaletteAddBody,
  PaletteResult,
  Presentation,
  ProductByColor,
  Program,
  RecommendResult,
  ReportResult,
  RunResult,
  RunStatus,
  SiteCount,
} from "./types";

const SILO_BASE = "/silos/psa";

export function getHealth() {
  return api.get<HealthResult>(`${SILO_BASE}/health`);
}

/** Rebuilds psa.db from the live Smartsheet. Seconds, not milliseconds. */
export function refreshFromSmartsheet() {
  return api.post<MessageResult>(`${SILO_BASE}/refresh`);
}

export function getPrograms() {
  return api.get<Program[]>(`${SILO_BASE}/programs`);
}

export function getPresentations(program: string) {
  return api.get<Presentation[]>(
    `${SILO_BASE}/presentations${buildQuery({ program })}`
  );
}

export function getPalette(vendor: string) {
  return api.get<PaletteResult>(
    `${SILO_BASE}/palette${buildQuery({ vendor })}`
  );
}

export function addPaletteColor(body: PaletteAddBody) {
  return api.post<MessageResult>(`${SILO_BASE}/palette`, body);
}

/**
 * POST, not DELETE: the shared client exports only get/post/put. The backend carries a
 * `/palette/remove` alias for exactly this reason (`api/router.py`).
 */
export function removePaletteColor(vendor: string, vendorColorName: string) {
  return api.post<MessageResult>(`${SILO_BASE}/palette/remove`, {
    vendor,
    vendor_color_name: vendorColorName,
  });
}

export function getProductsByColor(vendor: string, color: string) {
  return api.get<ProductByColor[]>(
    `${SILO_BASE}/products-by-color${buildQuery({ vendor, color })}`
  );
}

export function getSiteProductCounts() {
  return api.get<SiteCount[]>(`${SILO_BASE}/site-product-counts`);
}

export type RecommendBody = {
  program: string;
  source_row: number | string;
  /**
   * Optional restriction to ONE supplier. The screen omits it, so the recommendation spans both
   * catalogues and each suggested colour names its own supplier — at assessment time the cap is often
   * not yet tooled, so the supplier is an output of the colour decision rather than an input to it.
   * The Datwyler/West toggle now only chooses which catalogue the swatch grid shows.
   */
  vendor?: string;
};

/** Re-ingests the live Smartsheet first, so every click reflects the current sheet. */
export function recommendCapColors(body: RecommendBody) {
  return api.post<RecommendResult>(`${SILO_BASE}/recommend`, body);
}

export function exportRecommendation(body: RecommendBody) {
  return api.post<ExportResult>(`${SILO_BASE}/cap-export`, body);
}

export function generateReport(body: {
  program: string;
  presentation: number | string;
}) {
  return api.post<ReportResult>(`${SILO_BASE}/report`, body);
}

/**
 * Fetch a generated .docx and hand it to the browser.
 *
 * The shared client parses every response as text/JSON, so a binary body has to be fetched
 * directly. Same base URL and `credentials: "include"` so it behaves identically behind the
 * platform's ingress. Returns nothing; it either downloads or throws.
 */
export async function downloadGeneratedFile(
  downloadId: string,
  filename: string
) {
  const response = await fetch(
    `${getRuntimeConfig().apiBaseUrl}${SILO_BASE}/download/${downloadId}`,
    { credentials: "include" }
  );
  if (!response.ok) {
    throw new Error(
      response.status === 404
        ? "That download has expired — generate it again."
        : `Download failed (HTTP ${response.status}).`
    );
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoked on the next tick: Safari aborts the download if the URL dies synchronously.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * ---------------------------------------------------------------- runs (ARIA only)
 *
 * A job is the REPORT: clicking "Generate PSA report" queues one, which is what makes it appear in ARIA's
 * history. Only two of these are PSA's — status and file download are the PLATFORM's own endpoints, under
 * `/documents/...`, so PSA does not reimplement them.
 */
const PLATFORM_BASE = "/documents";

export type CreateRunBody = {
  program: string;
  source_row: number | string;
  /** Optional supplier restriction; the screen omits it. */
  vendor?: string;
  /** The screen names the job, because only it knows the readable labels. */
  title?: string;
};

export function createRun(body: CreateRunBody) {
  return api.post<CreatedRun>(`${SILO_BASE}/runs`, body);
}

/** Polled on the platform's own 2-second cadence while a run is in flight. */
export function getRunStatus(runId: string) {
  return api.get<RunStatus>(`${PLATFORM_BASE}/${runId}/status`);
}

/** The `report` stage's output — same shape as the synchronous route, plus the run's files. */
export function getRunResult(runId: string) {
  return api.get<RunResult>(`${SILO_BASE}/runs/${runId}/result`);
}

/**
 * Download a file the run produced, through the platform's endpoint.
 *
 * Same direct-fetch reasoning as `downloadGeneratedFile`: the shared client parses every response as
 * text/JSON, so a binary body has to be fetched here. Addressed by the platform's file id — the storage
 * key never reaches the client.
 */
export async function downloadRunFile(
  runId: string,
  fileId: string,
  filename: string
) {
  const response = await fetch(
    `${getRuntimeConfig().apiBaseUrl}${PLATFORM_BASE}/${runId}/files/${fileId}`,
    { credentials: "include" }
  );
  if (!response.ok) {
    throw new Error(`Download failed (HTTP ${response.status}).`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
