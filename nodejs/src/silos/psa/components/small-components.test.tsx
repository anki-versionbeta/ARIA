/**
 * The three read-only panels on the Cap Colour Selection page: the swatch grid, the products table under
 * it, and the per-site bar list.
 *
 * They are grouped in one file because they share one job and one failure mode. Each is a thin renderer
 * over a single query, and each has three answers that are NOT the happy path — still loading, the
 * request failed, the request succeeded and returned nothing. Those three read very differently to an
 * assessor: "no in-scope product uses this colour" is a finding, "could not read the analysis database"
 * is a broken environment, and a spinner is neither. Collapsing any of them into a blank area is the bug
 * these tests exist to catch, so every case asserts the actual wording on screen.
 *
 * Driven through the real `queries.ts` → `client.ts` → `fetch` chain via the shared harness, so a wrong
 * path or a wrong query string fails here rather than against a live server.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  paletteDatwyler,
  productsByColorBlue6043,
  siteCounts,
} from "../api/__fixtures__/live-payloads.gen";
import type { PaletteResult } from "../api/types";
import {
  type ApiMock,
  loadConfigForTests,
  mockApi,
  type Routes,
  renderWithQuery,
  respond,
} from "../test-support";
import { PaletteGrid } from "./palette-grid";
import { ProductsByColourTable } from "./products-by-colour-table";
import { SiteBarChart } from "./site-bar-chart";

const BASE = "/api/silos/psa";

/** Same reasoning as the other PSA files: jsdom plus module compile exceeds Vitest's 5 s default. */
const TIMEOUT = 30_000;

/**
 * The fetch mock has to be installed BEFORE the runtime config is loaded — `loadConfigForTests` fetches
 * `/config.json` through it, and jsdom cannot resolve a relative URL without it.
 */
async function mount(
  ui: React.ReactElement,
  routes: Routes
): Promise<{ api: ApiMock; user: ReturnType<typeof userEvent.setup> }> {
  const api = mockApi(routes);
  await loadConfigForTests();
  const user = userEvent.setup({ delay: null });
  renderWithQuery(ui);
  return { api, user };
}

describe("PaletteGrid", () => {
  it(
    "renders one pressable tile per palette colour",
    async () => {
      await mount(
        <PaletteGrid
          onSelectColor={() => undefined}
          selectedColor={null}
          vendor="Datwyler"
        />,
        { [`GET ${BASE}/palette`]: paletteDatwyler }
      );

      // The tile is a real button carrying aria-pressed, which is what makes the grid operable without
      // the Select that used to sit beside it.
      const tile = await screen.findByRole("button", { name: "Blue 6043" });
      expect(tile).toHaveAttribute("aria-pressed", "false");
      expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(
        paletteDatwyler.colors.length
      );
    },
    TIMEOUT
  );

  it(
    "marks only the selected colour as pressed, and clicking it again clears the selection",
    async () => {
      const onSelectColor = vi.fn();
      const { user } = await mount(
        <PaletteGrid
          onSelectColor={onSelectColor}
          selectedColor="Blue 6043"
          vendor="Datwyler"
        />,
        { [`GET ${BASE}/palette`]: paletteDatwyler }
      );

      expect(
        await screen.findByRole("button", { name: "Blue 6043", pressed: true })
      ).toBeInTheDocument();
      expect(screen.getAllByRole("button", { pressed: true })).toHaveLength(1);

      // null, not the name: re-clicking the pressed tile is the only way to dismiss the products table
      // now that the clearable Select is gone.
      await user.click(screen.getByRole("button", { name: "Blue 6043" }));
      expect(onSelectColor).toHaveBeenCalledWith(null);

      await user.click(screen.getByRole("button", { name: "Red 6055" }));
      expect(onSelectColor).toHaveBeenLastCalledWith("Red 6055");
    },
    TIMEOUT
  );

  it(
    "says the palette is empty rather than showing a blank area",
    async () => {
      const empty: PaletteResult = { ...paletteDatwyler, colors: [] };
      await mount(
        <PaletteGrid
          onSelectColor={() => undefined}
          selectedColor={null}
          vendor="West"
        />,
        { [`GET ${BASE}/palette`]: empty }
      );

      // Names the vendor, so the message is actionable — the other supplier's catalogue may be fine.
      expect(
        await screen.findByText(
          "No off-the-shelf West colours are in the palette."
        )
      ).toBeInTheDocument();
      expect(screen.queryAllByRole("button")).toHaveLength(0);
    },
    TIMEOUT
  );

  it(
    "surfaces the server's own reason when the palette cannot be loaded",
    async () => {
      await mount(
        <PaletteGrid
          onSelectColor={() => undefined}
          selectedColor={null}
          vendor="Datwyler"
        />,
        {
          [`GET ${BASE}/palette`]: respond(500, {
            detail: "The shipped cap palette could not be parsed.",
          }),
        }
      );

      // FastAPI's `detail`, not the component's fallback: the specific cause is what tells an admin what
      // to fix.
      expect(
        await screen.findByText("The shipped cap palette could not be parsed.")
      ).toBeInTheDocument();
      expect(screen.queryByRole("button")).toBeNull();
    },
    TIMEOUT
  );

  it(
    "falls back to its own wording when the failure carries no detail",
    async () => {
      await mount(
        <PaletteGrid
          onSelectColor={() => undefined}
          selectedColor={null}
          vendor="Datwyler"
        />,
        { [`GET ${BASE}/palette`]: respond(503, {}) }
      );

      expect(
        await screen.findByText(
          "Could not load the cap-colour palette. (HTTP 503)."
        )
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "announces the wait while the palette is in flight",
    async () => {
      // A never-resolving route: the pending branch is only observable while the promise is open.
      mockApi({});
      await loadConfigForTests();
      vi.stubGlobal(
        "fetch",
        vi.fn(() => new Promise<Response>(() => undefined))
      );
      renderWithQuery(
        <PaletteGrid
          onSelectColor={() => undefined}
          selectedColor={null}
          vendor="Datwyler"
        />
      );

      expect(screen.getByRole("status")).toHaveTextContent(
        "Loading the Datwyler palette…"
      );
    },
    TIMEOUT
  );
});

describe("ProductsByColourTable", () => {
  it(
    "renders nothing at all until a colour is picked",
    async () => {
      const { api } = await mount(
        <ProductsByColourTable colorName={null} vendor="Datwyler" />,
        { [`GET ${BASE}/products-by-color`]: productsByColorBlue6043 }
      );

      expect(screen.queryByRole("table")).toBeNull();
      expect(screen.queryByText(/Products using/)).toBeNull();
      // And no request was made — the query is disabled without a colour, so an idle page does not
      // hit the database.
      expect(api.calls).toEqual([]);
    },
    TIMEOUT
  );

  it(
    "lists the products on one exact vendor code, headed by that code",
    async () => {
      const { api } = await mount(
        <ProductsByColourTable colorName="Blue 6043" vendor="Datwyler" />,
        { [`GET ${BASE}/products-by-color`]: productsByColorBlue6043 }
      );

      // The heading renders in the pending state too, so it is the ROW that marks arrival.
      const row = productsByColorBlue6043[0];
      expect(await screen.findByText(row.product)).toBeInTheDocument();
      expect(screen.getByText("Products using Blue 6043")).toBeInTheDocument();
      expect(screen.getByText(row.contact)).toBeInTheDocument();
      expect(screen.getByText(row.mfr_sites)).toBeInTheDocument();
      // Spelled out in the heading even though the wire field is still `mfr_sites`.
      expect(
        screen.getByRole("columnheader", { name: "Manufacturing site(s)" })
      ).toBeInTheDocument();
      // The exact vendor code goes to the server, not the canonical hue — that is what makes the match
      // specific rather than "every blue".
      expect(api.calledPaths()).toContain(
        `GET ${BASE}/products-by-color?vendor=Datwyler&color=Blue+6043`
      );
    },
    TIMEOUT
  );

  it(
    "reports an empty result as a finding, keeping the heading",
    async () => {
      await mount(
        <ProductsByColourTable colorName="Green 6007" vendor="Datwyler" />,
        { [`GET ${BASE}/products-by-color`]: [] }
      );

      // "No product uses this" is the answer an assessor is looking for, so it must be stated — and the
      // heading has to stay, or the sentence has no subject.
      expect(
        await screen.findByText(
          "No in-scope product currently uses Green 6007."
        )
      ).toBeInTheDocument();
      expect(screen.getByText("Products using Green 6007")).toBeInTheDocument();
      expect(screen.queryByRole("table")).toBeNull();
    },
    TIMEOUT
  );

  it(
    "tells the user to refresh when the analysis database cannot be read",
    async () => {
      await mount(
        <ProductsByColourTable colorName="Blue 6043" vendor="Datwyler" />,
        { [`GET ${BASE}/products-by-color`]: respond(500, {}) }
      );

      // No `detail`, so the fallback runs — and the fallback is the one that carries the recovery step.
      expect(
        await screen.findByText(
          /click ↻ Refresh from Smartsheet, then try again/
        )
      ).toBeInTheDocument();
      expect(screen.getByText("Products using Blue 6043")).toBeInTheDocument();
      expect(screen.queryByRole("table")).toBeNull();
    },
    TIMEOUT
  );

  it(
    "prefers the server's own reason over the refresh advice",
    async () => {
      await mount(
        <ProductsByColourTable colorName="Blue 6043" vendor="Datwyler" />,
        {
          [`GET ${BASE}/products-by-color`]: respond(400, {
            detail: "Unknown vendor 'Datwyler'.",
          }),
        }
      );

      expect(
        await screen.findByText("Unknown vendor 'Datwyler'.")
      ).toBeInTheDocument();
      expect(screen.queryByText(/then try again/)).toBeNull();
    },
    TIMEOUT
  );
});

describe("SiteBarChart", () => {
  it(
    "renders one row per site, with the count readable as text",
    async () => {
      await mount(<SiteBarChart />, {
        [`GET ${BASE}/site-product-counts`]: siteCounts,
      });

      await waitFor(() => expect(screen.getByText("ABB")).toBeInTheDocument());
      expect(screen.getAllByRole("listitem")).toHaveLength(siteCounts.length);
      // A "site code" is sometimes a full postal address — `split_sites` collapses an address cell to one
      // node — so this must stay ONE row rather than being split on the commas.
      expect(
        screen.getByText("Allergan Pharmaceuticals, County Mayo, Westport")
      ).toBeInTheDocument();
      // The bar is decoration over text that already reads "ABB — 9 products"; the number is the data.
      expect(screen.getByText("9")).toBeInTheDocument();
      expect(screen.getAllByText("products").length).toBeGreaterThan(0);
    },
    TIMEOUT
  );

  it(
    "says there is no site data yet rather than drawing an empty axis",
    async () => {
      await mount(<SiteBarChart />, {
        [`GET ${BASE}/site-product-counts`]: [],
      });

      expect(
        await screen.findByText(
          "No manufacturing-site data yet — click ↻ Refresh from Smartsheet."
        )
      ).toBeInTheDocument();
      expect(screen.queryByRole("listitem")).toBeNull();
    },
    TIMEOUT
  );

  it(
    "treats a failure as 'no data yet' and names the fix",
    async () => {
      await mount(<SiteBarChart />, {
        [`GET ${BASE}/site-product-counts`]: respond(500, {}),
      });

      // Deliberately not an error Alert: on a fresh install the database simply has not been built, and
      // the request failing is the ordinary way that shows up.
      //
      // The trailing "(HTTP 500)." is `describeError`'s, appended to the component's fallback. It reads
      // oddly to a non-technical assessor — the sentence already ends in a full stop — but it is the
      // deliberate platform-wide shape (see `queries.ts:describeError`), so it is asserted as-is rather
      // than matched loosely.
      expect(
        await screen.findByText(
          "No local data yet — click ↻ Refresh from Smartsheet. (HTTP 500)."
        )
      ).toBeInTheDocument();
      expect(screen.queryByRole("listitem")).toBeNull();
    },
    TIMEOUT
  );

  it(
    "shows the server's own reason when it gives one",
    async () => {
      await mount(<SiteBarChart />, {
        [`GET ${BASE}/site-product-counts`]: respond(503, {
          detail: "The analysis database is being rebuilt.",
        }),
      });

      expect(
        await screen.findByText("The analysis database is being rebuilt.")
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "announces the wait while the counts are in flight",
    async () => {
      mockApi({});
      await loadConfigForTests();
      vi.stubGlobal(
        "fetch",
        vi.fn(() => new Promise<Response>(() => undefined))
      );
      renderWithQuery(<SiteBarChart />);

      expect(screen.getByRole("status")).toHaveTextContent(
        "Counting products per site…"
      );
    },
    TIMEOUT
  );
});
