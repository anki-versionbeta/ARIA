import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import { IsoRangePicker } from "./iso-range-picker";

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

const OUTLINE = {
  doc_title: "ISO 20417:2021",
  total_pages: 42,
  toc: [
    { level: 1, title: "4 General requirements", page: 5 },
    { level: 2, title: "4.1 Marking", page: 6 },
    { level: 2, title: "4.2 Accompanying information", page: 7 },
  ],
};

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset().mockResolvedValue(OUTLINE);
  post.mockReset().mockResolvedValue({ id: "d1", status: "queued" });
});

describe("IsoRangePicker", () => {
  it("lists every section from the outline", async () => {
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    expect(
      await screen.findByText("4.2 Accompanying information")
    ).toBeInTheDocument();
  });

  it("shows how large the document is", async () => {
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    expect(
      await screen.findByText(/3 sections · 42 pages/)
    ).toBeInTheDocument();
  });

  it("posts the chosen indices, not the titles", async () => {
    const user = userEvent.setup();
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    await screen.findByRole("combobox", { name: /first section/i });

    await user.click(screen.getByRole("combobox", { name: /first section/i }));
    await user.click(screen.getByRole("option", { name: /4\.1 Marking/ }));
    await user.click(screen.getByRole("button", { name: /generate/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/iso/documents/d1/toc-range", {
        start_idx: 1,
        end_idx: 2,
      })
    );
  });

  it("refuses a range that ends before it starts", async () => {
    const user = userEvent.setup();
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    await screen.findByRole("combobox", { name: /last section/i });

    await user.click(screen.getByRole("combobox", { name: /last section/i }));
    await user.click(
      screen.getByRole("option", { name: /4 General requirements/ })
    );
    await user.click(screen.getByRole("combobox", { name: /first section/i }));
    await user.click(screen.getByRole("option", { name: /4\.2 Accompanying/ }));

    expect(screen.getByRole("button", { name: /generate/i })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows the server's own message for a rejected range", async () => {
    post.mockRejectedValue(
      new ApiError(400, { detail: "Invalid section range selected." })
    );
    const user = userEvent.setup();
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    await screen.findByRole("button", { name: /generate/i });

    await user.click(screen.getByRole("button", { name: /generate/i }));

    expect(
      await screen.findByText("Invalid section range selected.")
    ).toBeInTheDocument();
  });

  it("offers a copy when the caller does not own the document", async () => {
    post.mockRejectedValue(new ApiError(403, { can_fork: true }));
    const user = userEvent.setup();
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    await screen.findByRole("button", { name: /generate/i });

    await user.click(screen.getByRole("button", { name: /generate/i }));

    expect(await screen.findByText(/make your own copy/i)).toBeInTheDocument();
  });

  it("is read-only for a non-owner", async () => {
    render(wrap(<IsoRangePicker documentId="d1" canEdit={false} />));
    await screen.findByRole("combobox", { name: /first section/i });
    expect(screen.getByRole("button", { name: /generate/i })).toBeDisabled();
  });

  it("explains a failed outline load", async () => {
    get.mockRejectedValue(new ApiError(409, { detail: "not ready" }));
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    expect(
      await screen.findByText(/could not load the table of contents/i)
    ).toBeInTheDocument();
  });

  it("defaults to the whole document", async () => {
    const user = userEvent.setup();
    render(wrap(<IsoRangePicker documentId="d1" canEdit />));
    await screen.findByRole("button", { name: /generate/i });

    await user.click(screen.getByRole("button", { name: /generate/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/iso/documents/d1/toc-range", {
        start_idx: 0,
        end_idx: 2,
      })
    );
  });
});
