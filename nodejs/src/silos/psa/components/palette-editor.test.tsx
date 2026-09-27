/**
 * The palette editor, and specifically its provenance affordances.
 *
 * The palette is a shipped catalogue plus an override object, so the editor has two jobs beyond
 * add/remove: say whether what you are looking at has been edited, and let you put it back. Both are
 * asserted here because both are claims made to an assessor — "these are the supplier's colours" is a
 * different statement from "these are the supplier's colours as someone changed them".
 *
 * Driven through the real `queries.ts` → `client.ts` → `fetch` chain, so a wrong path or body fails.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { paletteDatwyler } from "../api/__fixtures__/live-payloads.gen";
import type { PaletteResult } from "../api/types";
import {
  loadConfigForTests,
  mockApi,
  type Routes,
  renderWithQuery,
} from "../test-support";
import { PaletteEditor } from "./palette-editor";

const BASE = "/api/silos/psa";

/** The shipped catalogue, as captured live. */
/**
 * Vitest's per-test default is 5 s. These tests render the editor's nine-column form plus the 60-colour
 * grid and then drive several fields, which measured 5.4-6.2 s inside ARIA's full suite — passing alone
 * and failing under parallel load. An explicit budget is honest about the cost rather than leaving the
 * file to flake on whichever machine is busiest; the page tests already carry one for the same reason.
 */
const TIMEOUT = 30_000;

const SHIPPED: PaletteResult = paletteDatwyler;

/** The same catalogue reported as edited: one colour added, one hidden. */
const OVERRIDDEN: PaletteResult = {
  ...paletteDatwyler,
  is_override: true,
  added: 1,
  removed: 1,
};

/**
 * The fetch mock has to be installed BEFORE the runtime config is loaded — `loadConfigForTests` fetches
 * `/config.json` through it, and jsdom cannot resolve a relative URL without it.
 */
async function render(palette: PaletteResult, extra: Routes = {}) {
  const api = mockApi({
    [`GET ${BASE}/palette`]: palette,
    ...extra,
  });
  await loadConfigForTests();
  const user = userEvent.setup({ delay: null });
  renderWithQuery(
    <PaletteEditor onPaletteChanged={() => undefined} vendor="Datwyler" />
  );
  return { api, user };
}

/**
 * One of the editor's text inputs, by CSV column name.
 *
 * Prefix-matched: Unity marks a required `Field` by appending `*` to the label, so the accessible name
 * of the two mandatory columns is e.g. `"vendor_color_name*"`.
 */
function field(column: string) {
  return screen.getByRole("textbox", {
    name: new RegExp(`^${column}*?$`),
  });
}

/**
 * Open the accordion, as a user must.
 *
 * A collapsed Unity `Accordion` still renders its children into the DOM, so `getByText` finds them —
 * but the content is `hidden`, so it is out of the accessibility tree and `getByRole` / `getByLabelText`
 * do NOT. Opening it is therefore required for any interaction, and asserting through the roles rather
 * than the raw text is what keeps these tests honest about what a user can actually reach.
 */
async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getByRole("button", { name: "Add / remove a cap colour (palette)" })
  );
}

describe("PaletteEditor — provenance", () => {
  it(
    "says nothing about edits when the shipped catalogue is in force",
    async () => {
      const { user } = await render(SHIPPED);
      await waitFor(() =>
        expect(
          screen.getByText(/never changes any product/)
        ).toBeInTheDocument()
      );
      // Opened first: a negative role assertion against a collapsed accordion would pass for the wrong
      // reason (everything inside it is hidden).
      await open(user);
      expect(screen.queryByText(/This palette has local edits/)).toBeNull();
    },
    TIMEOUT
  );

  it(
    "reports what the override changed, and that it is shared",
    async () => {
      await render(OVERRIDDEN);
      expect(
        await screen.findByText(/This palette has local edits/)
      ).toBeInTheDocument();
      // The counts are the engine's, not a re-count of the rows on screen.
      expect(
        screen.getByText(/1 colour\(s\) added or changed, 1 hidden/)
      ).toBeInTheDocument();
      expect(
        screen.getByText(/everyone sees this palette, not just you/)
      ).toBeInTheDocument();
    },
    TIMEOUT
  );

  it(
    "explains an unreadable override without implying the tool is broken",
    async () => {
      const { user } = await render({ ...SHIPPED, override_invalid: true });
      expect(
        await screen.findByText(/could not be read, so the shipped catalogue/)
      ).toBeInTheDocument();
      // The whole point of the fallback: recommendations still work.
      expect(
        screen.getByText(/Recommendations are unaffected/)
      ).toBeInTheDocument();
      // No reset button exists; the recovery route offered is the next ordinary edit.
      await open(user);
      expect(
        screen.queryByRole("button", { name: /Restore the shipped catalogue/ })
      ).toBeNull();
      expect(
        screen.getByText(
          /the next add or remove below replaces the unreadable edit/
        )
      ).toBeInTheDocument();
    },
    TIMEOUT
  );
});

describe("PaletteEditor — add and remove still work", () => {
  it(
    "sends the eight CSV columns plus the vendor on add",
    async () => {
      const { api, user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: { ok: true, message: "Added." },
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Sky 9998");
      await user.type(field("canonical_color"), "Blue");
      await user.click(screen.getByRole("button", { name: "Add colour" }));

      expect(await screen.findByText("Added.")).toBeInTheDocument();
      const add = api.calls.find(
        (call) => call.method === "POST" && call.url === `${BASE}/palette`
      );
      expect(add?.body).toMatchObject({
        vendor: "Datwyler",
        vendor_color_name: "Sky 9998",
        canonical_color: "Blue",
        // Defaults are sent explicitly rather than relied on server-side.
        sizes_mm: "13;20",
        component: "pp_disc",
        off_the_shelf: true,
      });
    },
    TIMEOUT
  );

  it(
    "clears the add form on success so a second click cannot silently rewrite the row",
    async () => {
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: { ok: true, message: "Added." },
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Sky 9998");
      await user.type(field("canonical_color"), "Blue");
      await user.click(screen.getByRole("button", { name: "Add colour" }));

      await screen.findByText("Added.");
      expect(field("vendor_color_name")).toHaveValue("");
      expect(field("canonical_color")).toHaveValue("");
    },
    TIMEOUT
  );

  it(
    "keeps the values when the engine refuses the edit",
    async () => {
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: {
          ok: false,
          message: "Canonical colour is required (e.g. Blue, Green, Grey).",
        },
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Sky 9998");
      await user.click(screen.getByRole("button", { name: "Add colour" }));

      expect(
        await screen.findByText(/Canonical colour is required/)
      ).toBeInTheDocument();
      expect(field("vendor_color_name")).toHaveValue("Sky 9998");
    },
    TIMEOUT
  );
});
