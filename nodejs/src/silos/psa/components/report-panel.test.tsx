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

/**
 * How the panel renders the parts of a `ReportResult` the captured BoNT/E payload does not exercise.
 *
 * `ReportResult` comes through a permissive (`extra="allow"`) model over `workflow`'s own dict, and the
 * grades, the reason codes and the risk block are all pluggable — `GRADE_THRESHOLDS`, `COMPARE_BY_STATE`
 * and the pending SME Risk Index each change what arrives. So each of these is a shape the server can
 * legitimately send today, asserted against what an assessor would then see on screen.
 *
 * `log: ""` throughout: the real run log repeats the grade words and the programme codes, so a plain text
 * query would match the log as well as the thing under test. An empty log also collapses the Run log
 * accordion away, which is asserted in its own right below.
 */
describe("ReportPanel — grades, reason codes and the risk block", () => {
  const RISK_HIGH = {
    overall_risk: "High",
    n_comparators: 1,
    rationale: "Same vial size and an indistinguishable cap shade.",
    comparators: [
      {
        label: "AGN-151607 (GemibotA)",
        risk_level: "Low",
        n_distinguishing: 3,
        shared_sites: ["ABB"],
        distinguishing: ["Cap colour"],
      },
    ],
  };

  /** Only the fields the panel reads, with the noisy log removed. */
  const withResult = (overrides: Partial<ReportResult>): ReportResult => ({
    ...reportBonte,
    log: "",
    ...overrides,
  });

  /**
   * Unity's `Tag` carries its colour in a class, and colour is how the grade reads at a glance, so the
   * class IS the observable here — there is no accessible name or text that distinguishes an error tag
   * from a neutral one. jsdom has no CSS engine, so the computed colour cannot be read instead.
   */
  const tagFor = (text: string) => screen.getByText(text).closest(".UnityTag");

  async function render(result: ReportResult) {
    createObjectURL.mockClear();
    mockApi({
      [`POST ${RUNS_URL}`]: respond(404, { detail: "Not Found" }),
      [`POST ${REPORT_URL}`]: result,
      [`GET ${DOWNLOAD_URL}`]: { stub: "binary" },
    });
    await loadConfigForTests();
    const user = userEvent.setup();
    renderWithQuery(<ReportPanel presentation="20" program="AGN-151586" />);
    await generateReport(user);
    return user;
  }

  it("grades a High overall risk as an error and a Low comparator as a success", async () => {
    await render(withResult({ risk: RISK_HIGH }));

    expect(await screen.findByText("Provisional mix-up risk:")).toBeVisible();
    expect(tagFor("High")).toHaveClass("UnityTag_kindError");
    expect(tagFor("Low")).toHaveClass("UnityTag_kindSuccess");
    // The rationale is the substance of the grade, so it must be on screen beside it.
    expect(
      screen.getByText("Same vial size and an indistinguishable cap shade.")
    ).toBeInTheDocument();
  });

  it("falls back to a neutral grade for a level it has no colour for", async () => {
    /**
     * LOOKS WRONG, characterised rather than corrected: `riskKind` recognises exactly "High" / "Med" /
     * "Low", but `TakenColor.risk` in `api/types.ts` is typed `"High" | "Medium" | "Low"` — so the
     * engine's own longer spelling falls through to `neutral` and a Medium risk renders in the same
     * grey as an unknown one. The word is still shown, so nothing is lost silently, but the colour
     * stops carrying the grade. Reported upstream.
     */
    await render(
      withResult({ risk: { ...RISK_HIGH, overall_risk: "Medium" } })
    );

    expect(await screen.findByText("Medium")).toBeInTheDocument();
    expect(tagFor("Medium")).toHaveClass("UnityTag_kindNeutral");
    expect(tagFor("Medium")).not.toHaveClass("UnityTag_kindWarning");
  });

  it("renders no risk section at all when the run assessed no risk", async () => {
    await render(withResult({ risk: null }));

    // The report itself still arrived, so the panel must not look empty.
    expect(
      await screen.findByText(/ALL CHECKS PASSED — generated PSA report for/)
    ).toBeInTheDocument();
    expect(screen.queryByText("Provisional mix-up risk:")).toBeNull();
  });

  it("shows the grade without a comparator table when there was nothing to compare", async () => {
    // A product with no co-located, same-family neighbour: the grade is still an answer, and `note`
    // is where the engine explains why the table is empty.
    await render(
      withResult({
        risk: {
          overall_risk: "Low",
          n_comparators: 0,
          comparators: [],
          informational: [],
          note: "No co-located same-family comparators were found.",
        },
      })
    );

    expect(await screen.findByText("Provisional mix-up risk:")).toBeVisible();
    expect(tagFor("Low")).toHaveClass("UnityTag_kindSuccess");
    expect(
      screen.getByText("No co-located same-family comparators were found.")
    ).toBeInTheDocument();
    // An empty table with only headings would imply an assessment that did not happen.
    expect(screen.queryByRole("columnheader")).toBeNull();
    expect(screen.queryByText(/informational only \(not assessed/)).toBeNull();
  });

  it("lists the other products the engine considered a possible match", async () => {
    await render(
      withResult({
        alternatives: [
          [0.71, "AGN-151607", "GemibotA"],
          [0.44, "ABBV-383", "Etentamig"],
        ],
      })
    );

    // Programme code plus name, in the engine's own ranked order — the score itself is not shown.
    expect(
      await screen.findByText(
        "Other possible matches: AGN-151607 (GemibotA), ABBV-383 (Etentamig)"
      )
    ).toBeInTheDocument();
  });

  it("says what the product was identified by", async () => {
    await render(withResult({ reason: "code-in-filename-not-in-catalogue" }));

    // The wording matters: "(new product)" is what tells the assessor the code is not on the sheet.
    expect(
      await screen.findByText(
        "Matched by: product code in the filename (new product)."
      )
    ).toBeInTheDocument();
  });

  it("stays silent about a reason code it has no wording for", async () => {
    // `workflow.identify()` can grow new codes; an untranslated one must not surface as a raw slug.
    await render(withResult({ reason: "some-future-reason-code" }));

    expect(
      await screen.findByText(/ALL CHECKS PASSED — generated PSA report for/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Matched by:/)).toBeNull();
    expect(screen.queryByText(/some-future-reason-code/)).toBeNull();
  });

  it("omits the run log when the engine produced none", async () => {
    await render(withResult({}));

    expect(
      await screen.findByText(/ALL CHECKS PASSED — generated PSA report for/)
    ).toBeInTheDocument();
    // An empty accordion labelled "Run log" would promise something it cannot show.
    expect(screen.queryByText("Run log")).toBeNull();
    // No log ⇒ no data-source claim either way; the panel must not guess one.
    expect(screen.queryByText("Data source: live Smartsheet ✓")).toBeNull();
    expect(screen.queryByText(/Live Smartsheet is not configured/)).toBeNull();
  });

  it("still offers the report when it was produced but some checks did not pass", async () => {
    /**
     * `verify_ok: false` with `output_exists: true` is a real outcome: the .docx exists and is worth
     * reading, the verifier just could not confirm every field. Hiding the download would strand the
     * assessor with a file they cannot reach; claiming ALL CHECKS PASSED would be a false statement.
     */
    const user = await render(
      withResult({ verify_ok: false, output_exists: true })
    );

    expect(
      await screen.findByText(/some checks did not pass \(see the run log\)/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/ALL CHECKS PASSED/)).toBeNull();
    expect(screen.queryByText(/the report file was not produced/)).toBeNull();

    await user.click(
      screen.getByRole("button", { name: /Download PSA report/ })
    );
    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
  });

  it("spells out a comparator that nothing distinguishes", async () => {
    /**
     * The worst case the assessment can report: zero distinguishing attributes and no shared site
     * recorded. Both cells fall back to words rather than rendering blank — an empty "Distinguished by"
     * cell reads as "not assessed", which is the opposite of what it means here.
     */
    await render(
      withResult({
        risk: {
          overall_risk: "High",
          n_comparators: 1,
          comparators: [
            {
              label: "ABBV-1480",
              risk_level: "High",
              n_distinguishing: 0,
              shared_sites: [],
              distinguishing: [],
            },
          ],
        },
      })
    );

    expect(await screen.findByText("ABBV-1480")).toBeInTheDocument();
    expect(screen.getByText("none — looks alike")).toBeInTheDocument();
    expect(screen.getByText("-")).toBeInTheDocument();
  });

  it("reports the comparator count the engine sent, not a re-count of the rows", async () => {
    // The table is capped at six rows while the count describes the whole assessment, so the two are
    // deliberately allowed to disagree — a re-count would understate the scope of the report.
    await render(
      withResult({
        risk: {
          overall_risk: "Low",
          comparators: RISK_HIGH.comparators,
        },
      })
    );

    // `n_comparators` absent ⇒ 0, not a guess from the one row rendered below it.
    expect(
      await screen.findByText(
        /0 manufacturing co-located, same-family comparators — matches the report/
      )
    ).toBeInTheDocument();
    expect(screen.getByText("AGN-151607 (GemibotA)")).toBeInTheDocument();
  });

  it("shows a validation answer as information, not as a failure", async () => {
    // 'message' is the engine answering a question about the input; 'needs_override' (asserted above)
    // is a failure to identify the product. They must not read the same.
    await render(
      withResult({
        status: "message",
        message: "That presentation has no cap recorded yet.",
        log: "Data source: LIVE API",
      })
    );

    expect(
      await screen.findByText("That presentation has no cap recorded yet.")
    ).toBeInTheDocument();
    // The banner still reports the data source — the run did read the sheet before refusing.
    expect(screen.getByText("Data source: live Smartsheet ✓")).toBeVisible();
    // Nothing was generated, so neither the verify sentence nor a download may appear.
    expect(screen.queryByText(/ALL CHECKS PASSED/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Download PSA report/ })
    ).toBeNull();
  });
});
