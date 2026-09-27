import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import { MfgAtrNewDocument } from "./mfg-atr-new-document";

const get = vi.fn();
const post = vi.fn();
const navigate = vi.fn();

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

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
}));

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  get
    .mockReset()
    .mockResolvedValue({ request_ids: ["CMC-10352", "CMC-10400"] });
  post.mockReset().mockResolvedValue({ id: "d1" });
  navigate.mockReset();
});

describe("MfgAtrNewDocument", () => {
  it("queues an ATR run from a typed identifier", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.type(
      screen.getByRole("textbox", { name: /cmc request id/i }),
      "10352"
    );
    await user.click(screen.getByRole("button", { name: /generate atr/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/mfg_atr/runs", {
        report_type: "atr",
        identifier: "10352",
        source: "live",
        fixture_path: undefined,
      })
    );
  });

  it("goes to the new document so the user can watch it run", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.type(
      screen.getByRole("textbox", { name: /cmc request id/i }),
      "CMC-10352"
    );
    await user.click(screen.getByRole("button", { name: /generate atr/i }));

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith({
        to: "/documents/$documentId",
        params: { documentId: "d1" },
      })
    );
  });

  it("asks for an identifier rather than posting an empty one", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.click(screen.getByRole("button", { name: /generate atr/i }));

    expect(
      await screen.findByText(/please enter a cmc request id/i)
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("switches to the batch id vocabulary for MFGR", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.click(screen.getByRole("button", { name: /manufacturing/i }));

    expect(
      screen.getByRole("textbox", { name: /batch id/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /generate mfgr/i })
    ).toBeInTheDocument();
  });

  it("clears a typed id when the report type changes", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.type(
      screen.getByRole("textbox", { name: /cmc request id/i }),
      "CMC-10352"
    );
    await user.click(screen.getByRole("button", { name: /manufacturing/i }));

    // A CMC request id cannot validate as a batch id, so carrying it across would only
    // produce a 400 the user did not ask for.
    expect(screen.getByRole("textbox", { name: /batch id/i })).toHaveValue("");
  });

  it("offers the fixture source only for ATR", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    expect(
      screen.getByRole("button", { name: /fixture \(offline\)/i })
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /manufacturing/i }));

    expect(
      screen.queryByRole("button", { name: /fixture \(offline\)/i })
    ).not.toBeInTheDocument();
  });

  it("sends the fixture path when the fixture source is chosen", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.click(
      screen.getByRole("button", { name: /fixture \(offline\)/i })
    );
    await user.type(
      screen.getByRole("textbox", { name: /cmc request id/i }),
      "10352"
    );
    await user.click(screen.getByRole("button", { name: /generate atr/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/mfg_atr/runs", {
        report_type: "atr",
        identifier: "10352",
        source: "fixture",
        fixture_path: "tests/fixtures/cmc10352.json",
      })
    );
  });

  it("does not query the pickers until asked", async () => {
    render(wrap(<MfgAtrNewDocument />));
    // Opening the page must not hit the warehouse — the source cached these for an hour
    // for exactly this reason.
    await screen.findByRole("button", { name: /browse existing ids/i });
    expect(get).not.toHaveBeenCalled();
  });

  it("lists the live request ids once the picker is opened", async () => {
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.click(
      screen.getByRole("button", { name: /browse existing ids/i })
    );

    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/silos/mfg_atr/request-ids")
    );
  });

  it("tells the user to type an id when the warehouse is unreachable", async () => {
    get.mockResolvedValue({ request_ids: [], reachable: false });
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.click(
      screen.getByRole("button", { name: /browse existing ids/i })
    );

    expect(
      await screen.findByText(/picker is unavailable/i)
    ).toBeInTheDocument();
  });

  it("shows the server's own wording for a rejected identifier", async () => {
    post.mockRejectedValue(
      new ApiError(400, { detail: "Enter a CMC request id such as CMC-10352." })
    );
    const user = userEvent.setup();
    render(wrap(<MfgAtrNewDocument />));

    await user.type(
      screen.getByRole("textbox", { name: /cmc request id/i }),
      "nonsense"
    );
    await user.click(screen.getByRole("button", { name: /generate atr/i }));

    expect(
      await screen.findByText("Enter a CMC request id such as CMC-10352.")
    ).toBeInTheDocument();
  });
});
