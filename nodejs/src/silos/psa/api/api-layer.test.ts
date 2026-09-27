/**
 * The PSA api layer on its own — no component rendered.
 *
 * Everything here goes through the SAME mocked `fetch` the component tests use (`test-support.tsx`),
 * so the request path, method and body are what is asserted. What this file adds over those tests is
 * the endpoints and branches no screen exercises: `/health`, `/palette/remove`, `/cap-export`, and the
 * two direct-`fetch` binary downloads that bypass `@/api/http` and read `getRuntimeConfig()`
 * themselves. Plus `describeError`, which every panel's error text is built from.
 *
 * The hooks are driven with `renderHook` rather than a screen: a wrapper is needed because
 * `test-support.tsx` exports `renderWithQuery` for elements only, and it is deliberately the same
 * QueryClient configuration (retries off) so an error path settles on the first response.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import {
  type ApiMock,
  loadConfigForTests,
  mockApi,
  PSA_WAIT,
  respond,
} from "../test-support";
import {
  paletteDatwyler,
  productsByColorBlue6043,
  programs,
  recommendBonte,
  refreshOk,
  reportBonte,
  siteCounts,
} from "./__fixtures__/live-payloads.gen";
import { downloadGeneratedFile, downloadRunFile } from "./client";
import {
  describeError,
  useAddPaletteColor,
  useCapExport,
  useCreateRun,
  useGenerateReport,
  useHealth,
  usePalette,
  usePresentations,
  useProductsByColor,
  usePrograms,
  useRecommend,
  useRefresh,
  useRemovePaletteColor,
  useRunResult,
  useRunStatus,
  useSiteCounts,
} from "./queries";
import type { HealthResult } from "./types";

const BASE = "/api/silos/psa";

/** Live-shaped `/health`: db built, Smartsheet reachable, palette carrying local edits. */
const health: HealthResult = {
  ok: true,
  smartsheet_live: true,
  db_built: true,
  palette_is_override: true,
};

/**
 * jsdom implements neither object-URL method and both download helpers need both. Assigned onto the
 * real `URL` (not stubbed globally) so `new URL(...)` keeps working and the revoke, which happens on a
 * `setTimeout(…, 0)` that can outlive the test, does not throw after the test has passed.
 */
const createObjectURL = vi.fn(() => "blob:psa-api-test");
const revokeObjectURL = vi.fn();
Object.assign(URL, { createObjectURL, revokeObjectURL });

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

const renderPsaHook = <T>(hook: () => T) => renderHook(hook, { wrapper });

describe("PSA read queries", () => {
  let api: ApiMock;

  beforeEach(async () => {
    api = mockApi({
      [`GET ${BASE}/health`]: health,
      [`GET ${BASE}/programs`]: programs,
      [`GET ${BASE}/site-product-counts`]: siteCounts,
      [`GET ${BASE}/palette`]: paletteDatwyler,
      [`GET ${BASE}/products-by-color`]: productsByColorBlue6043,
    });
    await loadConfigForTests();
  });

  it("reads /health and reports the flags the screen warns on", async () => {
    const { result } = renderPsaHook(() => useHealth());

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(result.current.data).toEqual(health);
    expect(api.calledPaths()).toEqual([`GET ${BASE}/health`]);
    expect(api.unmatched).toEqual([]);
  });

  it("does not retry a failed read — the message is the answer, not a blip", async () => {
    api = mockApi({
      [`GET ${BASE}/health`]: respond(503, {
        detail: "psa.db has not been built",
      }),
    });
    const { result } = renderPsaHook(() => useHealth());

    await waitFor(() => expect(result.current.isError).toBe(true), {
      timeout: PSA_WAIT,
    });
    // One attempt only: `retry: false` in the hook itself, so it holds in ARIA's own QueryClient too.
    expect(api.calls).toHaveLength(1);
    expect(describeError(result.current.error, "Could not load status")).toBe(
      "psa.db has not been built"
    );
  });

  it("reads the program list from the silo's own prefix", async () => {
    const { result } = renderPsaHook(() => usePrograms());

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toEqual([`GET ${BASE}/programs`]);
    expect(result.current.data).toHaveLength(programs.length);
    expect(result.current.data?.[0].label).toBe(
      "ABBV-151 (Livmo (GARP/TGFb mAb))"
    );
  });

  it("reads the site counts", async () => {
    const { result } = renderPsaHook(() => useSiteCounts());

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toEqual([`GET ${BASE}/site-product-counts`]);
    expect(result.current.data?.[0]).toEqual({
      site_code: "ABB",
      n_products: 9,
    });
  });

  it("reads a vendor's palette and reports whether it carries local edits", async () => {
    const { result } = renderPsaHook(() => usePalette("Datwyler"));

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toEqual([`GET ${BASE}/palette?vendor=Datwyler`]);
    expect(result.current.data?.is_override).toBe(paletteDatwyler.is_override);
    expect(result.current.data?.colors[0].vendor_color_name).toBe(
      "Transparent 6001"
    );
  });

  it("holds presentations back until a program is chosen, then sends it as a query param", async () => {
    const { result, rerender } = renderHook(
      ({ program }: { program: string | null }) => usePresentations(program),
      { wrapper, initialProps: { program: null } }
    );

    // The `enabled` guard: no program, no request — the endpoint 422s without one.
    expect(result.current.fetchStatus).toBe("idle");
    expect(api.calls).toHaveLength(0);

    api = mockApi({
      [`GET ${BASE}/presentations?program=AGN-151586`]: [
        { source_row: 20, label: "2R (2.00 mL) vial" },
      ],
    });
    rerender({ program: "AGN-151586" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toEqual([
      `GET ${BASE}/presentations?program=AGN-151586`,
    ]);
    expect(api.unmatched).toEqual([]);
  });

  it("holds products-by-color back until a colour is chosen, then sends both params", async () => {
    const { result, rerender } = renderHook(
      ({ color }: { color: string | null }) =>
        useProductsByColor("Datwyler", color),
      { wrapper, initialProps: { color: null } }
    );

    expect(result.current.fetchStatus).toBe("idle");
    expect(api.calls).toHaveLength(0);

    rerender({ color: "Blue 6043" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    // Both the vendor and the colour must survive URL encoding; the space is the interesting part.
    expect(api.calledPaths()).toEqual([
      `GET ${BASE}/products-by-color?vendor=Datwyler&color=Blue+6043`,
    ]);
    expect(result.current.data?.[0].product).toBe(
      productsByColorBlue6043[0].product
    );
  });
});

describe("PSA mutations", () => {
  let api: ApiMock;

  beforeEach(async () => {
    api = mockApi({
      [`POST ${BASE}/refresh`]: refreshOk,
      [`POST ${BASE}/palette`]: { ok: true, message: "Colour added." },
      [`POST ${BASE}/palette/remove`]: { ok: true, message: "Colour removed." },
      [`POST ${BASE}/cap-export`]: {
        ok: true,
        message: "Export ready.",
        download_id: "dl-1",
        filename: "cap_colours.xlsx",
      },
    });
    await loadConfigForTests();
  });

  it("refreshes with a POST carrying no body", async () => {
    const { result } = renderPsaHook(() => useRefresh());

    const refreshed = await result.current.mutateAsync();

    expect(api.calls).toHaveLength(1);
    expect(api.calls[0]).toMatchObject({
      method: "POST",
      url: `${BASE}/refresh`,
      body: undefined,
    });
    expect(refreshed).toEqual(refreshOk);
  });

  it("adds a palette colour with the body the backend names", async () => {
    const { result } = renderPsaHook(() => useAddPaletteColor());

    await result.current.mutateAsync({
      vendor: "Datwyler",
      vendor_color_name: "Transparent 9999",
      vendor_code: "9999",
      canonical_color: "Clear",
    });

    expect(api.calls[0].url).toBe(`${BASE}/palette`);
    expect(api.calls[0].method).toBe("POST");
    expect(api.calls[0].body).toEqual({
      vendor: "Datwyler",
      vendor_color_name: "Transparent 9999",
      vendor_code: "9999",
      canonical_color: "Clear",
    });
  });

  it("removes a palette colour by POSTing the alias, because the client has no delete", async () => {
    const { result } = renderPsaHook(() => useRemovePaletteColor("West"));

    // The hook closes over the vendor; the mutation argument is only the colour name.
    await result.current.mutateAsync("Dark Grey 4014");

    expect(api.calls).toHaveLength(1);
    expect(api.calls[0].method).toBe("POST");
    expect(api.calls[0].url).toBe(`${BASE}/palette/remove`);
    expect(api.calls[0].body).toEqual({
      vendor: "West",
      vendor_color_name: "Dark Grey 4014",
    });
  });

  it("exports the recommendation to /cap-export and returns the download handle", async () => {
    const { result } = renderPsaHook(() => useCapExport());

    const exported = await result.current.mutateAsync({
      program: "AGN-151586",
      source_row: 20,
    });

    expect(api.calls[0].url).toBe(`${BASE}/cap-export`);
    expect(api.calls[0].method).toBe("POST");
    // No `vendor`: the screen omits it so the export spans both catalogues.
    expect(api.calls[0].body).toEqual({
      program: "AGN-151586",
      source_row: 20,
    });
    expect(exported.download_id).toBe("dl-1");
    expect(exported.filename).toBe("cap_colours.xlsx");
  });

  it("invalidates the whole palette prefix on an edit, not just the edited vendor", async () => {
    api = mockApi({
      [`GET ${BASE}/palette`]: paletteDatwyler,
      [`POST ${BASE}/palette/remove`]: { ok: true, message: "Colour removed." },
    });

    // Both vendors mounted; the override document covers both, so both keys must refetch after an
    // edit made against only one of them.
    const { result } = renderHook(
      () => ({
        datwyler: usePalette("Datwyler"),
        west: usePalette("West"),
        remove: useRemovePaletteColor("West"),
      }),
      { wrapper }
    );
    await waitFor(
      () =>
        expect(
          result.current.datwyler.isSuccess && result.current.west.isSuccess
        ).toBe(true),
      { timeout: PSA_WAIT }
    );
    const paletteCalls = () =>
      api.calledPaths().filter((p) => p.includes("/palette?vendor="));
    expect(paletteCalls()).toHaveLength(2);

    await result.current.remove.mutateAsync("Dark Grey 4014");

    // The Datwyler key refetches too, despite `staleTime: 5 * 60 * 1000` — invalidation overrides it.
    await waitFor(() => expect(paletteCalls()).toHaveLength(4), {
      timeout: PSA_WAIT,
    });
    expect(paletteCalls()).toContain(`GET ${BASE}/palette?vendor=Datwyler`);
  });

  it("invalidates the DB-backed reads after a refresh, so the pickers refetch", async () => {
    api = mockApi({
      [`POST ${BASE}/refresh`]: refreshOk,
      [`GET ${BASE}/programs`]: programs,
    });

    // One QueryClient shared by both hooks, so the invalidation in `useRefresh` is observable.
    const { result } = renderHook(
      () => ({ programsQuery: usePrograms(), refresh: useRefresh() }),
      { wrapper }
    );
    await waitFor(
      () => expect(result.current.programsQuery.isSuccess).toBe(true),
      { timeout: PSA_WAIT }
    );
    expect(
      api.calledPaths().filter((path) => path === `GET ${BASE}/programs`)
    ).toHaveLength(1);

    await result.current.refresh.mutateAsync();

    await waitFor(
      () =>
        expect(
          api.calledPaths().filter((path) => path === `GET ${BASE}/programs`)
        ).toHaveLength(2),
      { timeout: PSA_WAIT }
    );
  });
});

describe("the two heavy mutations, which also rebuild psa.db", () => {
  let api: ApiMock;

  beforeEach(async () => {
    api = mockApi({
      [`POST ${BASE}/recommend`]: recommendBonte,
      [`POST ${BASE}/report`]: reportBonte,
      [`GET ${BASE}/site-product-counts`]: siteCounts,
    });
    await loadConfigForTests();
  });

  it("recommends against /recommend and returns the engine's own subject", async () => {
    const { result } = renderPsaHook(() => useRecommend());

    const recommendation = await result.current.mutateAsync({
      program: "AGN-151586",
      source_row: 20,
    });

    expect(api.calls[0].method).toBe("POST");
    expect(api.calls[0].url).toBe(`${BASE}/recommend`);
    expect(api.calls[0].body).toEqual({
      program: "AGN-151586",
      source_row: 20,
    });
    expect(recommendation.subject?.label).toBe("AGN-151586 (BoNT/E)");
    expect(recommendation.error).toBeNull();
  });

  it("passes a vendor restriction through when one is given", async () => {
    const { result } = renderPsaHook(() => useRecommend());

    await result.current.mutateAsync({
      program: "AGN-151586",
      source_row: 20,
      vendor: "Datwyler",
    });

    expect(api.calls[0].body).toEqual({
      program: "AGN-151586",
      source_row: 20,
      vendor: "Datwyler",
    });
  });

  it("invalidates the site counts after recommending, because the DB was rebuilt", async () => {
    const { result } = renderHook(
      () => ({ counts: useSiteCounts(), recommend: useRecommend() }),
      { wrapper }
    );
    await waitFor(() => expect(result.current.counts.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    const countCalls = () =>
      api.calledPaths().filter((p) => p === `GET ${BASE}/site-product-counts`);
    expect(countCalls()).toHaveLength(1);

    await result.current.recommend.mutateAsync({
      program: "AGN-151586",
      source_row: 20,
    });

    await waitFor(() => expect(countCalls()).toHaveLength(2), {
      timeout: PSA_WAIT,
    });
  });

  it("generates a report from the program and presentation alone", async () => {
    const { result } = renderPsaHook(() => useGenerateReport());

    const report = await result.current.mutateAsync({
      program: "AGN-151586",
      presentation: "20",
    });

    expect(api.calls[0].method).toBe("POST");
    expect(api.calls[0].url).toBe(`${BASE}/report`);
    expect(api.calls[0].body).toEqual({
      program: "AGN-151586",
      presentation: "20",
    });
    expect(report.status).toBe("ok");
    expect(report.download_id).toBe(reportBonte.download_id);
  });
});

describe("run hooks (ARIA only)", () => {
  const RUN_ID = "run-abc123";

  beforeEach(async () => {
    await loadConfigForTests();
  });

  it("queues a run under the silo's prefix and returns the platform's job id", async () => {
    const api = mockApi({ [`POST ${BASE}/runs`]: { id: RUN_ID } });
    const { result } = renderPsaHook(() => useCreateRun());

    const created = await result.current.mutateAsync({
      program: "AGN-151586",
      source_row: 20,
      title: "PSA — AGN-151586 (BoNT/E)",
    });

    expect(api.calls[0].method).toBe("POST");
    expect(api.calls[0].url).toBe(`${BASE}/runs`);
    expect(api.calls[0].body).toEqual({
      program: "AGN-151586",
      source_row: 20,
      title: "PSA — AGN-151586 (BoNT/E)",
    });
    expect(created.id).toBe(RUN_ID);
  });

  it("polls the PLATFORM's status route, not one of PSA's", async () => {
    const api = mockApi({
      [`GET /api/documents/${RUN_ID}/status`]: {
        id: RUN_ID,
        status: "running",
        stage: "assess",
        progress_pct: 40,
      },
    });
    const { result } = renderPsaHook(() => useRunStatus(RUN_ID));

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toContain(`GET /api/documents/${RUN_ID}/status`);
    expect(api.calledPaths().some((p) => p.includes("/silos/psa/runs"))).toBe(
      false
    );
    expect(result.current.data?.progress_pct).toBe(40);
  });

  it("stops polling once the run reaches a terminal status", async () => {
    const api = mockApi({
      [`GET /api/documents/${RUN_ID}/status`]: {
        id: RUN_ID,
        status: "complete",
        stage: "report",
        progress_pct: 100,
      },
    });
    const { result } = renderPsaHook(() => useRunStatus(RUN_ID));

    await waitFor(() => expect(result.current.data?.status).toBe("complete"), {
      timeout: PSA_WAIT,
    });
    const afterFirst = api.calls.length;
    // `refetchInterval` returns false on a terminal status. POLL_MS is 2000, so waiting past two
    // intervals proves the timer really stopped rather than merely not having fired yet.
    await new Promise((resolve) => setTimeout(resolve, 4500));
    expect(api.calls).toHaveLength(afterFirst);
  });

  it("does not poll a status without a run id", async () => {
    const api = mockApi({});
    const { result } = renderPsaHook(() => useRunStatus(null));

    expect(result.current.fetchStatus).toBe("idle");
    expect(api.calls).toHaveLength(0);
  });

  it("does not fetch a run result before the run is done", async () => {
    const api = mockApi({
      [`GET ${BASE}/runs/${RUN_ID}/result`]: { ...reportBonte, run_id: RUN_ID },
    });
    const { result } = renderPsaHook(() => useRunResult(RUN_ID, false));

    // Both guards matter: `enabled: Boolean(runId) && enabled`. Before the run finishes the `report`
    // checkpoint does not exist and the response would be an empty shell.
    expect(result.current.fetchStatus).toBe("idle");
    expect(api.calls).toHaveLength(0);
  });

  it("does not fetch a run result without a run id, even when enabled", async () => {
    const api = mockApi({});
    const { result } = renderPsaHook(() => useRunResult(null, true));

    expect(result.current.fetchStatus).toBe("idle");
    expect(api.calls).toHaveLength(0);
  });

  it("reads the finished run's detail from the silo's own result route", async () => {
    const api = mockApi({
      [`GET ${BASE}/runs/${RUN_ID}/result`]: {
        ...reportBonte,
        run_id: RUN_ID,
        run_status: "complete",
        files: [{ id: "file-1", filename: "psa_assessment.docx" }],
      },
    });
    const { result } = renderPsaHook(() => useRunResult(RUN_ID, true));

    await waitFor(() => expect(result.current.isSuccess).toBe(true), {
      timeout: PSA_WAIT,
    });
    expect(api.calledPaths()).toEqual([`GET ${BASE}/runs/${RUN_ID}/result`]);
    expect(result.current.data?.files[0].filename).toBe("psa_assessment.docx");
    expect(result.current.data?.verify_ok).toBe(reportBonte.verify_ok);
  });
});

/**
 * The two binary downloads. They call `fetch` directly instead of going through `@/api/http`, so the
 * base URL comes from `getRuntimeConfig()` in this module — worth asserting here, because a screen
 * test would only show that *something* was downloaded.
 */
describe("binary downloads", () => {
  beforeEach(async () => {
    createObjectURL.mockClear();
    await loadConfigForTests();
  });

  it("fetches a generated file by opaque id from the configured base URL, with cookies", async () => {
    const api = mockApi({
      [`GET ${BASE}/download/dl-42`]: { stub: "binary" },
    });

    await downloadGeneratedFile("dl-42", "psa_assessment.docx");

    expect(api.calledPaths()).toEqual([`GET ${BASE}/download/dl-42`]);
    // `credentials: "include"` is what makes it work behind the platform's ingress.
    expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("names the saved file and cleans the anchor out of the document", async () => {
    mockApi({ [`GET ${BASE}/download/dl-42`]: { stub: "binary" } });
    const clicked: { href: string; download: string }[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        // Captured mid-click: the element is removed immediately afterwards.
        clicked.push({ href: this.href, download: this.download });
        expect(this.isConnected).toBe(true);
      });

    await downloadGeneratedFile("dl-42", "psa_assessment.docx");

    expect(clicked).toEqual([
      { href: "blob:psa-api-test", download: "psa_assessment.docx" },
    ]);
    // No orphaned anchor left behind for the next render to trip over.
    expect(document.querySelectorAll("a[download]")).toHaveLength(0);
    click.mockRestore();
  });

  it("explains an expired generated download rather than failing silently", async () => {
    mockApi({
      [`GET ${BASE}/download/dl-gone`]: respond(404, { detail: "gone" }),
    });

    await expect(
      downloadGeneratedFile("dl-gone", "psa_assessment.docx")
    ).rejects.toThrow("That download has expired — generate it again.");
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("reports the status for a non-404 generated-download failure", async () => {
    mockApi({
      [`GET ${BASE}/download/dl-42`]: respond(500, { detail: "disk" }),
    });

    await expect(
      downloadGeneratedFile("dl-42", "psa_assessment.docx")
    ).rejects.toThrow("Download failed (HTTP 500).");
  });

  it("downloads a run's file through the PLATFORM's endpoint, not the silo's", async () => {
    const api = mockApi({
      "GET /api/documents/run-abc123/files/file-1": { stub: "binary" },
    });

    await downloadRunFile("run-abc123", "file-1", "psa_assessment.docx");

    // Addressed by the platform's file id — the storage key never reaches the client.
    expect(api.calledPaths()).toEqual([
      "GET /api/documents/run-abc123/files/file-1",
    ]);
    expect(api.calledPaths().some((path) => path.includes("/silos/psa/"))).toBe(
      false
    );
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("reports the status when a run file cannot be fetched", async () => {
    mockApi({
      "GET /api/documents/run-abc123/files/file-1": respond(404, {
        detail: "no such file",
      }),
    });

    // Note: unlike `downloadGeneratedFile`, this path has no special 404 wording — a run's files do
    // not expire, so a 404 here means the id is wrong rather than stale. The generic message is
    // therefore the right one, but it is worth knowing the two helpers differ.
    await expect(
      downloadRunFile("run-abc123", "file-1", "psa_assessment.docx")
    ).rejects.toThrow("Download failed (HTTP 404).");
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});

describe("describeError", () => {
  it("prefers FastAPI's detail string, which is the only message worth showing", () => {
    expect(
      describeError(
        new ApiError(400, { detail: "Program not found in the Smartsheet." }),
        "Could not recommend"
      )
    ).toBe("Program not found in the Smartsheet.");
  });

  it("names a 404 specially, because that is the standalone shim missing a route", () => {
    // `/runs` 404s outside ARIA, and the body is a bare {"detail":"Not Found"} — object, not the
    // string branch. (Here `detail` is deliberately non-string so the status branch is what answers.)
    expect(
      describeError(new ApiError(404, { detail: { code: 1 } }), "No run")
    ).toBe("No run (not found).");
  });

  it("falls back to the status for any other ApiError without a usable detail", () => {
    expect(describeError(new ApiError(500, null), "Report failed")).toBe(
      "Report failed (HTTP 500)."
    );
    expect(
      describeError(new ApiError(422, { detail: [] }), "Report failed")
    ).toBe("Report failed (HTTP 422).");
  });

  it("passes through a plain Error's message — a network failure reads better than a code", () => {
    expect(
      describeError(new TypeError("Failed to fetch"), "Report failed")
    ).toBe("Failed to fetch");
  });

  it("uses the fallback for a thrown value that says nothing", () => {
    expect(describeError(new Error(""), "Report failed")).toBe("Report failed");
    expect(describeError("just a string", "Report failed")).toBe(
      "Report failed"
    );
    expect(describeError(undefined, "Report failed")).toBe("Report failed");
  });
});
