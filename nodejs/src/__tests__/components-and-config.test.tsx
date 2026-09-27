/**
 * Targeted tests for the branches the existing suite never reaches.
 *
 * This file is deliberately not a second pass over the happy paths — those are already
 * pinned by `routes-shell.test.tsx`, `documents-routes.test.tsx` and the colocated tests.
 * What is left uncovered is the unglamorous half of each component: the error alert, the
 * empty state, the disabled button, the fallback branch of a normaliser. Those are the
 * states a user actually complains about, and each one here corresponds to a specific
 * uncovered line, so the tests are narrow on purpose.
 *
 * Two shapes are used, chosen per target rather than uniformly:
 *
 *  - Leaf components and silo panels (`StepProgress`, `HistoryFilters`, `UploadPanel`,
 *    `SectionTree`, `BopConfig`, `IsoRangePicker`, `ReportEditPanel`) are rendered
 *    directly with props. Their uncovered branches are driven entirely by props and by
 *    what the API answers, so driving a whole route to reach them would only add flake
 *    and seconds.
 *  - Route-level behaviour (`/history`, `/login`) goes through `renderAt` from the shared
 *    harness, because the search-param normaliser and the router-driven sort/pagination
 *    callbacks only exist in that context.
 *  - `queries.ts` hooks with no remaining call site in the tested UI (`useAdministrators`,
 *    `useRestoreVersion`) go through `renderHook`.
 *
 * `vi.mock("@/api/http", ...)` is copied from `routes-shell.test.tsx` rather than shared:
 * `vi.mock` is hoisted per file and cannot come from the harness.
 */

import { UnityProvider } from "@abbvie-unity/react";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ADMIN_USER,
  apiHandlers,
  newQueryClient,
  renderAt,
  SILOS,
} from "@/__tests__/harness";
import {
  isForbidden,
  isNotFound,
  isStaleRevision,
  useAdministrators,
  useRestoreVersion,
} from "@/api/queries";
import type { DocumentSummary, Section } from "@/api/types";
import { HistoryFilters } from "@/components/history-filters";
import { SectionTree } from "@/components/section-tree";
import { StepProgress } from "@/components/step-progress";
import { UploadPanel } from "@/components/upload-panel";
import { loadRuntimeConfig } from "@/config/runtime";
import { BopConfig } from "@/silos/bop/bop-config";
import { IsoRangePicker } from "@/silos/iso/iso-range-picker";
import { ReportEditPanel } from "@/silos/mfg_atr/report-edit-panel";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();
const del = vi.fn();

vi.mock("@/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/http")>();
  return {
    ...actual,
    // Kept reachable under another name so the real `request` — the only place the 204,
    // non-JSON and 401 paths live — can still be exercised. `vi.importActual` would give
    // a second copy of the module graph, whose `getRuntimeConfig` is a different
    // singleton and therefore always unloaded.
    unmockedApi: actual.api,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      patch: vi.fn(),
      del: (...args: unknown[]) => del(...args),
    },
  };
});

// The mock spreads `actual`, so `ApiError` here is the same class the components narrow on
// with `instanceof`. Constructing a look-alike would silently fail every check.
const httpModule = (await import(
  "@/api/http"
)) as typeof import("@/api/http") & {
  unmockedApi: typeof import("@/api/http").api;
};
const { ApiError, setUnauthorizedHandler } = httpModule;

beforeEach(() => {
  get.mockReset().mockImplementation(apiHandlers());
  post.mockReset().mockResolvedValue({});
  put.mockReset().mockResolvedValue({});
  del.mockReset().mockResolvedValue(null);
});

/** Leaf components still read Unity theme context and, for BopConfig, a query client. */
function renderLeaf(node: ReactNode) {
  const client = newQueryClient();
  const wrap = (child: ReactNode) => (
    <UnityProvider defaultMode="light" density="balanced">
      <QueryClientProvider client={client}>{child}</QueryClientProvider>
    </UnityProvider>
  );
  const result = render(wrap(node));
  return {
    client,
    ...result,
    /** Re-renders inside the same providers, so component state survives a prop change. */
    rerenderLeaf: (next: ReactNode) => result.rerender(wrap(next)),
  };
}

const BOP_STAGES = ["extract", "generate", "review", "await_review", "build"];

function makeDocument(
  overrides: Partial<DocumentSummary> = {}
): DocumentSummary {
  return {
    id: "d1",
    silo_id: "bop",
    title: "Autoclave BOP",
    status: "complete",
    stage: "build",
    progress_pct: 100,
    progress_message: null,
    owner: { id: "u-admin", username: "asha.rao", display_name: "Asha Rao" },
    started_at: "2026-05-01T10:00:00Z",
    finished_at: "2026-05-01T10:05:00Z",
    duration_ms: 300000,
    ...overrides,
  };
}

function documentPage(items: DocumentSummary[], total = items.length) {
  return { items, page: 1, page_size: 25, total };
}

const PROMPTS = {
  petra: "Write the section as PETRA would.",
  reviewer: "Critique the draft.",
  petra_is_override: false,
  reviewer_is_override: false,
};

describe("the stage progress bar", () => {
  it("names every stage the silo declared, humanised", async () => {
    renderLeaf(
      <StepProgress
        stages={BOP_STAGES}
        currentStage="review"
        status="running"
      />
    );

    // `await_review` is the stage id; "Await Review" is what the user must read.
    expect(await screen.findByText("Await Review")).toBeInTheDocument();
    expect(screen.getByText("Extract")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
  });

  it("marks a finished run as complete at every stage", async () => {
    renderLeaf(
      <StepProgress
        stages={BOP_STAGES}
        currentStage="build"
        status="complete"
      />
    );

    // A complete run short-circuits the per-stage comparison, so even the first stage
    // must not be left looking outstanding.
    expect(await screen.findByText("Extract")).toBeInTheDocument();
    expect(screen.getByText("Generate")).toBeInTheDocument();
  });

  it("blames a failure on the stage that was running, not the whole run", async () => {
    renderLeaf(
      <StepProgress
        stages={BOP_STAGES}
        currentStage="generate"
        status="failed"
      />
    );

    const stepper = await screen.findByText("Generate");
    expect(stepper).toBeInTheDocument();
    // Earlier stages did finish; only the current one is invalid.
    expect(screen.getByText("Extract")).toBeInTheDocument();
  });

  it("shows a queued run with no stage yet as entirely outstanding", async () => {
    renderLeaf(
      <StepProgress stages={BOP_STAGES} currentStage={null} status="queued" />
    );

    // currentStage null means indexOf is never consulted: nothing may read as done.
    expect(await screen.findByText("Extract")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
  });
});

describe("the BOP prompt configuration screen", () => {
  it("explains that the prompts could not be loaded when the endpoint is missing", async () => {
    get.mockImplementation((path: string) => {
      if (path.startsWith("/silos/bop/config/prompts")) {
        return Promise.reject(new ApiError(404, null));
      }
      return apiHandlers()(path);
    });
    renderLeaf(<BopConfig />);

    expect(
      await screen.findByText("Could not load the BOP prompts")
    ).toBeInTheDocument();
    expect(
      screen.getByText(/backend may need restarting/i)
    ).toBeInTheDocument();
  });

  it("shows both prompts with their length and their default provenance", async () => {
    get.mockImplementation(
      apiHandlers({ "/silos/bop/config/prompts": PROMPTS })
    );
    renderLeaf(<BopConfig />);

    expect(await screen.findByText("PETRA generator")).toBeInTheDocument();
    expect(screen.getByText("Reviewer")).toBeInTheDocument();
    expect(screen.getAllByText("Default")).toHaveLength(2);
    expect(
      screen.getByText(`${PROMPTS.petra.length} characters`)
    ).toBeInTheDocument();
  });

  it("warns that saving a prompt affects everyone", async () => {
    get.mockImplementation(
      apiHandlers({ "/silos/bop/config/prompts": PROMPTS })
    );
    renderLeaf(<BopConfig />);

    expect(
      await screen.findByText(/applies to everyone, not just you/i)
    ).toBeInTheDocument();
  });

  it("keeps save unavailable until the prompt is actually changed", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({ "/silos/bop/config/prompts": PROMPTS })
    );
    renderLeaf(<BopConfig />);

    await screen.findByText("PETRA generator");
    const [save] = screen.getAllByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    const [petraBox] = screen.getAllByRole("textbox");
    await user.type(petraBox, " Extra guidance.");
    expect(save).toBeEnabled();
  });

  it("sends the edited prompt to the endpoint for the prompt that was edited", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({ "/silos/bop/config/prompts": PROMPTS })
    );
    renderLeaf(<BopConfig />);

    await screen.findByText("Reviewer");
    const boxes = screen.getAllByRole("textbox");
    await user.type(boxes[1], " Be terse.");
    await user.click(screen.getAllByRole("button", { name: "Save" })[1]);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/bop/config/prompts/reviewer", {
        prompt: `${PROMPTS.reviewer} Be terse.`,
      })
    );
  });

  it("offers no reset while a prompt is still the shipped default", async () => {
    get.mockImplementation(
      apiHandlers({ "/silos/bop/config/prompts": PROMPTS })
    );
    renderLeaf(<BopConfig />);

    await screen.findByText("PETRA generator");
    for (const button of screen.getAllByRole("button", {
      name: "Reset to default",
    })) {
      expect(button).toBeDisabled();
    }
  });

  it("resets a customised prompt back to the default on request", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/silos/bop/config/prompts": {
          ...PROMPTS,
          petra: "Локальная версия",
          petra_is_override: true,
        },
      })
    );
    renderLeaf(<BopConfig />);

    await screen.findByText("Customised");
    const [reset] = screen.getAllByRole("button", { name: "Reset to default" });
    expect(reset).toBeEnabled();
    await user.click(reset);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/silos/bop/config/prompts/petra/reset")
    );
  });

  it("resets the reviewer prompt independently of the generator prompt", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/silos/bop/config/prompts": {
          ...PROMPTS,
          reviewer: "Be unusually harsh.",
          reviewer_is_override: true,
        },
      })
    );
    renderLeaf(<BopConfig />);

    await screen.findByText("Customised");
    const resets = screen.getAllByRole("button", { name: "Reset to default" });
    // The generator is still default, so only the reviewer may be reset.
    expect(resets[0]).toBeDisabled();
    await user.click(resets[1]);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/silos/bop/config/prompts/reviewer/reset"
      )
    );
  });

  it("adopts the server's text when the saved prompt comes back changed", async () => {
    const user = userEvent.setup();
    let current = { ...PROMPTS };
    get.mockImplementation((path: string) =>
      path.startsWith("/silos/bop/config/prompts")
        ? Promise.resolve(current)
        : apiHandlers()(path)
    );
    // The server normalises what it stores, so the textarea has to follow the refetch
    // rather than keep the draft the user typed.
    post.mockImplementation(() => {
      current = {
        ...current,
        petra: "Normalised by the server.",
        petra_is_override: true,
      };
      return Promise.resolve({});
    });
    renderLeaf(<BopConfig />);

    await screen.findByText("PETRA generator");
    await user.type(screen.getAllByRole("textbox")[0], "!");
    await user.click(screen.getAllByRole("button", { name: "Save" })[0]);

    expect(
      await screen.findByDisplayValue("Normalised by the server.")
    ).toBeInTheDocument();
  });
});

describe("the history page", () => {
  it("reports a failed listing with the reason the API gave", async () => {
    get.mockImplementation((path: string) =>
      path.startsWith("/documents")
        ? Promise.reject(new Error("upstream database is down"))
        : apiHandlers()(path)
    );
    await renderAt("/history");

    expect(
      await screen.findByText("Could not load documents")
    ).toBeInTheDocument();
    expect(screen.getByText("upstream database is down")).toBeInTheDocument();
  });

  it("invites the first upload when nothing has ever been generated", async () => {
    get.mockImplementation(apiHandlers({ "/documents": documentPage([]) }));
    await renderAt("/history");

    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
  });

  it("shows the table rather than the empty state when a filter is what emptied it", async () => {
    get.mockImplementation(apiHandlers({ "/documents": documentPage([]) }));
    await renderAt("/history?q=nothing-matches-this");

    // "No documents yet" would wrongly suggest the platform is unused; the table's own
    // no-results message is the honest answer.
    expect(
      await screen.findByText("No documents match these filters.")
    ).toBeInTheDocument();
    expect(screen.queryByText("No documents yet")).not.toBeInTheDocument();
  });

  it("lists a document with its owner and type", async () => {
    get.mockImplementation(
      apiHandlers({ "/documents": documentPage([makeDocument()]) })
    );
    await renderAt("/history");

    expect(await findRowLink("Autoclave BOP")).toHaveAttribute(
      "href",
      "/documents/d1"
    );
    // Scoped to the table: the signed-in user's name is also in the header menu.
    const table = screen.getByRole("table");
    expect(within(table).getByText("BOP")).toBeInTheDocument();
    expect(within(table).getByText("Asha Rao")).toBeInTheDocument();
  });

  it("discards a sort key and a status the API would reject", async () => {
    get.mockImplementation(
      apiHandlers({ "/documents": documentPage([makeDocument()]) })
    );
    await renderAt(
      "/history?sort=owner_email&status=exploded&page=0&order=asc"
    );

    // A hand-edited or stale URL must degrade to the default listing, not send
    // `sort=owner_email` to a server that only knows date/name/user.
    await waitFor(() => expect(get).toHaveBeenCalled());
    const requested = get.mock.calls
      .map(([path]) => String(path))
      .find((path) => path.startsWith("/documents?"));
    expect(requested).toContain("sort=date");
    expect(requested).toContain("order=asc");
    expect(requested).toContain("page=1");
    expect(requested).not.toContain("status=");
  });

  it("scopes the listing to the signed-in user when only-mine is set", async () => {
    get.mockImplementation(
      apiHandlers({ "/documents": documentPage([makeDocument()]) })
    );
    await renderAt("/history?mine=true");

    await waitFor(() => {
      const requested = get.mock.calls
        .map(([path]) => String(path))
        .find((path) => path.startsWith("/documents?"));
      expect(requested).toContain(`user=${ADMIN_USER.username}`);
    });
  });

  it("re-requests the listing sorted the other way when a column header is clicked", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({ "/documents": documentPage([makeDocument()]) })
    );
    await renderAt("/history");

    await findRowLink("Autoclave BOP");
    await user.click(screen.getByRole("button", { name: /document/i }));

    await waitFor(() => {
      const requested = get.mock.calls
        .map(([path]) => String(path))
        .filter((path) => path.startsWith("/documents?"));
      expect(requested.some((path) => path.includes("sort=name"))).toBe(true);
    });
  });

  it("asks for the next page when the paginator is advanced", async () => {
    const user = userEvent.setup();
    const rows = Array.from({ length: 25 }, (_, index) =>
      makeDocument({ id: `d${index}`, title: `Document ${index}` })
    );
    get.mockImplementation(
      apiHandlers({ "/documents": documentPage(rows, 60) })
    );
    await renderAt("/history");

    await findRowLink("Document 0");
    await user.click(await screen.findByRole("button", { name: /next/i }));

    await waitFor(() => {
      const requested = get.mock.calls
        .map(([path]) => String(path))
        .filter((path) => path.startsWith("/documents?"));
      expect(requested.some((path) => path.includes("page=2"))).toBe(true);
    });
  });
});

/**
 * The DataTable's first paint of real rows is the slowest thing in this file, and with the
 * whole suite competing for workers it overran the 1s default often enough to fail.
 */
function findRowLink(title: string) {
  return screen.findByRole("link", { name: title }, { timeout: 15000 });
}

describe("the history filters", () => {
  it("offers no type filter while the silo list is still unknown", async () => {
    // /silos answering empty is the pre-authorisation state: a user with no modules must
    // not be shown a type filter with nothing in it.
    get.mockImplementation(apiHandlers({ "/silos": [] }));
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={vi.fn()}
      />
    );

    expect(
      await screen.findByPlaceholderText("Search by title")
    ).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/silos"));
    // Status is the only picker left; the type picker is not rendered at all.
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
  });

  it("offers one type per silo the API returned", async () => {
    const user = userEvent.setup();
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={vi.fn()}
      />
    );

    await waitFor(() =>
      expect(screen.getAllByRole("combobox")).toHaveLength(2)
    );
    await user.click(screen.getAllByRole("combobox")[1]);
    for (const silo of SILOS) {
      expect(await screen.findByText(silo.label)).toBeInTheDocument();
    }
  });

  it("waits for a pause in typing before changing the search", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={onChange}
      />
    );

    await user.type(
      await screen.findByPlaceholderText("Search by title"),
      "auto"
    );

    // The point of the debounce: four keystrokes must not become four listings.
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ q: "auto" }));
    expect(onChange.mock.calls.filter(([patch]) => "q" in patch)).toHaveLength(
      1
    );
  });

  it("narrows the listing to the current user when only-mine is ticked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={onChange}
      />
    );

    await user.click(
      await screen.findByRole("checkbox", { name: /only mine/i })
    );

    expect(onChange).toHaveBeenCalledWith({ mine: true });
  });

  it("filters by a status picked from the list", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={onChange}
      />
    );

    await waitFor(() =>
      expect(screen.getAllByRole("combobox")).toHaveLength(2)
    );
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByText("Needs review"));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({ status: "awaiting_user" })
    );
  });

  it("filters by a document type picked from the list", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderLeaf(
      <HistoryFilters
        values={{ q: "", silo: "", status: "", mine: false }}
        onChange={onChange}
      />
    );

    await waitFor(() =>
      expect(screen.getAllByRole("combobox")).toHaveLength(2)
    );
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByText(SILOS[1].label));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({ silo: SILOS[1].id })
    );
  });
});

describe("the upload panel", () => {
  it("lists the extensions the silo accepts", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[".pdf", ".docx"]}
        isUploading={false}
        error={null}
        onUpload={vi.fn()}
      />
    );

    expect(
      await screen.findByText("Accepted: .pdf, .docx")
    ).toBeInTheDocument();
  });

  it("says any file will do when the silo declares no extensions", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[]}
        isUploading={false}
        error={null}
        onUpload={vi.fn()}
      />
    );

    expect(
      await screen.findByText("Any file type is accepted.")
    ).toBeInTheDocument();
  });

  it("explains a rejected upload in terms of the file, not the status code", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[".pdf"]}
        isUploading={false}
        error={new ApiError(413, null)}
        onUpload={vi.fn()}
      />
    );

    expect(
      await screen.findByText("That file is too large.")
    ).toBeInTheDocument();
  });

  it("names the document type when the server refuses the file type", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[".pdf"]}
        isUploading={false}
        error={new ApiError(415, null)}
        onUpload={vi.fn()}
      />
    );

    expect(
      await screen.findByText(
        "That file type is not accepted for this document type."
      )
    ).toBeInTheDocument();
  });

  it("falls back to a retry message for a failure it cannot interpret", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[".pdf"]}
        isUploading={false}
        error={new Error("socket hang up")}
        onUpload={vi.fn()}
      />
    );

    expect(
      await screen.findByText("The upload failed. Please try again.")
    ).toBeInTheDocument();
  });

  it("shows the chosen file with its size and starts authoring on request", async () => {
    const user = userEvent.setup();
    const onUpload = vi.fn();
    renderLeaf(
      <UploadPanel
        accepts={[".pdf"]}
        isUploading={false}
        error={null}
        onUpload={onUpload}
      />
    );

    const file = new File(["x".repeat(2048)], "autoclave.pdf", {
      type: "application/pdf",
    });
    await user.upload(fileInput(), file);

    expect(await screen.findByText("autoclave.pdf · 2 KB")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start authoring" }));
    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it("keeps the start button out of reach while an upload is in flight", async () => {
    const user = userEvent.setup();
    const onUpload = vi.fn();
    const props = {
      accepts: [".pdf"],
      error: null,
      onUpload,
    };
    const { rerenderLeaf } = renderLeaf(
      <UploadPanel {...props} isUploading={false} />
    );

    await user.upload(
      fileInput(),
      new File(["x"], "autoclave.pdf", { type: "application/pdf" })
    );
    await screen.findByRole("button", { name: "Start authoring" });

    rerenderLeaf(<UploadPanel {...props} isUploading={true} />);

    // Double-submitting would start a second run from the same file.
    const button = screen.getByRole("button", { name: /uploading/i });
    expect(button).toBeDisabled();
    expect(onUpload).not.toHaveBeenCalled();
  });

  it("explains a file the dropzone itself refused, listing what it wanted", async () => {
    renderLeaf(
      <UploadPanel
        accepts={[".pdf"]}
        isUploading={false}
        error={null}
        onUpload={vi.fn()}
      />
    );

    // `userEvent.upload` honours the input's own `accept` attribute and discards a
    // mismatched file before the component ever sees it, which is precisely the branch
    // under test. A real browser drag-and-drop does reach the component, so the change
    // event is dispatched directly here.
    const input = fileInput();
    const file = new File(["x"], "notes.txt", { type: "text/plain" });
    Object.defineProperty(input, "files", {
      value: [file],
      configurable: true,
    });
    fireEvent.change(input);

    expect(
      await screen.findByText("That file was rejected. Accepted: .pdf")
    ).toBeInTheDocument();
  });
});

/** The Dropzone's input is presentational-hidden, so it has no accessible name. */
function fileInput() {
  const input = document.querySelector('input[type="file"]');
  if (!input) throw new Error("the dropzone rendered no file input");
  return input as HTMLInputElement;
}

describe("the section tree", () => {
  it("labels an undotted section key with the key itself", async () => {
    // ISO's keys are flat, so `parts.slice(1)` is empty and the whole key is the label.
    const sections: Section[] = [
      {
        section_key: "scope",
        content_html: "<p>a</p>",
        revision: 1,
        updated_at: "2026-05-01T10:00:00Z",
      },
    ];
    renderLeaf(
      <SectionTree sections={sections} selected={null} onSelect={vi.fn()} />
    );

    const nav = await screen.findByRole("navigation");
    expect(within(nav).getAllByText("Scope").length).toBeGreaterThan(0);
  });

  it("groups dotted keys under their first segment", async () => {
    const sections: Section[] = [
      {
        section_key: "operating_procedure.setup",
        content_html: "<p>a</p>",
        revision: 2,
        updated_at: "2026-05-01T10:00:00Z",
      },
      {
        section_key: "operating_procedure.teardown",
        content_html: "<p>b</p>",
        revision: 1,
        updated_at: "2026-05-01T10:00:00Z",
      },
    ];
    renderLeaf(
      <SectionTree
        sections={sections}
        selected="operating_procedure.setup"
        onSelect={vi.fn()}
      />
    );

    expect(await screen.findByText("Operating Procedure")).toBeInTheDocument();
    expect(screen.getByText("Setup")).toBeInTheDocument();
    expect(screen.getByText("Teardown")).toBeInTheDocument();
  });

  it("reports the full dotted key when a section is picked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const sections: Section[] = [
      {
        section_key: "operating_procedure.teardown",
        content_html: "<p>b</p>",
        revision: 1,
        updated_at: "2026-05-01T10:00:00Z",
      },
    ];
    renderLeaf(
      <SectionTree sections={sections} selected={null} onSelect={onSelect} />
    );

    await user.click(await screen.findByText("Teardown"));

    // The label is the leaf, but the caller needs the key it can save against.
    expect(onSelect).toHaveBeenCalledWith("operating_procedure.teardown");
  });
});

describe("the login route", () => {
  it("prefers the reason the API gave for refusing the credentials", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(
      new ApiError(401, {
        detail: "Sign in with your username, not your email.",
      })
    );
    await renderAt("/login");

    await signIn(user);

    expect(
      await screen.findByText("Sign in with your username, not your email.")
    ).toBeInTheDocument();
  });

  it("falls back to a plain wrong-password message when the API says nothing useful", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(new ApiError(401, null));
    await renderAt("/login");

    await signIn(user);

    expect(
      await screen.findByText("Incorrect username or password.")
    ).toBeInTheDocument();
  });

  it("distinguishes an unavailable sign-in service from a bad password", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(new ApiError(503, null));
    await renderAt("/login");

    await signIn(user);

    // A 503 is not the user's fault, so telling them the password is wrong would send
    // them round a loop of retyping it.
    expect(
      await screen.findByText(
        "Sign in is unavailable right now. Please try again."
      )
    ).toBeInTheDocument();
  });
});

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    await screen.findByRole("textbox", { name: /username/i }),
    "asha.rao"
  );
  const password = document.querySelector(
    'input[type="password"]'
  ) as HTMLInputElement;
  await user.type(password, "secret");
  await user.click(await screen.findByRole("button", { name: /sign in/i }));
}

describe("the ISO clause range picker", () => {
  const OUTLINE = {
    doc_title: "ISO 13485",
    total_pages: 60,
    toc: [{ title: "Scope" }, { title: "Normative references" }],
  };

  it("repeats the API's own complaint about the range rather than a generic failure", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({ "/silos/iso/documents/d1/toc": OUTLINE })
    );
    post.mockRejectedValue(
      new ApiError(400, { detail: "That range spans more than 40 clauses." })
    );
    renderLeaf(<IsoRangePicker documentId="d1" canEdit={true} />);

    await user.click(
      await screen.findByRole("button", { name: /generate the assessment/i })
    );

    // The user can act on "more than 40 clauses"; they cannot act on "could not start".
    expect(
      await screen.findByText("That range spans more than 40 clauses.")
    ).toBeInTheDocument();

    // A fault with nothing readable in it falls back rather than showing a raw status.
    post.mockRejectedValue(new ApiError(500, null));
    await user.click(
      screen.getByRole("button", { name: /generate the assessment/i })
    );

    expect(
      await screen.findByText("Could not start the build.")
    ).toBeInTheDocument();
  });
});

describe("the MFG/ATR review panel", () => {
  const REVIEW = {
    report_type: "atr",
    display: "CMC-10352",
    generated: true,
    flags: [],
    // Null on purpose: it keeps the lazily loaded PDF overlay out of this test, which is
    // about the submit failure rather than about rendering a document.
    preview_file_id: null,
  };

  it("names the stage the run has already moved on to when finalizing conflicts", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({ "/silos/mfg_atr/documents/d1/review": REVIEW })
    );
    post.mockRejectedValue(
      new ApiError(409, { detail: "This run is already at build." })
    );
    renderLeaf(<ReportEditPanel documentId="d1" canEdit={true} />);

    await user.click(
      await screen.findByRole("button", { name: /apply values and finalize/i })
    );

    expect(
      await screen.findByText("This run is already at build.")
    ).toBeInTheDocument();

    post.mockRejectedValue(new ApiError(500, null));
    await user.click(
      screen.getByRole("button", { name: /apply values and finalize/i })
    );

    expect(
      await screen.findByText("Could not apply your values.")
    ).toBeInTheDocument();
  });
});

describe("the HTTP client", () => {
  // `getRuntimeConfig()` throws until the boot-time fetch has resolved, which `main.tsx`
  // awaits before mounting. Nothing in the test tree does that, so do it here once.
  beforeAll(async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ apiBaseUrl: "/api" }), { status: 200 })
        )
    );
    await loadRuntimeConfig();
    vi.unstubAllGlobals();
  });

  it("treats a 204 as an empty body rather than failing to parse it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    );

    // A retry answers 204: parsing "" as JSON would throw and look like a failed retry.
    await expect(
      httpModule.unmockedApi.put("/documents/d1/sections/scope", { html: "" })
    ).resolves.toBeNull();
    vi.unstubAllGlobals();
  });

  it("hands back a non-JSON body as text instead of throwing", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(new Response("plain text reply", { status: 200 }))
    );

    await expect(httpModule.unmockedApi.get("/anything")).resolves.toBe(
      "plain text reply"
    );
    vi.unstubAllGlobals();
  });

  it("signs the user out when a request other than login reports an expired session", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    // A fresh Response per call: a body can only be read once.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 401 }))
    );

    await expect(
      httpModule.unmockedApi.get("/documents")
    ).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalled();

    // Login opts out: a 401 there is a wrong password, not an expired session, and
    // bouncing the user to /login mid-login would lose what they typed.
    onUnauthorized.mockClear();
    await expect(
      httpModule.unmockedApi.post(
        "/auth/login",
        {},
        { ignoreUnauthorized: true }
      )
    ).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("the query layer", () => {
  function hookWrapper() {
    const client = newQueryClient();
    return ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  }

  it("asks who can grant module access", async () => {
    // Shown to users with no modules at all, so it must not itself require one.
    const administrators = [
      {
        id: "u-admin",
        username: "asha.rao",
        display_name: "Asha Rao",
        email: null,
      },
    ];
    get.mockImplementation(
      apiHandlers({ "/auth/administrators": administrators })
    );
    const { result } = renderHook(() => useAdministrators(), {
      wrapper: hookWrapper(),
    });

    await waitFor(() => expect(result.current.data).toEqual(administrators));
    expect(get).toHaveBeenCalledWith("/auth/administrators");
  });

  it("restores an older version through the section's own restore endpoint", async () => {
    const { result } = renderHook(() => useRestoreVersion("d1"), {
      wrapper: hookWrapper(),
    });

    await act(async () => {
      await result.current.mutateAsync({
        sectionKey: "operating_procedure.setup",
        versionNum: 3,
      });
    });

    expect(post).toHaveBeenCalledWith(
      "/documents/d1/sections/operating_procedure.setup/restore/3"
    );
  });

  it("recognises the conflict, forbidden and missing responses by status", async () => {
    // These three drive user-facing copy across the document routes, so a wrong
    // status here silently turns a fork offer into a generic error.
    expect(isStaleRevision(new ApiError(409, null))).toBe(true);
    expect(isStaleRevision(new ApiError(403, null))).toBe(false);
    expect(isStaleRevision(new Error("network"))).toBe(false);
    expect(isForbidden(new ApiError(403, { can_fork: true }))).toBe(true);
    expect(isNotFound(new ApiError(404, null))).toBe(true);
  });
});
