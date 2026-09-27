import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import { MfgAtrNewDocument } from "@/silos/mfg_atr/mfg-atr-new-document";

/**
 * The colocated mfg-atr-new-document.test.tsx exercises the ATR path (typed request id,
 * request-id picker, rejected identifier). This file covers the parts that were untested:
 * the MFGR batch path, the "browse" picker for batches, the Enter-key shortcut, the
 * pick-then-load flow, and the non-400 fault message.
 */
const get = vi.fn();
const post = vi.fn();
const navigate = vi.fn();

vi.mock("@/api/http", async () => {
  const actual = await vi.importActual<typeof import("@/api/http")>("@/api/http");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
    },
  };
});

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
}));

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

async function switchToMfgr(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /manufacturing \(mfgr\)/i }));
}

beforeEach(() => {
  get.mockReset().mockResolvedValue({ batch_ids: ["BAX000584", "BAX000701"] });
  post.mockReset().mockResolvedValue({ id: "d1" });
  navigate.mockReset();
});

describe("MfgAtrNewDocument — MFGR path", () => {
  it("queues an MFGR run from a typed batch id", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.type(
      screen.getByRole("textbox", { name: /batch id/i }),
      "BAX000584"
    );
    await user.click(screen.getByRole("button", { name: /generate mfgr/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/mfg_atr/runs", {
        report_type: "mfgr",
        identifier: "BAX000584",
        source: "live",
        fixture_path: undefined,
      })
    );
  });

  it("navigates to the queued MFGR document", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.type(
      screen.getByRole("textbox", { name: /batch id/i }),
      "BAX000584"
    );
    await user.click(screen.getByRole("button", { name: /generate mfgr/i }));

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith({
        to: "/documents/$documentId",
        params: { documentId: "d1" },
      })
    );
  });

  it("asks for a batch id rather than posting an empty one", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.click(screen.getByRole("button", { name: /generate mfgr/i }));

    expect(await screen.findByText(/please enter a batch id/i)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("browses the live batch ids from the batch-ids endpoint", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.click(screen.getByRole("button", { name: /browse existing ids/i }));

    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/silos/mfg_atr/batch-ids")
    );
  });

  it("tells the user to type an id when the batch picker is unreachable", async () => {
    get.mockResolvedValue({ batch_ids: [], reachable: false });
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.click(screen.getByRole("button", { name: /browse existing ids/i }));

    expect(await screen.findByText(/picker is unavailable/i)).toBeInTheDocument();
  });

  it("asks for a selection when Load is pressed with nothing picked", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.click(screen.getByRole("button", { name: /browse existing ids/i }));
    await user.click(await screen.findByRole("button", { name: /load/i }));

    expect(
      await screen.findByText(/pick one from the dropdown first/i)
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("submits on Enter without needing the button", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.type(
      screen.getByRole("textbox", { name: /batch id/i }),
      "BAX000701{Enter}"
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/silos/mfg_atr/runs",
        expect.objectContaining({ report_type: "mfgr", identifier: "BAX000701" })
      )
    );
  });

  it("names the HTTP status for a non-400 fault", async () => {
    post.mockRejectedValue(new ApiError(500, { detail: "boom" }));
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));
    await switchToMfgr(user);

    await user.type(
      screen.getByRole("textbox", { name: /batch id/i }),
      "BAX000584"
    );
    await user.click(screen.getByRole("button", { name: /generate mfgr/i }));

    expect(
      await screen.findByText(/could not start the report \(http 500\)/i)
    ).toBeInTheDocument();
  });
});
