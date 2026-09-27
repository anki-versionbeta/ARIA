import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import { ReportEditPanel } from "./report-edit-panel";

const get = vi.fn();
const post = vi.fn();

vi.mock("@/api/http", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/http")>("@/api/http");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
    },
  };
});

// Normally loaded once at app bootstrap; reading it unloaded throws by design.
vi.mock("@/config/runtime", () => ({
  getRuntimeConfig: () => ({ apiBaseUrl: "/api" }),
}));

/**
 * The overlay editor is stubbed rather than exercised: it loads pdfjs-dist and renders to a
 * canvas, neither of which jsdom provides. What this panel is responsible for is the seam —
 * whatever the overlay reports is what gets submitted — so the stub reports a known map.
 */
const FIELD_VALUES = { regulatory: "Yes", hqc_assessment: "meets" };

vi.mock("./pdf-overlay-editor", () => ({
  PdfOverlayEditor: ({
    ref,
    src,
  }: {
    ref?: { current: unknown };
    src: string;
  }) => {
    if (ref) ref.current = { getFieldValues: () => FIELD_VALUES };
    return <div data-testid="overlay" data-src={src} />;
  },
}));

const REVIEW = {
  report_type: "atr",
  display: "CMC-10352",
  generated: true,
  flags: [
    {
      severity: "warn",
      area: "results",
      message: "Missing value for pH value",
    },
  ],
  preview_file_id: "f1",
};

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset().mockResolvedValue(REVIEW);
  post.mockReset().mockResolvedValue({ id: "d1", status: "queued" });
});

describe("ReportEditPanel", () => {
  it("names the report being reviewed", async () => {
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    expect(await screen.findByText(/CMC-10352/)).toBeInTheDocument();
  });

  it("surfaces the review flags from the generate checkpoint", async () => {
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    expect(
      await screen.findByText("Missing value for pH value")
    ).toBeInTheDocument();
  });

  it("points the overlay at the attached preview file", async () => {
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    const overlay = await screen.findByTestId("overlay");
    expect(overlay.dataset.src).toContain("/documents/d1/files/f1");
  });

  it("submits the values the overlay reports", async () => {
    const user = userEvent.setup();
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    await user.click(await screen.findByRole("button", { name: /apply/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/silos/mfg_atr/documents/d1/field-values",
        { field_values: FIELD_VALUES }
      )
    );
  });

  it("still offers to finalize when no PDF is attached", async () => {
    get.mockResolvedValue({ ...REVIEW, preview_file_id: null });
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));

    expect(await screen.findByText(/nothing to edit/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply/i })).toBeEnabled();
  });

  it("shows the server's own message when the run has moved on", async () => {
    post.mockRejectedValue(
      new ApiError(409, {
        detail:
          "Run is complete at stage finalize; it is not waiting for your values",
      })
    );
    const user = userEvent.setup();
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    await user.click(await screen.findByRole("button", { name: /apply/i }));

    expect(
      await screen.findByText(/not waiting for your values/)
    ).toBeInTheDocument();
  });

  it("offers a copy when the caller does not own the document", async () => {
    post.mockRejectedValue(new ApiError(403, { can_fork: true }));
    const user = userEvent.setup();
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    await user.click(await screen.findByRole("button", { name: /apply/i }));

    expect(await screen.findByText(/make your own copy/i)).toBeInTheDocument();
  });

  it("is read-only for a non-owner", async () => {
    render(wrap(<ReportEditPanel documentId="d1" canEdit={false} />));
    expect(
      await screen.findByRole("button", { name: /apply/i })
    ).toBeDisabled();
  });

  it("names ATR's manual fields on an ATR run", async () => {
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    expect(await screen.findByText(/HQC Specification ID/)).toBeInTheDocument();
  });

  it("names MFGR's manual fields on an MFGR run", async () => {
    // Same stage and the same overlay serve both, but they are different documents —
    // showing ATR's field names on an MFGR run would simply be wrong.
    get.mockResolvedValue({
      ...REVIEW,
      report_type: "mfgr",
      display: "BAX000584",
    });
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));

    expect(await screen.findByText(/clinical phase/)).toBeInTheDocument();
    expect(screen.queryByText(/HQC Specification ID/)).not.toBeInTheDocument();
  });

  it("explains a failed load", async () => {
    get.mockRejectedValue(new ApiError(404, { detail: "Not found" }));
    render(wrap(<ReportEditPanel documentId="d1" canEdit />));
    expect(
      await screen.findByText(/could not load the report for review/i)
    ).toBeInTheDocument();
  });
});
