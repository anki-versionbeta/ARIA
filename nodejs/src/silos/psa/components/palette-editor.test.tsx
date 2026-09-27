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
  respond,
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

/**
 * Pick an option in one of the editor's two Unity `Select`s.
 *
 * A `Select` is an `<input role="combobox">` whose options only enter the DOM once it is open, so the
 * click is required — reading `options` off the props would assert the fixture, not the widget.
 */
async function selectOption(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
  optionText: string
) {
  await user.click(screen.getByRole("combobox", { name: label }));
  const options = await screen.findAllByRole("option");
  const target = options.find(
    (option) => (option.textContent ?? "").trim() === optionText
  );
  if (!target) {
    throw new Error(
      `No "${label}" option ${optionText}. Saw: ${options
        .map((option) => (option.textContent ?? "").trim())
        .join(" | ")}`
    );
  }
  await user.click(target);
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

  it(
    "sends off_the_shelf false when the colour is added as custom",
    async () => {
      /**
       * The one non-text column. It matters to the assessor rather than being cosmetic: the swatch grid
       * lists off-the-shelf `pp_disc` rows only, which the editor's own caption warns about, so a colour
       * saved as `custom` is deliberately invisible up there. The Select carries "1"/"0" and the body
       * carries a boolean, so the mapping is worth pinning.
       */
      const { api, user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: { ok: true, message: "Added." },
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Bespoke 1");
      await user.type(field("canonical_color"), "Grey");
      await selectOption(user, "off_the_shelf", "custom");
      await user.click(screen.getByRole("button", { name: "Add colour" }));

      await screen.findByText("Added.");
      const add = api.calls.find(
        (call) => call.method === "POST" && call.url === `${BASE}/palette`
      );
      expect(add?.body).toMatchObject({
        vendor_color_name: "Bespoke 1",
        canonical_color: "Grey",
        off_the_shelf: false,
      });
    },
    TIMEOUT
  );

  it(
    "will not remove anything until a colour is picked, then posts that name",
    async () => {
      const { api, user } = await render(SHIPPED, {
        [`POST ${BASE}/palette/remove`]: { ok: true, message: "Removed." },
      });

      await open(user);
      // The guard is the disabled button, not a silent no-op on click: with nothing picked there is
      // nothing the request could name.
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Remove selected" })
        ).toBeDisabled()
      );

      await selectOption(user, "Remove a colour", "Blue 6043");
      expect(
        screen.getByRole("button", { name: "Remove selected" })
      ).toBeEnabled();
      await user.click(screen.getByRole("button", { name: "Remove selected" }));

      expect(await screen.findByText("Removed.")).toBeInTheDocument();
      // POST, not DELETE, and via the `/palette/remove` alias — the shared client has no delete verb.
      const remove = api.calls.find(
        (call) => call.url === `${BASE}/palette/remove`
      );
      expect(remove?.method).toBe("POST");
      expect(remove?.body).toEqual({
        vendor: "Datwyler",
        vendor_color_name: "Blue 6043",
      });
      expect(api.unmatched).toEqual([]);
    },
    TIMEOUT
  );

  it(
    "clears the remove picker on success so the same colour cannot be removed twice",
    async () => {
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette/remove`]: { ok: true, message: "Removed." },
      });

      await open(user);
      await selectOption(user, "Remove a colour", "Blue 6043");
      await user.click(screen.getByRole("button", { name: "Remove selected" }));

      await screen.findByText("Removed.");
      expect(
        screen.getByRole("combobox", { name: "Remove a colour" })
      ).toHaveValue("");
      // Cleared selection ⇒ the button is back to being unclickable, so a second post is impossible.
      expect(
        screen.getByRole("button", { name: "Remove selected" })
      ).toBeDisabled();
    },
    TIMEOUT
  );

  it(
    "keeps the picked colour when the engine refuses the removal",
    async () => {
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette/remove`]: {
          ok: false,
          message: "That colour is not in the Datwyler palette.",
        },
      });

      await open(user);
      await selectOption(user, "Remove a colour", "Blue 6043");
      await user.click(screen.getByRole("button", { name: "Remove selected" }));

      expect(
        await screen.findByText(/not in the Datwyler palette/)
      ).toBeInTheDocument();
      // Nothing was removed, so the choice stays put and the user can read the message against it.
      expect(
        screen.getByRole("combobox", { name: "Remove a colour" })
      ).toHaveValue("Blue 6043");
    },
    TIMEOUT
  );

  it(
    "reports a transport failure with the server's own detail, not a generic message",
    async () => {
      /**
       * `{ok: false}` at HTTP 200 is a validation answer; a non-2xx is a thrown `ApiError`. Both end up
       * in the same status line, and this is the branch that goes through `describeError`.
       */
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: respond(500, {
          detail: "The palette override could not be written.",
        }),
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Sky 9998");
      await user.type(field("canonical_color"), "Blue");
      await user.click(screen.getByRole("button", { name: "Add colour" }));

      expect(
        await screen.findByText("The palette override could not be written.")
      ).toBeInTheDocument();
      // A failure must not look like a success: the typed row is still there to retry with.
      expect(field("vendor_color_name")).toHaveValue("Sky 9998");
    },
    TIMEOUT
  );

  it(
    "shows the last edit's outcome, not the first one's",
    async () => {
      /**
       * Add and remove share one status line, resolved by `submittedAt`. A stale success left under a
       * subsequent failure would read as "the edit worked" — the opposite of what happened.
       */
      const { user } = await render(SHIPPED, {
        [`POST ${BASE}/palette`]: { ok: true, message: "Added." },
        [`POST ${BASE}/palette/remove`]: {
          ok: false,
          message: "Refusing to hide the last colour in the palette.",
        },
      });

      await open(user);
      await waitFor(() =>
        expect(field("vendor_color_name")).toBeInTheDocument()
      );
      await user.type(field("vendor_color_name"), "Sky 9998");
      await user.type(field("canonical_color"), "Blue");
      await user.click(screen.getByRole("button", { name: "Add colour" }));
      await screen.findByText("Added.");

      await selectOption(user, "Remove a colour", "Blue 6043");
      await user.click(screen.getByRole("button", { name: "Remove selected" }));

      expect(
        await screen.findByText(/Refusing to hide the last colour/)
      ).toBeInTheDocument();
      expect(screen.queryByText("Added.")).toBeNull();
    },
    TIMEOUT
  );
});
