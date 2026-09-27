/**
 * TanStack Query hooks for the PSA screen.
 *
 * Read queries carry `retry: false` because a failure here is a configuration answer (unbuilt
 * database, missing Smartsheet credential), not a transient blip — retrying just delays the message
 * the screen wants to show.
 *
 * Refresh, recommend and report are mutations even though two of them only read: each rebuilds
 * `psa.db` server-side, so they must be user-triggered and must invalidate what they changed. That
 * mirrors the Dash screen, where nothing renders until the button is clicked.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/http";
import {
  addPaletteColor,
  type CreateRunBody,
  createRun,
  exportRecommendation,
  generateReport,
  getHealth,
  getPalette,
  getPresentations,
  getProductsByColor,
  getPrograms,
  getRunResult,
  getRunStatus,
  getSiteProductCounts,
  type RecommendBody,
  recommendCapColors,
  refreshFromSmartsheet,
  removePaletteColor,
} from "./client";
import type { PaletteAddBody } from "./types";

const KEYS = {
  health: ["psa", "health"] as const,
  programs: ["psa", "programs"] as const,
  presentations: (program: string) =>
    ["psa", "presentations", program] as const,
  palette: (vendor: string) => ["psa", "palette", vendor] as const,
  productsByColor: (vendor: string, color: string) =>
    ["psa", "products-by-color", vendor, color] as const,
  siteCounts: ["psa", "site-product-counts"] as const,
  runStatus: (runId: string) => ["psa", "run-status", runId] as const,
  runResult: (runId: string) => ["psa", "run-result", runId] as const,
};

/** Statuses the worker will not move on from, so polling stops. */
const TERMINAL_RUN_STATUSES = new Set([
  "complete",
  "completed",
  "failed",
  "cancelled",
]);

/** The platform's own cadence — `documents.py:103` says the browser polls status every 2 seconds. */
const POLL_MS = 2000;

export function useHealth() {
  return useQuery({ queryKey: KEYS.health, queryFn: getHealth, retry: false });
}

export function usePrograms() {
  return useQuery({
    queryKey: KEYS.programs,
    queryFn: getPrograms,
    retry: false,
  });
}

export function usePresentations(program: string | null) {
  return useQuery({
    queryKey: KEYS.presentations(program ?? ""),
    queryFn: () => getPresentations(program as string),
    enabled: Boolean(program),
    retry: false,
  });
}

export function usePalette(vendor: string) {
  return useQuery({
    queryKey: KEYS.palette(vendor),
    queryFn: () => getPalette(vendor),
    retry: false,
    // The palette is a shipped asset plus an override object; it only changes through the editor
    // below, and every editor mutation invalidates this key.
    staleTime: 5 * 60 * 1000,
  });
}

export function useProductsByColor(vendor: string, color: string | null) {
  return useQuery({
    queryKey: KEYS.productsByColor(vendor, color ?? ""),
    queryFn: () => getProductsByColor(vendor, color as string),
    enabled: Boolean(color),
    retry: false,
  });
}

export function useSiteCounts() {
  return useQuery({
    queryKey: KEYS.siteCounts,
    queryFn: getSiteProductCounts,
    retry: false,
  });
}

/** Rebuilds the database, so it invalidates every DB-backed read. The palette is not DB-backed. */
export function useRefresh() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: refreshFromSmartsheet,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEYS.programs });
      queryClient.invalidateQueries({ queryKey: KEYS.siteCounts });
      queryClient.invalidateQueries({ queryKey: ["psa", "presentations"] });
      queryClient.invalidateQueries({ queryKey: ["psa", "products-by-color"] });
      queryClient.invalidateQueries({ queryKey: KEYS.health });
    },
  });
}

/**
 * An edit lands in the palette override, which is one document covering BOTH vendors and which every
 * `/palette` response reports on (`is_override`, `added`, `removed`) — so the prefix is invalidated,
 * not just this vendor's key, and `health` with it.
 */
function invalidatePalette(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["psa", "palette"] });
  queryClient.invalidateQueries({ queryKey: KEYS.health });
}

export function useAddPaletteColor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PaletteAddBody) => addPaletteColor(body),
    onSuccess: () => invalidatePalette(queryClient),
  });
}

export function useRemovePaletteColor(vendor: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vendorColorName: string) =>
      removePaletteColor(vendor, vendorColorName),
    onSuccess: () => invalidatePalette(queryClient),
  });
}

/**
 * Recommend also re-ingests Smartsheet, so it invalidates the DB-backed reads for the same reason
 * refresh does — the pickers and counts may now be different.
 */
export function useRecommend() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: RecommendBody) => recommendCapColors(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEYS.siteCounts });
      queryClient.invalidateQueries({ queryKey: ["psa", "products-by-color"] });
    },
  });
}

export function useCapExport() {
  return useMutation({
    mutationFn: (body: RecommendBody) => exportRecommendation(body),
  });
}

export function useGenerateReport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { program: string; presentation: number | string }) =>
      generateReport(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEYS.siteCounts });
      queryClient.invalidateQueries({ queryKey: ["psa", "products-by-color"] });
    },
  });
}

/**
 * Turn any thrown value into something worth showing. Follows ARIA's own pattern
 * (`mfg-atr-new-document.tsx`): prefer FastAPI's `detail`, fall back to the status, then a generic.
 */
export function describeError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === "string") return body.detail;
    if (error.status === 404) return `${fallback} (not found).`;
    return `${fallback} (HTTP ${error.status}).`;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

/**
 * ---------------------------------------------------------------- runs (ARIA only)
 *
 * These 404 against the standalone shim, which has no run routes, so every hook is `retry: false` and the
 * report panel falls back to the synchronous route rather than retrying something that will never exist.
 */
export function useCreateRun() {
  return useMutation({ mutationFn: (body: CreateRunBody) => createRun(body) });
}

/**
 * Poll a run until it stops moving. `refetchInterval` returns false on a terminal status, so a finished
 * job is not polled forever.
 */
export function useRunStatus(runId: string | null) {
  return useQuery({
    queryKey: KEYS.runStatus(runId ?? ""),
    queryFn: () => getRunStatus(runId as string),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return POLL_MS;
      return TERMINAL_RUN_STATUSES.has(status) ? false : POLL_MS;
    },
  });
}

/**
 * The finished report's detail. Fetched only once the run is done — before that the `report` checkpoint
 * does not exist and the response would be an empty shell.
 */
export function useRunResult(runId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: KEYS.runResult(runId ?? ""),
    queryFn: () => getRunResult(runId as string),
    enabled: Boolean(runId) && enabled,
    retry: false,
  });
}
