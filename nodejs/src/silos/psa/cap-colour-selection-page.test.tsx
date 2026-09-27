/**
 * The whole Cap Colour Selection page, driven by the REAL captured shim payloads.
 *
 * What is worth testing here is not the markup — the panels have their own tests — but the four
 * behaviours the Dash callbacks encoded, which are easy to break and invisible in a screenshot:
 * the two validation refusals, and the three "clear what no longer applies" rules documented on
 * `CapColourSelectionPage`. Everything goes through the mocked `fetch`, so a wrong request path or
 * body fails here rather than against a live server.
 *
 * Timeouts are explicit: this page mounts ~50 interactive nodes plus four queries, which exceeds
 * Vitest's 5 s default in jsdom.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import {
  paletteDatwyler,
  paletteWest,
  presentationsAbbv383,
  presentationsBonte,
  productsByColorBlue6043,
  programs,
  recommendBonte,
  refreshOk,
  reportBonte,
  siteCounts,
} from "./api/__fixtures__/live-payloads.gen";
import { CapColourSelectionPage } from "./cap-colour-selection-page";
import {
  type ApiMock,
  loadConfigForTests,
  type MockRequest,
  mockApi,
  PSA_WAIT,
  type Routes,
  renderWithQuery,
} from "./test-support";

const BASE = "/api/silos/psa";
const TIMEOUT = 30_000;

const DATWYLER_NAMES = new Set(
  paletteDatwyler.colors.map((color) => color.vendor_color_name)
);
const WEST_NAMES = new Set(
  paletteWest.colors.map((color) => color.vendor_color_name)
);

/**
 * Vendor and program are read off the request, not stubbed per test: the page issues the same two
 * GETs with different query strings, and branching here is what makes "switching vendor refetches"
 * observable instead of assumed.
 *
 * `request` is annotated because `Routes`' value type (`unknown | (fn)`) collapses to `unknown`, so
 * the parameter has nothing to infer from.
 */
function routes(): Routes {
  return {
    [`GET ${BASE}/programs`]: programs,
    [`GET ${BASE}/palette`]: (request: MockRequest) =>
      request.url.includes("West") ? paletteWest : paletteDatwyler,
    [`GET ${BASE}/site-product-counts`]: siteCounts,
    [`GET ${BASE}/presentations`]: (request: MockRequest) =>
      request.url.includes("AGN-151586")
        ? presentationsBonte
        : presentationsAbbv383,
    [`GET ${BASE}/products-by-color`]: productsByColorBlue6043,
    [`POST ${BASE}/recommend`]: recommendBonte,
    [`POST ${BASE}/report`]: reportBonte,
    [`POST ${BASE}/refresh`]: refreshOk,
  };
}

/** Accessible names collapse whitespace; the presentation labels contain double spaces. */
const norm = (text: string) => text.replace(/\s+/g, " ").trim();

/** Swatches are buttons whose whole label is a palette colour name. */
function swatchCount(names: Set<string>) {
  return screen
    .getAllByRole("button")
    .filter((button) => names.has(norm(button.textContent ?? ""))).length;
}

type User = ReturnType<typeof userEvent.setup>;

/**
 * Unity's searchable `Select` is an `<input role="combobox">`; its options only enter the DOM once
 * it is open, and picking one puts the option's LABEL in the input's `value`.
 */
async function selectOption(user: User, field: string, optionText: string) {
  await user.click(screen.getByRole("combobox", { name: field }));
  const options = await screen.findAllByRole("option");
  const target = options.find(
    (option) => norm(option.textContent ?? "") === norm(optionText)
  );
  if (!target) {
    throw new Error(
      `No "${field}" option ${optionText}. Saw: ${options
        .map((option) => norm(option.textContent ?? ""))
        .join(" | ")}`
    );
  }
  await user.click(target);
}

function comboboxValue(field: string) {
  return (screen.getByRole("combobox", { name: field }) as HTMLInputElement)
    .value;
}

const clickButton = (user: User, name: string | RegExp) =>
  user.click(screen.getByRole("button", { name }));

/** Both palettes and the site chart are on screen before any test starts interacting. */
async function renderPage() {
  const user = userEvent.setup({ delay: null });
  renderWithQuery(<CapColourSelectionPage />);
  // An explicit budget, not a global default: this helper pays module compile and jsdom warm-up on
  // the first test in the file, and raising the timeout globally would change it for every OTHER
  // silo's tests too (see `PSA_WAIT` in test-support).
  await waitFor(() => expect(swatchCount(DATWYLER_NAMES)).toBe(40), {
    timeout: PSA_WAIT,
  });
  return user;
}

describe("CapColourSelectionPage", () => {
  let api: ApiMock;

  beforeEach(async () => {
    api = mockApi(routes());
    await loadConfigForTests();
  });

  it(
    "renders the live palette and the per-site counts",
    async () => {
      await renderPage();

      // No stub was missed — an unmatched request would have rendered an error alert instead.
      expect(api.unmatched).toEqual([]);
      expect(api.calledPaths()).toContain(
        `GET ${BASE}/palette?vendor=Datwyler`
      );

      // The site chart carries real Smartsheet data, including the one "site" that is a full
      // postal address rather than a code — it must render as one row, not be split.
      expect(screen.getByText("ABB")).toBeInTheDocument();
      expect(
        screen.getByText("Allergan Pharmaceuticals, County Mayo, Westport")
      ).toBeInTheDocument();
      expect(screen.getByText("GRAM")).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "swaps the whole palette when the seal manufacturer changes",
    async () => {
      const user = await renderPage();

      await clickButton(user, "West");

      await waitFor(() => expect(swatchCount(WEST_NAMES)).toBe(20));
      expect(swatchCount(DATWYLER_NAMES)).toBe(0);
      expect(screen.queryByText("Transparent 6001")).not.toBeInTheDocument();
      expect(api.calledPaths()).toContain(`GET ${BASE}/palette?vendor=West`);
    },
    TIMEOUT
  );

  it(
    "lists the products using one exact vendor code, not the whole hue",
    async () => {
      const user = await renderPage();

      await clickButton(user, "Blue 6043");

      expect(
        await screen.findByText("Products using Blue 6043")
      ).toBeInTheDocument();
      expect(screen.getByText("Michael Coleman")).toBeInTheDocument();
      // The colour goes to the server as the vendor code, which is what makes the match exact.
      expect(api.calledPaths()).toContain(
        `GET ${BASE}/products-by-color?vendor=Datwyler&color=Blue+6043`
      );
    },
    TIMEOUT
  );

  it(
    "refuses to recommend without a product, and asks for nothing else",
    async () => {
      const user = await renderPage();

      await clickButton(user, "Recommend cap colours");

      expect(
        screen.getByText("Select a product first, then click Recommend.")
      ).toBeInTheDocument();
      expect(api.calledPaths()).not.toContain(`POST ${BASE}/recommend`);
    },
    TIMEOUT
  );

  it(
    "refuses to recommend a product without a presentation",
    async () => {
      const user = await renderPage();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await clickButton(user, "Recommend cap colours");

      expect(
        screen.getByText("Select a presentation, then click Recommend.")
      ).toBeInTheDocument();
      expect(api.calledPaths()).not.toContain(`POST ${BASE}/recommend`);
    },
    TIMEOUT
  );

  it(
    "sends the product and presentation row but NOT a supplier, and shows the result",
    async () => {
      /**
       * `vendor` was sent until 2026-08-21, and its absence is now the contract: the recommendation
       * considers both catalogues and names the supplier of each colour it suggests, because at
       * assessment time the cap is frequently not yet tooled or contracted. Sending the toggle would
       * silently narrow the answer to one supplier. Asserted with `toEqual` so an accidental
       * reintroduction of the field fails here rather than quietly halving the candidate pool.
       */
      const user = await renderPage();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await selectOption(user, "Presentation", "2R (2.00 mL) vial");
      await clickButton(user, "Recommend cap colours");

      expect(
        await screen.findByText(/Already selected \(current cap\)/)
      ).toBeInTheDocument();
      const call = api.calls.find((entry) => entry.url === `${BASE}/recommend`);
      expect(call?.body).toEqual({
        program: "AGN-151586",
        source_row: "20",
      });
    },
    TIMEOUT
  );

  it(
    "keeps the recommendation when the seal manufacturer changes, because it spans both",
    async () => {
      /**
       * This asserted the OPPOSITE until 2026-08-21, and the change is the point rather than a
       * concession: the toggle used to drive the recommendation, so switching it genuinely invalidated
       * the result. Now the recommendation considers both catalogues and names the supplier of each
       * colour, so the toggle only governs which catalogue the swatch grid displays. Rule 1 is "nothing
       * outlives its selection" — and the selection, product and presentation, has not changed. Clearing
       * would discard a still-correct answer for looking at the other catalogue.
       */
      const user = await renderPage();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await selectOption(user, "Presentation", "2R (2.00 mL) vial");
      await clickButton(user, "Recommend cap colours");
      await screen.findByText(/Already selected \(current cap\)/);

      await clickButton(user, "West");

      // The grid did switch catalogue — proof the click landed, so the assertion below means something.
      await waitFor(() => expect(swatchCount(WEST_NAMES)).toBe(20));
      expect(
        screen.getByText(/Already selected \(current cap\)/)
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "resets the presentation when the product changes",
    async () => {
      const user = await renderPage();

      await selectOption(user, "Product", "ABBV-383 (Etentamig)");
      await selectOption(user, "Presentation", "10R | Commercial | 20 mg");
      expect(comboboxValue("Presentation")).toContain("10R");

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");

      // Rule 2: a source_row only means anything inside one program, so it cannot carry over.
      expect(comboboxValue("Presentation")).toBe("");
      await clickButton(user, "Recommend cap colours");
      expect(
        screen.getByText("Select a presentation, then click Recommend.")
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "drops the product selection when the sheet is re-read",
    async () => {
      const user = await renderPage();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await selectOption(user, "Presentation", "2R (2.00 mL) vial");

      await clickButton(user, "Refresh from Smartsheet");

      expect(
        await screen.findByText(
          "Product list refreshed from the LIVE Smartsheet."
        )
      ).toBeInTheDocument();
      // Rule 3: the row keys were just re-read, so the old choice may no longer exist.
      expect(comboboxValue("Product")).toBe("");
      await clickButton(user, "Recommend cap colours");
      expect(
        screen.getByText("Select a product first, then click Recommend.")
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "hands the page's own selection to the report generator",
    async () => {
      const user = await renderPage();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await selectOption(user, "Presentation", "2R (2.00 mL) vial");
      await clickButton(user, "Generate PSA report");

      await waitFor(() =>
        expect(
          api.calls.find((entry) => entry.url === `${BASE}/report`)
        ).toBeDefined()
      );
      expect(
        api.calls.find((entry) => entry.url === `${BASE}/report`)?.body
      ).toEqual({ program: "AGN-151586", presentation: "20" });
      expect(
        await screen.findByText(/ALL CHECKS PASSED — generated PSA report for/)
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "keeps the .docx export disabled until there is something to export",
    async () => {
      const user = await renderPage();

      expect(
        screen.getByRole("button", { name: /Export recommendation/ })
      ).toBeDisabled();

      await selectOption(user, "Product", "AGN-151586 (BoNT/E)");
      await selectOption(user, "Presentation", "2R (2.00 mL) vial");

      expect(
        screen.getByRole("button", { name: /Export recommendation/ })
      ).toBeEnabled();
    },
    TIMEOUT
  );
});
