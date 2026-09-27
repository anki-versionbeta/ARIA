/**
 * The PSA report panel, driven by the REAL captured `/report` response for BoNT/E
 * (`verify_ok: true`, "ALL CHECKS PASSED", risk Med, closest AGN-151607 (GemibotA)).
 *
 * These go through the mocked `fetch` rather than a mocked client, so the request path, method and
 * body are part of what is asserted — a wrong URL fails here instead of only against a live server.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { reportBonte } from "../api/__fixtures__/live-payloads.gen";
import type { ReportResult } from "../api/types";
import {
  type ApiMock,
  loadConfigForTests,
  mockApi,
  renderWithQuery,
  respond,
} from "../test-support";
import { ReportPanel } from "./report-panel";

const REPORT_URL = "/api/silos/psa/report";
const RUNS_URL = "/api/silos/psa/runs";
const DOWNLOAD_URL = `/api/silos/psa/download/${reportBonte.download_id}`;

/**
 * jsdom implements neither object-URL method, and `downloadGeneratedFile` needs both.
 *
 * Defined once on the real `URL` rather than replacing the global: `new URL(...)` must keep working,
 * and the revoke happens on a `setTimeout(…, 0)` that can outlive the test — a stub torn down in
 * `afterEach` would make that timer throw after the test had already passed.
 */
const createObjectURL = vi.fn(() => "blob:psa-test");
const revokeObjectURL = vi.fn();
Object.assign(URL, { createObjectURL, revokeObjectURL });

/** Generate, then wait for the result block. Every assertion below starts from this state. */
async function generateReport(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Generate PSA report" }));
}

describe("ReportPanel — successful run (BoNT/E, live Smartsheet)", () => {
  let api: ApiMock;

  beforeEach(async () => {
    createObjectURL.mockClear();
    api = mockApi({
      // 404 is exactly what the standalone shim returns: `psa/runs.py` needs platform types, so
      // `router.py` includes it only inside ARIA and there is no `/runs` route here at all. Stubbed
      // explicitly so the fallback is part of what these tests assert, not an accident of the mock.
      [`POST ${RUNS_URL}`]: respond(404, { detail: "Not Found" }),
      [`POST ${REPORT_URL}`]: reportBonte,
      [`GET ${DOWNLOAD_URL}`]: { stub: "binary" },
    });
    await loadConfigForTests();
  });

  it("posts the program and presentation exactly as selected", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    // Two calls, in this order: try to queue a job, then fall back because there is no platform here.
    await waitFor(() => expect(api.calls).toHaveLength(2));
    expect(api.calls[0].url).toBe(RUNS_URL);
    expect(api.calls[1].url).toBe(REPORT_URL);
    expect(api.calls[1].method).toBe("POST");
    expect(api.calls[1].body).toEqual({
      program: "AGN-151586",
      presentation: "20",
    });
    // The run attempt carries the same subject, plus the readable title the screen builds.
    expect(api.calls[0].body).toMatchObject({
      program: "AGN-151586",
      source_row: "20",
    });
    // An unmatched route would have answered 501; assert none was needed.
    expect(api.unmatched).toEqual([]);
  });

  it("reports the verify result and the presentation the run actually used", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    // Deliberately the full sentence: the bare "ALL CHECKS PASSED" also appears in the run log,
    // which the collapsed Accordion still renders into the DOM.
    const alert = await screen.findByText(
      /ALL CHECKS PASSED — generated PSA report for/
    );
    expect(alert).toHaveTextContent("AGN-151586 (BoNT/E)");
    // `presentation_used` is the int 20 while the picker held the string "20"; the label lookup is
    // int-to-int against `presentations[].source_row`, so it must still resolve.
    expect(alert).toHaveTextContent("presentation: 2R (2.00 mL) vial / TBD");
  });

  it("confirms the run used the live Smartsheet", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    expect(
      await screen.findByText("Data source: live Smartsheet ✓")
    ).toBeInTheDocument();
  });

  it("shows the provisional risk grade, its scope, and the closest comparator", async () => {
    const user = userEvent.setup();
    const { container } = renderWithQuery(
      <ReportPanel presentation="20" program="AGN-151586" />
    );
    await generateReport(user);

    await screen.findByText(/ALL CHECKS PASSED — generated PSA report for/);
    expect(container).toHaveTextContent("Provisional mix-up risk:");
    // "Med" is the overall grade AND a row grade for GemibotA and ABBV-1480 — three tags, not one.
    expect(screen.getAllByText("Med")).toHaveLength(3);
    expect(container).toHaveTextContent(
      "2 manufacturing co-located, same-family comparators — matches the report"
    );
    expect(screen.getByText("AGN-151607 (GemibotA)")).toBeInTheDocument();
    // What distinguishes it is the substance of the assessment, so it must be on screen.
    expect(
      screen.getByText("Vial / container size, Cap colour")
    ).toBeInTheDocument();
    expect(container).toHaveTextContent(
      "distinguished from AGN-151586 (BoNT/E) by 2 attribute(s)"
    );
  });

  it("keeps informational co-location separate and capped at six rows", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    expect(
      await screen.findByText(
        /informational only \(not assessed, not in the report\)/
      )
    ).toBeInTheDocument();
    // 10 informational rows came back. The first six render…
    expect(screen.getByText("ABBV-1480")).toBeInTheDocument();
    expect(screen.getByText("ABBV-383 (Etentamig)")).toBeInTheDocument();
    // …and the rest are dropped, as on the Dash screen.
    expect(
      screen.queryByText("ABBV-066 (Risankizumab)")
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/RGX-314 \(Sura-Vec/)).not.toBeInTheDocument();
  });

  it("exposes the run log without letting it dominate the panel", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    // Behind a collapsed Accordion, so it is present but not competing with the result for attention.
    // (Unity renders the panel's children into the DOM while collapsed, hidden rather than unmounted.)
    expect(await screen.findByText("Run log")).toBeInTheDocument();
  });

  it("downloads the .docx by opaque id, never by path", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    await user.click(
      await screen.findByRole("button", { name: /download psa report/i })
    );

    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(api.calledPaths()).toContain(`GET ${DOWNLOAD_URL}`);
    // The engine's `output_path` must never reach the client for the UI to act on.
    expect(JSON.stringify(reportBonte)).not.toContain("output_path");
  });
});

describe("ReportPanel — refusals and failures", () => {
  it("refuses to run without a product, and names what is missing", async () => {
    const api = mockApi({ [`POST ${REPORT_URL}`]: reportBonte });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation={null} program={null} />);

    await generateReport(user);

    expect(
      screen.getByText(
        "Select a product above first, then click Generate PSA report."
      )
    ).toBeInTheDocument();
    // Nothing may reach the server until the selection is complete.
    expect(api.calls).toHaveLength(0);
  });

  it("refuses to run without a presentation", async () => {
    const api = mockApi({ [`POST ${REPORT_URL}`]: reportBonte });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation={null} program="AGN-151586" />);

    await generateReport(user);

    expect(
      screen.getByText(
        "Select a presentation above first, then click Generate PSA report."
      )
    ).toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });

  it("surfaces a server error rather than a blank panel", async () => {
    mockApi({
      [`POST ${REPORT_URL}`]: respond(500, { detail: "engine exploded" }),
    });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(await screen.findByText("engine exploded")).toBeInTheDocument();
  });

  it("passes on the engine's own message when it cannot identify the product", async () => {
    const needsOverride: ReportResult = {
      status: "needs_override",
      message: "Could not identify the product with enough confidence.",
      log: "…",
    };
    mockApi({ [`POST ${REPORT_URL}`]: needsOverride });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(
      await screen.findByText(
        "Could not identify the product with enough confidence."
      )
    ).toBeInTheDocument();
    // No file was produced, so no download may be offered.
    expect(
      screen.queryByRole("button", { name: /download psa report/i })
    ).not.toBeInTheDocument();
  });

  it("warns when the run fell back to the stale .xlsx export", async () => {
    mockApi({
      [`POST ${REPORT_URL}`]: {
        ...reportBonte,
        log: "Data source: STALE .xlsx export\nALL CHECKS PASSED",
      },
    });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(
      await screen.findByText(/Live Smartsheet is not configured/)
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Data source: live Smartsheet ✓")
    ).not.toBeInTheDocument();
  });

  it("explains an expired download instead of failing silently", async () => {
    mockApi({
      [`POST ${REPORT_URL}`]: reportBonte,
      [`GET ${DOWNLOAD_URL}`]: respond(404, { detail: "gone" }),
    });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);

    await user.click(
      await screen.findByRole("button", { name: /download psa report/i })
    );

    expect(
      await screen.findByText("That download has expired — generate it again.")
    ).toBeInTheDocument();
  });

  it("says the file is missing when the run reports no output", async () => {
    mockApi({
      [`POST ${REPORT_URL}`]: { ...reportBonte, output_exists: false },
    });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(
      await screen.findByText(/the report file was not produced/)
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /download psa report/i })
    ).not.toBeInTheDocument();
  });
});

describe("ReportPanel — inside ARIA, where the report becomes a recorded job", () => {
  /**
   * The primary path. Clicking Generate queues a job, which is what makes the assessment appear in ARIA's
   * history tab; the worker does the work and the finished detail comes back from the run rather than from
   * the synchronous route.
   *
   * Asserted over the mocked `fetch` because the split matters and is invisible locally: two of these calls
   * are PSA's (`/silos/psa/...`) and two are the PLATFORM's (`/documents/...`). Sending one to the wrong
   * place would 404 only inside ARIA, where none of our tests run.
   */
  const RUN_ID = "run-xyz789";
  const STATUS_URL = `/api/documents/${RUN_ID}/status`;
  const RESULT_URL = `/api/silos/psa/runs/${RUN_ID}/result`;
  const FILE_ID = "file-abc";

  let api: ApiMock;

  const routes = (overrides: Record<string, unknown> = {}) => ({
    [`POST ${RUNS_URL}`]: { id: RUN_ID },
    [`GET ${STATUS_URL}`]: {
      id: RUN_ID,
      status: "complete",
      stage: "report",
      progress_pct: 100,
      progress_message: "Assessment generated",
    },
    [`GET ${RESULT_URL}`]: {
      ...reportBonte,
      run_id: RUN_ID,
      run_status: "complete",
      stage: "report",
      files: [
        { id: FILE_ID, filename: "psa_assessment.docx", size_bytes: 27794 },
      ],
    },
    [`GET /api/documents/${RUN_ID}/files/${FILE_ID}`]: { stub: "binary" },
    ...overrides,
  });

  beforeEach(async () => {
    createObjectURL.mockClear();
    api = mockApi(routes());
    await loadConfigForTests();
  });

  it("queues a job instead of doing the work in the request", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    await waitFor(() =>
      expect(api.calls.find((call) => call.url === RUNS_URL)).toBeDefined()
    );
    // The synchronous route must NOT be used when a platform is present: that is what would leave the
    // history tab empty, which is the whole reason the job exists.
    expect(api.calls.some((call) => call.url === REPORT_URL)).toBe(false);
  });

  it("polls the PLATFORM's status endpoint, not one of PSA's", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    await waitFor(() =>
      expect(api.calledPaths()).toContain(`GET ${STATUS_URL}`)
    );
    expect(
      api
        .calledPaths()
        .some((path) => path.includes(`${RUNS_URL}/${RUN_ID}/status`))
    ).toBe(false);
  });

  it("shows the job id, so the run can be found in the history tab", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(
      await screen.findByText(new RegExp(`Recorded as job ${RUN_ID}`))
    ).toBeInTheDocument();
  });

  it("renders the finished report's detail from the run, not from a second call", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    // Same detail the synchronous route always produced — the verify banner and the provisional risk —
    // which is what the `/runs/{id}/result` endpoint exists to preserve.
    // Anchored to the alert HEADING: the run log below it also contains "ALL CHECKS PASSED", so a plain
    // text query matches twice. (That log is also where the engine's absolute server paths surface — a
    // known, recorded issue: `ReportOut.log` is captured stdout.)
    expect(
      await screen.findByRole("heading", { name: /ALL CHECKS PASSED/ })
    ).toBeInTheDocument();
    expect(api.calledPaths()).toContain(`GET ${RESULT_URL}`);
  });

  it("downloads the deliverable through the platform, not a temporary id", async () => {
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);
    await user.click(
      await screen.findByRole("button", { name: /Download PSA report/ })
    );

    // A run's output belongs to the run, where it stays downloadable and auditable — as against the
    // synchronous route's process-local download id.
    await waitFor(() =>
      expect(api.calledPaths()).toContain(
        `GET /api/documents/${RUN_ID}/files/${FILE_ID}`
      )
    );
    expect(
      api.calledPaths().some((path) => path.includes("/silos/psa/download/"))
    ).toBe(false);
  });

  it("reports a failed job rather than leaving the button spinning", async () => {
    api = mockApi(
      routes({
        [`GET ${STATUS_URL}`]: {
          id: RUN_ID,
          status: "failed",
          stage: "fetch",
          progress_pct: 10,
          progress_message: "Smartsheet capture failed",
        },
      })
    );
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);

    await generateReport(user);

    expect(await screen.findByText(/The job failed/)).toBeInTheDocument();
  });
});
