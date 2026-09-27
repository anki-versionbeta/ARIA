/**
 * The four routes a run passes through: Home → /new → /silos/:id/new → /documents/:id.
 *
 * These pages are almost entirely *state selection* — the document page alone renders six
 * mutually exclusive shapes off one `status` field (queued/running progress, parked for
 * review, parked but silo-owned review, failed with retry, complete with sections and
 * files, and not-yours read-only) — so the tests are organised one per state rather than
 * one per component. Asserting the state is the only way to reach the branch.
 *
 * Driven through the real router over a memory history (see `harness.tsx` for why mocking
 * `createFileRoute` does not work). Two consequences worth knowing before editing:
 *
 *  - `await renderAt(path)` is load-bearing. A nested route paints nothing until the match
 *    resolves, so without the await the container is empty and no query ever fires.
 *  - every assertion is `await screen.findBy...`, because the data arrives a tick later.
 *
 * `apiHandlers` matches a path by equality *or prefix*, so overrides are ordered
 * most-specific-first: `/documents/d1/sections` must precede `/documents/d1`, otherwise the
 * detail payload would answer the sections request too.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { apiHandlers, PLAIN_USER, renderAt, SILOS } from "@/__tests__/harness";
import { ApiError } from "@/api/http";
import type { DocumentDetail, Section } from "@/api/types";
import { loadRuntimeConfig } from "@/config/runtime";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();
const del = vi.fn();

vi.mock("@/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/http")>();
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      patch: vi.fn(),
      del: (...args: unknown[]) => del(...args),
    },
  };
});

/**
 * The file download is a plain anchor at `${apiBaseUrl}/documents/...`, and the base URL is
 * fetched at boot rather than baked in, so `getRuntimeConfig()` throws until
 * `loadRuntimeConfig()` has resolved. `main.tsx` awaits it before mounting; nothing in the
 * test setup does, so the document page's files table would take the whole route down with
 * it. Load it once here rather than mocking the module, so the href asserted below is
 * assembled by the real code.
 */
beforeAll(async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ apiBaseUrl: "http://aria.test/api" }),
    })
  );
  await loadRuntimeConfig();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  get.mockReset().mockImplementation(apiHandlers());
  post.mockReset().mockResolvedValue({});
  put.mockReset().mockResolvedValue({});
  del.mockReset().mockResolvedValue(null);
});

/** A complete run owned by the signed-in admin. Spread and overridden per state. */
function documentDetail(
  overrides: Partial<DocumentDetail> = {}
): DocumentDetail {
  return {
    id: "d1",
    silo_id: "bop",
    title: "Filling Line 3 — Bill of Process",
    status: "complete",
    stage: null,
    progress_pct: 100,
    progress_message: null,
    owner: { id: "u-admin", username: "asha.rao", display_name: "Asha Rao" },
    started_at: "2026-03-04T09:15:00Z",
    finished_at: "2026-03-04T09:41:00Z",
    duration_ms: 1_560_000,
    error_message: null,
    files: [],
    forked_from_run_id: null,
    can_edit: true,
    ...overrides,
  };
}

const SECTIONS: Section[] = [
  {
    section_key: "bop.setup.equipment",
    content_html: "<p>Autoclave qualified at 121 °C.</p>",
    revision: 3,
    updated_at: "2026-03-04T09:40:00Z",
  },
];

/**
 * Answer the document page's requests for one document, most-specific path first.
 *
 * The trailing-slash entry is not redundant: without it the section *list* would also
 * answer `.../sections/:key/versions`, and a `Section` has no `created_at`, so the version
 * dropdown formatted `undefined` as a date and took the route down through its error
 * boundary. Empty version history keeps the editor to the live revision, which is what
 * `section-editor.test.tsx` already exercises in detail.
 *
 * `sections` defaults to none — what a run that has generated nothing looks like.
 */
function documentPageHandlers(
  detail: DocumentDetail,
  sections: Section[] = [],
  extra: Record<string, unknown> = {}
) {
  return apiHandlers({
    ...extra,
    [`/documents/${detail.id}/sections/`]: [],
    [`/documents/${detail.id}/sections`]: sections,
    [`/documents/${detail.id}`]: detail,
  });
}

describe("the home page", () => {
  it("greets the signed-in user by their given name", async () => {
    await renderAt("/");

    // givenName() strips the surname, so the heading is the first name only even though
    // /auth/me returns "Asha Rao".
    expect(
      await screen.findByRole("heading", { name: "Welcome, Asha" })
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/AI-Driven Report Intelligence/i)
    ).toBeInTheDocument();
  });

  it("drops the name rather than greeting someone by a login id", async () => {
    // display_name falls back to the username when the directory read fails, and
    // givenName() returns "" for anything unusable, so the greeting goes nameless.
    get.mockImplementation(
      apiHandlers({}, { ...PLAIN_USER, display_name: "" })
    );
    await renderAt("/");

    expect(
      await screen.findByRole("heading", { name: "Welcome" })
    ).toBeInTheDocument();
  });

  it("describes what ARIA does before asking the user to start", async () => {
    await renderAt("/");

    expect(await screen.findByText("Reads the source")).toBeInTheDocument();
    expect(await screen.findByText("Drafts the document")).toBeInTheDocument();
    expect(await screen.findByText("Keeps you in control")).toBeInTheDocument();
  });

  it("sends Get Started to the one page that lists the document types", async () => {
    const user = userEvent.setup();
    const { router } = await renderAt("/");

    await user.click(
      await screen.findByRole("button", { name: /get started/i })
    );

    // Asserting the destination rather than the destination's heading. This test is about
    // where the button goes, and waiting for /new to paint failed on roughly one
    // full-suite run in three: a real router navigation plus that page's own queries does
    // not reliably finish inside the 1s default that `findBy*` allows. What /new actually
    // renders is covered by "offers a card per enabled type" below.
    await waitFor(() => expect(router.state.location.pathname).toBe("/new"));
  });
});

describe("choosing a document type", () => {
  it("offers a card per enabled type, saying what each one takes as input", async () => {
    await renderAt("/new");

    expect(await screen.findByText("Equipment BOP")).toBeInTheDocument();
    expect(
      await screen.findByText("Starts from .pdf or .docx you upload.")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("ISO Applicability Assessment")
    ).toBeInTheDocument();
  });

  it("says a type needs no upload when it accepts no file extensions", async () => {
    get.mockImplementation(
      apiHandlers({
        "/silos": [
          { id: "mfg_atr", label: "ATR / MFGR", accepts: [], stages: [] },
        ],
      })
    );
    await renderAt("/new");

    expect(
      await screen.findByText("Starts from an identifier — nothing to upload.")
    ).toBeInTheDocument();
  });

  it("hides the search box while there is only one type to search through", async () => {
    get.mockImplementation(apiHandlers({ "/silos": [SILOS[0]] }));
    await renderAt("/new");

    await screen.findByText("Equipment BOP");
    expect(
      screen.queryByPlaceholderText("Search document types")
    ).not.toBeInTheDocument();
  });

  it("narrows the list to the types matching what was typed", async () => {
    const user = userEvent.setup();
    await renderAt("/new");

    await user.type(
      await screen.findByPlaceholderText("Search document types"),
      "iso"
    );

    expect(
      await screen.findByText("ISO Applicability Assessment")
    ).toBeInTheDocument();
    expect(screen.queryByText("Equipment BOP")).not.toBeInTheDocument();
  });

  it("says so rather than showing an empty grid when nothing matches the search", async () => {
    const user = userEvent.setup();
    await renderAt("/new");

    await user.type(
      await screen.findByPlaceholderText("Search document types"),
      "nonesuch"
    );

    expect(
      await screen.findByText(/No document type matches/)
    ).toBeInTheDocument();
  });

  it("reports a failure to load the types instead of looking empty", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/silos") return Promise.reject(new ApiError(500, null));
      return apiHandlers()(path);
    });
    await renderAt("/new");

    expect(
      await screen.findByText("Could not load the available document types")
    ).toBeInTheDocument();
  });

  it("blames the platform, not the caller, when a user with modules sees no types", async () => {
    // PLAIN_USER holds "bop", so an empty list here can only mean nothing is mounted.
    get.mockImplementation(apiHandlers({ "/silos": [] }, PLAIN_USER));
    await renderAt("/new");

    expect(
      await screen.findByText("No document types are enabled right now")
    ).toBeInTheDocument();
  });

  it("takes a Go button to the upload page for that type", async () => {
    const user = userEvent.setup();
    const { router } = await renderAt("/new");

    await user.click(
      await screen.findByRole("button", { name: "Go — Equipment BOP" })
    );

    // Destination asserted as the route, not as that page's heading -- same reason as the
    // Get Started test above: a real navigation plus the upload page's own queries does
    // not reliably paint inside the 1s `findBy*` default under full-suite load. The
    // upload page's own contents are covered by the tests in the next describe block.
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/silos/bop/new")
    );
  });
});

describe("a caller who has been granted no modules", () => {
  const noModules = { ...PLAIN_USER, modules: [] };
  const requestHandlers = (extra: Record<string, unknown> = {}) =>
    apiHandlers(
      {
        "/silos": [],
        "/modules": [
          { id: "bop", label: "Equipment BOP" },
          { id: "iso", label: "ISO Applicability Assessment" },
        ],
        ...extra,
      },
      noModules
    );

  it("is offered a request form rather than being told the platform is empty", async () => {
    get.mockImplementation(requestHandlers());
    await renderAt("/new");

    expect(
      await screen.findByText("You do not have access to any modules yet")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Choose at least one module.")
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /send request/i })
    ).toBeDisabled();
  });

  it("sends the modules ticked and the reason typed", async () => {
    const user = userEvent.setup();
    get.mockImplementation(requestHandlers());
    await renderAt("/new");

    await user.click(await screen.findByLabelText("Equipment BOP"));
    expect(await screen.findByText("Requesting 1 module.")).toBeInTheDocument();

    await user.click(
      await screen.findByLabelText("ISO Applicability Assessment")
    );
    expect(
      await screen.findByText("Requesting 2 modules.")
    ).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(/authoring ATR reports/i),
      "  joining the BOP team  "
    );
    await user.click(screen.getByRole("button", { name: /send request/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/access-requests", {
        modules: ["bop", "iso"],
        // Trimmed, and dropped entirely when blank — the API treats "" as a note.
        note: "joining the BOP team",
      })
    );
  });

  it("omits the note entirely when none was written", async () => {
    const user = userEvent.setup();
    get.mockImplementation(requestHandlers());
    await renderAt("/new");

    await user.click(await screen.findByLabelText("Equipment BOP"));
    await user.click(screen.getByRole("button", { name: /send request/i }));

    // `undefined`, not "": the note is optional and an empty string would be stored as a
    // blank justification.
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/access-requests", {
        modules: ["bop"],
        note: undefined,
      })
    );
  });

  it("waits for the answer before deciding which panel to show", async () => {
    get.mockImplementation(
      requestHandlers({ "/access-requests/mine": () => new Promise(() => {}) })
    );
    const { container } = await renderAt("/new");

    // Neither the form nor the "already asked" alert: showing the form first would let
    // somebody file a second request over the one already in the queue.
    await waitFor(() => expect(container.querySelector(".h-9")).toBeTruthy());
    expect(
      screen.queryByText("You do not have access to any modules yet")
    ).not.toBeInTheDocument();
  });

  it("untickes a module back off the request", async () => {
    const user = userEvent.setup();
    get.mockImplementation(requestHandlers());
    await renderAt("/new");

    const bop = await screen.findByLabelText("Equipment BOP");
    await user.click(bop);
    await user.click(bop);

    expect(
      await screen.findByText("Choose at least one module.")
    ).toBeInTheDocument();
  });

  it("shows what an already-filed request asked for instead of a second form", async () => {
    get.mockImplementation(
      requestHandlers({
        "/access-requests/mine": {
          id: "ar-1",
          user_id: noModules.id,
          username: noModules.username,
          display_name: noModules.display_name,
          email: noModules.email,
          modules: ["bop", "unknown_silo"],
          note: null,
          status: "pending",
          created_at: "2026-03-01T10:00:00Z",
          decided_at: null,
          decided_by: null,
          decision_note: null,
        },
      })
    );
    await renderAt("/new");

    expect(
      await screen.findByText("Your access request is waiting for approval")
    ).toBeInTheDocument();
    expect(await screen.findByText("Equipment BOP")).toBeInTheDocument();
    // labelFor falls back to the raw id, so a module the platform no longer publishes
    // still appears rather than rendering a blank tag.
    expect(await screen.findByText("unknown_silo")).toBeInTheDocument();
  });

  it("surfaces the API's own reason when the request cannot be filed", async () => {
    const user = userEvent.setup();
    get.mockImplementation(requestHandlers());
    post.mockRejectedValue(
      new ApiError(409, { detail: "You already have an open request." })
    );
    await renderAt("/new");

    await user.click(await screen.findByLabelText("Equipment BOP"));
    await user.click(screen.getByRole("button", { name: /send request/i }));

    expect(
      await screen.findByText("You already have an open request.")
    ).toBeInTheDocument();
  });
});

describe("starting a new run for a silo", () => {
  it("names the type and lists the extensions it accepts", async () => {
    await renderAt("/silos/bop/new");

    expect(
      await screen.findByRole("heading", { name: "New Equipment BOP" })
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Accepted: .pdf, .docx")
    ).toBeInTheDocument();
  });

  it("refuses an id that is not an available type, dark or unknown alike", async () => {
    await renderAt("/silos/not_a_silo/new");

    expect(
      await screen.findByText(/"not_a_silo" is not an available document type/)
    ).toBeInTheDocument();
  });

  it("waits rather than guessing while the type list is still loading", async () => {
    get.mockImplementation(
      apiHandlers({ "/silos": () => new Promise(() => {}) })
    );
    const { container } = await renderAt("/silos/bop/new");

    // The Unity Spinner exposes no accessible name, so the sized wrapper is the only
    // handle on it. What matters is that the "not an available type" alert is *not*
    // rendered while the list is unknown.
    await waitFor(() => expect(container.querySelector(".h-9")).toBeTruthy());
    expect(
      screen.queryByText(/is not an available document type/)
    ).not.toBeInTheDocument();
  });

  it("queues a run for the chosen file and follows it to the document", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/documents/d9/sections/": [],
        "/documents/d9/sections": [],
        "/documents/d9": documentDetail({ id: "d9", status: "queued" }),
      })
    );
    // The upload is multipart, so `createDocument` uses `fetch` directly rather than the
    // JSON client the rest of these tests mock — the browser must set the boundary.
    const upload = vi.fn().mockResolvedValue({
      ok: true,
      status: 201,
      text: async () => JSON.stringify({ id: "d9" }),
    });
    vi.stubGlobal("fetch", upload);

    const { container, router } = await renderAt("/silos/bop/new");
    await screen.findByText("Accepted: .pdf, .docx");

    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement;
    await user.upload(
      input,
      new File(["source"], "line3-source.pdf", { type: "application/pdf" })
    );
    await user.click(
      await screen.findByRole("button", { name: "Start authoring" })
    );

    await waitFor(() =>
      expect(upload).toHaveBeenCalledWith(
        "http://aria.test/api/silos/bop/documents",
        expect.objectContaining({ method: "POST", credentials: "include" })
      )
    );
    // Navigated to the queued run, so the work continues without this page open.
    //
    // Asserted as the route rather than by waiting for the document page to paint. That
    // page is a separate chunk whose import plus its own queries do not finish
    // predictably under full-suite load, which made this the last flaky test in the
    // suite. What it renders is covered by the "a document that is still running" and
    // "a document that finished" blocks above.
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/documents/d9")
    );
    vi.unstubAllGlobals();
  });

  it("hands over to the silo's own screen when it does not start from a file", async () => {
    get.mockImplementation(
      apiHandlers({
        "/silos": [
          ...SILOS,
          { id: "mfg_atr", label: "ATR / MFGR", accepts: [], stages: [] },
        ],
      })
    );
    await renderAt("/silos/mfg_atr/new");

    // mfg_atr replaces the shared upload panel: it takes an identifier, not an upload.
    expect(
      screen.queryByText("Upload a source document")
    ).not.toBeInTheDocument();
  });
});

describe("a document that is still being generated", () => {
  it("shows the live stage and progress while the run is in flight", async () => {
    get.mockImplementation(
      documentPageHandlers(
        documentDetail({
          status: "running",
          stage: "generate",
          progress_pct: 40,
          progress_message: "Drafting section 3 of 9",
        })
      )
    );
    await renderAt("/documents/d1");

    expect(await screen.findByText("In progress")).toBeInTheDocument();
    expect(await screen.findByText("running · generate")).toBeInTheDocument();
    expect(
      await screen.findByText("Drafting section 3 of 9")
    ).toBeInTheDocument();
  });

  it("renders the whole page from the URL so a reload rebuilds it", async () => {
    get.mockImplementation(
      documentPageHandlers(documentDetail({ status: "queued" }))
    );
    await renderAt("/documents/d1");

    expect(
      await screen.findByText("Filling Line 3 — Bill of Process")
    ).toBeInTheDocument();
    // The silo, owner and start time all come from the one detail request keyed by the id
    // in the path — nothing is held in a variable that a refresh would lose.
    expect(await screen.findByText(/^BOP · Asha Rao ·/)).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/documents/d1"));
  });

  it("shows only a spinner until the document itself has loaded", async () => {
    get.mockImplementation(
      apiHandlers({ "/documents/d1": () => new Promise(() => {}) })
    );
    const { container } = await renderAt("/documents/d1");

    await waitFor(() => expect(container.querySelector(".h-9")).toBeTruthy());
    expect(screen.queryByText(/All documents/)).not.toBeInTheDocument();
  });
});

describe("a document parked for a person to review", () => {
  const parked = documentDetail({
    status: "awaiting_user",
    stage: "await_review",
    progress_pct: 80,
  });

  it("asks for the review and offers to resume the run", async () => {
    get.mockImplementation(documentPageHandlers(parked, SECTIONS));
    await renderAt("/documents/d1");

    expect(await screen.findByText("Waiting for you")).toBeInTheDocument();
    expect(
      await screen.findByText("Ready for your review")
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Build document" })
    ).toBeEnabled();
  });

  it("resumes the run with an empty checkpoint when the generic panel builds", async () => {
    const user = userEvent.setup();
    get.mockImplementation(documentPageHandlers(parked, SECTIONS));
    await renderAt("/documents/d1");

    await user.click(
      await screen.findByRole("button", { name: "Build document" })
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/documents/d1/build", {})
    );
  });

  it("tells a non-owner to copy the document rather than repeating the 403", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      documentPageHandlers({ ...parked, can_edit: true }, SECTIONS)
    );
    post.mockRejectedValue(new ApiError(403, { can_fork: true }));
    await renderAt("/documents/d1");

    await user.click(
      await screen.findByRole("button", { name: "Build document" })
    );

    expect(
      await screen.findByText(
        /You do not own this document, so it cannot be built/
      )
    ).toBeInTheDocument();
  });

  it("falls back to a generic message when the build fails for another reason", async () => {
    const user = userEvent.setup();
    get.mockImplementation(documentPageHandlers(parked, SECTIONS));
    post.mockRejectedValue(new ApiError(500, null));
    await renderAt("/documents/d1");

    await user.click(
      await screen.findByRole("button", { name: "Build document" })
    );

    expect(
      await screen.findByText("Could not start the build.")
    ).toBeInTheDocument();
  });

  it("keeps a read-only caller from building someone else's document", async () => {
    get.mockImplementation(
      documentPageHandlers({ ...parked, can_edit: false }, SECTIONS)
    );
    await renderAt("/documents/d1");

    expect(
      await screen.findByRole("button", { name: "Build document" })
    ).toBeDisabled();
  });

  it("lets the silo replace the generic review panel entirely", async () => {
    // ISO parks to ask which clauses to assess, so a generic "Build document" would
    // complete the stage with no range at all. Its own screen takes over instead, and the
    // shared section editor is suppressed with it.
    get.mockImplementation(
      documentPageHandlers(
        { ...parked, id: "d1", silo_id: "iso", stage: "await_range" },
        SECTIONS
      )
    );
    await renderAt("/documents/d1");

    await screen.findByText("Waiting for you");
    expect(screen.queryByText("Ready for your review")).not.toBeInTheDocument();
    expect(screen.queryByText("Sections")).not.toBeInTheDocument();
  });
});

describe("a document that failed", () => {
  const failed = documentDetail({
    status: "failed",
    stage: "generate",
    error_message: "The source PDF has no extractable text.",
  });

  it("shows why it failed and offers to resume from the last good stage", async () => {
    get.mockImplementation(documentPageHandlers(failed));
    await renderAt("/documents/d1");

    expect(
      await screen.findByText("This document failed to generate")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("The source PDF has no extractable text.")
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/Retrying resumes from the last completed stage/)
    ).toBeInTheDocument();
  });

  it("retries the run rather than starting a second one", async () => {
    const user = userEvent.setup();
    get.mockImplementation(documentPageHandlers(failed));
    await renderAt("/documents/d1");

    await user.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/documents/d1/retry")
    );
  });

  it("still offers a retry when the worker died without saying why", async () => {
    get.mockImplementation(
      documentPageHandlers(
        documentDetail({ status: "failed", error_message: null })
      )
    );
    await renderAt("/documents/d1");

    expect(
      await screen.findByRole("button", { name: "Retry" })
    ).toBeInTheDocument();
    // No error_message means no alert — the status badge is the only signal.
    expect(
      screen.queryByText("This document failed to generate")
    ).not.toBeInTheDocument();
  });

  // Suspected defect: RetryPanel is rendered for a failed document regardless of
  // `can_edit`, unlike ReviewPanel which disables its button. A non-owner is offered a
  // Retry that can only 403, and the panel shows no error when it does. Correct behaviour
  // would be to disable it and point at the fork, as the review panel does. Pinned here.
  it("offers a non-owner a retry it cannot perform", async () => {
    get.mockImplementation(
      documentPageHandlers({ ...failed, can_edit: false })
    );
    await renderAt("/documents/d1");

    expect(await screen.findByRole("button", { name: "Retry" })).toBeEnabled();
  });
});

describe("a finished document", () => {
  it("lists the input and output files with a real download link each", async () => {
    get.mockImplementation(
      documentPageHandlers(
        documentDetail({
          files: [
            {
              id: "f1",
              kind: "input",
              filename: "line3-source.pdf",
              size_bytes: 2048,
              content_type: "application/pdf",
              created_at: "2026-03-04T09:15:00Z",
            },
            {
              id: "f2",
              kind: "output",
              filename: "line3-bop.docx",
              size_bytes: 3_145_728,
              content_type: null,
              created_at: "2026-03-04T09:41:00Z",
            },
          ],
        }),
        SECTIONS
      )
    );
    await renderAt("/documents/d1");

    // An anchor with `download`, not a click handler: the browser has to do the download.
    const output = await screen.findByRole("link", { name: "line3-bop.docx" });
    expect(output).toHaveAttribute("download", "line3-bop.docx");
    expect(output.getAttribute("href")).toContain("/documents/d1/files/f2");

    const row = output.closest("tr") as HTMLElement;
    expect(within(row).getByText("Output")).toBeInTheDocument();
    expect(within(row).getByText("3.0 MB")).toBeInTheDocument();
    expect(await screen.findByText("Input")).toBeInTheDocument();
    expect(await screen.findByText("2 KB")).toBeInTheDocument();
  });

  it("says the files table is empty rather than rendering headers over nothing", async () => {
    get.mockImplementation(documentPageHandlers(documentDetail()));
    await renderAt("/documents/d1");

    expect(
      await screen.findByText("No files are attached to this document yet.")
    ).toBeInTheDocument();
    expect(screen.queryByText("Size")).not.toBeInTheDocument();
  });

  it("opens the section editor on the generated sections", async () => {
    get.mockImplementation(documentPageHandlers(documentDetail(), SECTIONS));
    await renderAt("/documents/d1");

    expect(await screen.findByText("Sections")).toBeInTheDocument();
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/documents/d1/sections")
    );
  });

  it("says there is nothing to edit when the run produced no sections", async () => {
    get.mockImplementation(documentPageHandlers(documentDetail(), []));
    await renderAt("/documents/d1");

    expect(
      await screen.findByText("This document has no editable sections yet.")
    ).toBeInTheDocument();
  });

  it("links back to the original when the document is a copy", async () => {
    get.mockImplementation(
      documentPageHandlers(documentDetail({ forked_from_run_id: "d0" }))
    );
    await renderAt("/documents/d1");

    expect(
      await screen.findByRole("link", { name: "the original document" })
    ).toHaveAttribute("href", "/documents/d0");
  });
});

describe("a document owned by somebody else", () => {
  const theirs = documentDetail({
    can_edit: false,
    owner: {
      id: "u-other",
      username: "ben.carter",
      display_name: "Ben Carter",
    },
  });

  it("is read-only, and says whose it is and what to do about it", async () => {
    get.mockImplementation(documentPageHandlers(theirs, SECTIONS));
    await renderAt("/documents/d1");

    expect(await screen.findByText("Read-only")).toBeInTheDocument();
    expect(
      await screen.findByText(/Ben Carter owns this document/)
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/make your own copy to edit this document/i)
    ).toBeInTheDocument();
  });

  it("forks a copy and takes the caller to it, leaving the original alone", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        // The copy is a second document, so both ids are answered here. Its sub-resources
        // need entries of their own or the prefix match would hand the detail payload to
        // the section list.
        "/documents/d2/sections/": [],
        "/documents/d2/sections": SECTIONS,
        "/documents/d2": documentDetail({ id: "d2", forked_from_run_id: "d1" }),
        "/documents/d1/sections/": [],
        "/documents/d1/sections": SECTIONS,
        "/documents/d1": theirs,
      })
    );
    post.mockResolvedValue({ id: "d2" });
    await renderAt("/documents/d1");

    await user.click(
      await screen.findByRole("button", { name: "Create my own copy" })
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/documents/d1/fork")
    );
    // Navigated to the copy: it is editable, so the read-only banner is gone.
    await waitFor(() =>
      expect(screen.queryByText("Read-only")).not.toBeInTheDocument()
    );
    expect(
      await screen.findByRole("link", { name: "the original document" })
    ).toBeInTheDocument();
  });
});

describe("a document that cannot be loaded", () => {
  it("distinguishes a document that does not exist from a failure to read it", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/documents/nope")
        return Promise.reject(new ApiError(404, null));
      return apiHandlers()(path);
    });
    await renderAt("/documents/nope");

    expect(
      await screen.findByText("This document does not exist.")
    ).toBeInTheDocument();
  });

  it("reports a server-side failure without claiming the document is missing", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/documents/d1")
        return Promise.reject(new ApiError(500, null));
      return apiHandlers()(path);
    });
    await renderAt("/documents/d1");

    expect(
      await screen.findByText("Could not load this document.")
    ).toBeInTheDocument();
  });
});
