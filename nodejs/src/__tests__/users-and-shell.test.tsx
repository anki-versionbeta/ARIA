/**
 * User management, and the three pieces of the shell that only an admin sees.
 *
 * The users page is the only screen in the app that changes somebody else's access, so the
 * rules it enforces are asserted from both sides: a fixed administrator cannot be edited
 * *and* an ordinary one can; a role that implies every module hides the tick boxes *and*
 * switching back to User shows them again. The page sends the complete module set rather
 * than a delta, and modules go before the role on a demotion — both are pinned here,
 * because a regression in either silently grants or removes access.
 *
 * Everything is driven through the real router (see `harness.tsx` for why the route
 * components cannot simply be imported), which is also what exercises `_app`'s header:
 * `RequestBell` and `UserMenu` are rendered by the layout, not by the page, and
 * `useCurrentSiloId` reads the matched route rather than any state we could set.
 *
 * `apiHandlers` answers the shell endpoints; each test declares only the paths it cares
 * about. `/auth/me` is overridden wherever a test needs a pending-request count, because
 * the shared fixtures do not carry one.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ADMIN_USER,
  apiHandlers,
  PLAIN_USER,
  renderAt,
} from "@/__tests__/harness";
import { ApiError } from "@/api/http";
import type { AccessRequest, AdminUser, Module } from "@/api/types";

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

const MODULES: Module[] = [
  { id: "bop", label: "Equipment BOP" },
  { id: "iso", label: "ISO Applicability Assessment" },
  { id: "mfg_atr", label: "Manufacturing ATR" },
];

/** First row is auto-selected on arrival, so the editable user is deliberately first. */
const BEN: AdminUser = {
  id: "u-plain",
  username: "ben.carter",
  display_name: "Ben Carter",
  email: "ben.carter@abbvie.com",
  role: "user",
  modules: ["bop"],
  modules_from_role: false,
  protected: false,
  last_seen: "2026-08-20T09:00:00Z",
};

const CARA: AdminUser = {
  id: "u-super",
  username: "cara.singh",
  display_name: "Cara Singh",
  email: null,
  role: "super_user",
  modules: ["bop", "iso", "mfg_atr"],
  modules_from_role: true,
  protected: false,
  last_seen: "2026-08-21T09:00:00Z",
};

const FIXED: AdminUser = {
  id: "u-fixed",
  username: "svc.aria",
  display_name: "Dana Fixed",
  email: "svc.aria@abbvie.com",
  role: "admin",
  modules: ["bop", "iso", "mfg_atr"],
  modules_from_role: true,
  protected: true,
  last_seen: "2026-08-22T09:00:00Z",
};

const USERS = [BEN, CARA, FIXED];

const REQUEST: AccessRequest = {
  id: "r-1",
  user_id: "u-erin",
  username: "erin.doyle",
  display_name: "Erin Doyle",
  email: "erin.doyle@abbvie.com",
  modules: ["iso", "ghost"],
  note: "Joining the ISO review team.",
  status: "pending",
  created_at: "2026-08-25T08:30:00Z",
  decided_at: null,
  decided_by: null,
  decision_note: null,
};

/** The users page needs the list and the module catalogue on top of the shell paths. */
function usersPageHandlers(overrides: Record<string, unknown> = {}) {
  return apiHandlers({
    "/admin/users": USERS,
    "/modules": MODULES,
    "/admin/roles": [],
    ...overrides,
  });
}

/** `/auth/me` with a pending count: the shared fixtures do not carry one. */
function adminWithPending(pending: number) {
  return { ...ADMIN_USER, pending_access_requests: pending };
}

async function renderUsersPage(overrides: Record<string, unknown> = {}) {
  get.mockImplementation(usersPageHandlers(overrides));
  const rendered = await renderAt("/users");
  // The detail pane only exists once the list has resolved and row one is selected. The
  // generous timeout is for the first test in the file, which pays for compiling the
  // route tree before anything renders.
  await screen.findByRole(
    "heading",
    { name: "Module access" },
    { timeout: 8000 }
  );
  return rendered;
}

function moduleCheckbox(label: string) {
  return screen.getByRole("checkbox", { name: new RegExp(label, "i") });
}

function saveButton() {
  return screen.getByRole("button", { name: /save changes/i });
}

beforeEach(() => {
  get.mockReset().mockImplementation(apiHandlers());
  post.mockReset().mockResolvedValue({});
  put.mockReset().mockResolvedValue({});
  del.mockReset().mockResolvedValue(null);
});

describe("the user management page", () => {
  it("lists everyone the API returned and selects the first person on arrival", async () => {
    await renderUsersPage();

    expect(
      await screen.findByRole("heading", { name: "User management" })
    ).toBeInTheDocument();
    expect(screen.getByText("Cara Singh")).toBeInTheDocument();
    expect(screen.getByText("Dana Fixed")).toBeInTheDocument();
    // Row one is selected, so its email and its current grants are on screen.
    expect(screen.getByText("ben.carter@abbvie.com")).toBeInTheDocument();
    expect(moduleCheckbox("Equipment BOP")).toBeChecked();
    expect(moduleCheckbox("ISO Applicability Assessment")).not.toBeChecked();
  });

  it("abandons an unfinished edit when another person is chosen", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.click(moduleCheckbox("Manufacturing ATR"));
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByText("Cara Singh"));

    expect(
      await screen.findByRole("heading", { name: "Cara Singh" })
    ).toBeInTheDocument();
    // Back to Ben, and the tick that was never saved is gone.
    await user.click(screen.getByText("Ben Carter"));
    expect(await screen.findByText("All changes saved")).toBeInTheDocument();
    expect(moduleCheckbox("Manufacturing ATR")).not.toBeChecked();
    expect(put).not.toHaveBeenCalled();
  });

  it("explains what the selected person's role grants", async () => {
    await renderUsersPage();

    expect(
      screen.getByText("Only the modules ticked below.")
    ).toBeInTheDocument();
    expect(screen.getByText("All changes saved")).toBeInTheDocument();
  });

  it("narrows the list to the people matching the search term", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.type(screen.getByPlaceholderText("Search people"), "cara");

    await waitFor(() =>
      expect(screen.queryByText("Dana Fixed")).not.toBeInTheDocument()
    );
    // Hiding the selected row moves the selection, so Cara's pane is now shown.
    expect(
      await screen.findByRole("heading", { name: "Cara Singh" })
    ).toBeInTheDocument();
  });

  it("matches on username as well as display name", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.type(screen.getByPlaceholderText("Search people"), "svc.aria");

    await waitFor(() =>
      expect(screen.queryByText("Cara Singh")).not.toBeInTheDocument()
    );
    // The surviving row is also the selection now, so the name is on screen twice.
    expect(screen.getAllByText("Dana Fixed").length).toBeGreaterThan(1);
  });

  it("says nobody matches when the search excludes everyone", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.type(screen.getByPlaceholderText("Search people"), "zzz");

    expect(await screen.findByText(/Nobody matches/)).toBeInTheDocument();
  });

  it("shows a person's details only after the list resolves", async () => {
    // An empty roster leaves nothing selected, so the right-hand pane is the empty state.
    await renderAtUsersWithRoster([]);

    expect(await screen.findByText("Nobody selected")).toBeInTheDocument();
    expect(
      screen.getByText("Choose someone to manage their access.")
    ).toBeInTheDocument();
  });

  it("reports the API's own message when the list cannot be loaded", async () => {
    get.mockImplementation(
      usersPageHandlers({
        "/admin/users": () =>
          Promise.reject(
            new ApiError(500, { detail: "The directory is unavailable." })
          ),
      })
    );
    await renderAt("/users");

    expect(
      await screen.findByText("The directory is unavailable.")
    ).toBeInTheDocument();
  });

  it("sends the whole ticked set of modules, not just the change", async () => {
    const user = userEvent.setup();
    put.mockResolvedValue({ ...BEN, modules: ["bop", "iso"] });
    await renderUsersPage();

    expect(saveButton()).toBeDisabled();
    await user.click(moduleCheckbox("ISO Applicability Assessment"));

    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await user.click(saveButton());

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/admin/users/u-plain/modules", {
        modules: ["bop", "iso"],
      })
    );
    // The saved row comes back from the API and the pane settles on it.
    expect(await screen.findByText("All changes saved")).toBeInTheDocument();
    expect(moduleCheckbox("ISO Applicability Assessment")).toBeChecked();
    // A role that did not change must not be written back.
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("does not confirm the save once the API echoes the updated row", async () => {
    // Characterization, not the intent. users.tsx:179 sets `saved` after the mutation
    // resolves, but the mutation's onSuccess has already patched the cached row, and the
    // reset effect at users.tsx:132-137 — keyed on the selected row's identity — runs on
    // that re-render and clears `saved` again. Because the API always returns the updated
    // user, the "Saved." alert at users.tsx:304 is effectively dead code. Correct
    // behaviour would be to keep the confirmation until the selection changes, e.g. by
    // keying the reset on `selected.id` alone.
    const user = userEvent.setup();
    put.mockResolvedValue({ ...BEN, modules: ["bop", "iso"] });
    await renderUsersPage();

    await user.click(moduleCheckbox("ISO Applicability Assessment"));
    await user.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText("All changes saved")).toBeInTheDocument()
    );
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
  });

  it("can take every module away, and warns that the person will see nothing", async () => {
    const user = userEvent.setup();
    put.mockResolvedValue({ ...BEN, modules: [] });
    await renderUsersPage();

    await user.click(moduleCheckbox("Equipment BOP"));

    expect(
      await screen.findByText(/Nothing ticked/, { exact: false })
    ).toBeInTheDocument();
    await user.click(saveButton());

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/admin/users/u-plain/modules", {
        modules: [],
      })
    );
  });

  it("restores the server's answer when the edit is discarded", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.click(moduleCheckbox("Equipment BOP"));
    expect(moduleCheckbox("Equipment BOP")).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: /discard/i }));

    expect(moduleCheckbox("Equipment BOP")).toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(put).not.toHaveBeenCalled();
  });

  it("shows the API's refusal instead of a generic failure", async () => {
    const user = userEvent.setup();
    put.mockRejectedValue(
      new ApiError(409, {
        detail: {
          reason: "last_admin",
          message: "Cannot remove the last admin.",
        },
      })
    );
    await renderUsersPage();

    await user.click(moduleCheckbox("ISO Applicability Assessment"));
    await user.click(saveButton());

    expect(
      await screen.findByText("Cannot remove the last admin.")
    ).toBeInTheDocument();
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
  });

  it("hides the module ticks for a role that already grants everything", async () => {
    await renderUsersPage({ "/admin/users": [CARA, BEN] });

    // Cara is a super user: individual grants would be a no-op.
    expect(
      await screen.findByText(/Every module, from the Super user role/)
    ).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(
      screen.getByText("Every module, automatically.")
    ).toBeInTheDocument();
  });

  it("refuses to edit a fixed administrator from this screen", async () => {
    await renderUsersPage({ "/admin/users": [FIXED, BEN] });

    expect(
      await screen.findByText(
        /Dana Fixed is a fixed administrator\. Their role and access cannot be changed here\./
      )
    ).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    expect(screen.getByRole("button", { name: /discard/i })).toBeDisabled();
    // The footer says nothing about unsaved work: there is nothing to save.
    expect(screen.queryByText("All changes saved")).not.toBeInTheDocument();
  });

  it("offers Save to an ordinary administrator once something changes", async () => {
    const user = userEvent.setup();
    await renderUsersPage();

    await user.click(moduleCheckbox("Manufacturing ATR"));

    expect(saveButton()).toBeEnabled();
  });

  it("changes a role, and writes the modules before the role", async () => {
    const user = userEvent.setup();
    put.mockResolvedValue({ ...BEN, role: "admin" });
    await renderUsersPage();

    await chooseRole(user, "Admin");

    // Promotion to Admin makes the tick boxes moot, so they go away.
    expect(
      await screen.findByText(/Every module, from the Admin role/)
    ).toBeInTheDocument();
    await user.click(saveButton());

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/admin/users/u-plain/role", {
        role: "admin",
      })
    );
    // Only the role: a promotion does not rewrite the individual grants.
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("keeps the demoted person on the ticked set rather than their old grants", async () => {
    const user = userEvent.setup();
    put.mockResolvedValue({ ...CARA, role: "user", modules: ["bop", "iso"] });
    await renderUsersPage({ "/admin/users": [CARA, BEN] });

    await chooseRole(user, "User");
    // Modules become editable again on the way down; ISO is what we grant.
    await user.click(moduleCheckbox("ISO Applicability Assessment"));
    await user.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    // Modules first: otherwise the role change lands and the grants are recomputed.
    expect(put.mock.calls[0][0]).toBe("/admin/users/u-super/modules");
    expect(put.mock.calls[1][0]).toBe("/admin/users/u-super/role");
    expect(put.mock.calls[1][1]).toEqual({ role: "user" });
  });

  it("sends a non-administrator away instead of showing them a locked page", async () => {
    get.mockImplementation(usersPageHandlers()); // the roster would answer if asked
    get.mockImplementation(
      apiHandlers({ "/modules": MODULES, "/admin/users": USERS }, PLAIN_USER)
    );
    const { router } = await renderAt("/users");

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    // And the admin-only list was never requested on their behalf.
    expect(get).not.toHaveBeenCalledWith("/admin/users");
  });
});

describe("the access request queue", () => {
  // A super user is selected so the detail pane shows no module tick boxes: the module
  // names on screen are then unambiguously the ones the request asked for.
  const queueHandlers = (overrides: Record<string, unknown> = {}) => ({
    "/admin/access-requests": [REQUEST],
    "/admin/users": [CARA],
    ...overrides,
  });

  it("is absent when nobody is waiting", async () => {
    await renderUsersPage();

    expect(screen.queryByText(/Access requests \(/)).not.toBeInTheDocument();
  });

  it("names the requester, the modules asked for, and why", async () => {
    await renderUsersPage(queueHandlers());

    expect(await screen.findByText("Access requests (1)")).toBeInTheDocument();
    expect(screen.getByText("Erin Doyle")).toBeInTheDocument();
    expect(
      screen.getByText("ISO Applicability Assessment")
    ).toBeInTheDocument();
    // An id the module catalogue does not know is shown raw rather than dropped.
    expect(screen.getByText("ghost")).toBeInTheDocument();
    expect(
      screen.getByText("“Joining the ISO review team.”")
    ).toBeInTheDocument();
    expect(screen.getByText(/erin\.doyle · asked/)).toBeInTheDocument();
  });

  it("grants exactly what was asked for when approved", async () => {
    const user = userEvent.setup();
    await renderUsersPage(queueHandlers());

    await user.click(await screen.findByRole("button", { name: /approve/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/admin/access-requests/r-1/approve",
        // No module list: approving grants what the request named.
        { modules: undefined, note: undefined }
      )
    );
  });

  it("posts a rejection to the reject endpoint", async () => {
    const user = userEvent.setup();
    await renderUsersPage(queueHandlers());

    await user.click(await screen.findByRole("button", { name: /reject/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/access-requests/r-1/reject", {
        note: undefined,
      })
    );
  });

  it("reports why a decision could not be recorded", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(
      new ApiError(409, { detail: "That request was already decided." })
    );
    await renderUsersPage(queueHandlers());

    await user.click(await screen.findByRole("button", { name: /approve/i }));

    expect(
      await screen.findByText("That request was already decided.")
    ).toBeInTheDocument();
  });

  it("omits the note line for a request that came without one", async () => {
    await renderUsersPage(
      queueHandlers({
        "/admin/access-requests": [
          { ...REQUEST, note: null, modules: ["bop"] },
        ],
      })
    );

    expect(await screen.findByText("Access requests (1)")).toBeInTheDocument();
    expect(screen.queryByText(/Joining the ISO/)).not.toBeInTheDocument();
    expect(screen.getByText("Equipment BOP")).toBeInTheDocument();
  });
});

describe("the pending request bell", () => {
  it("counts the requests waiting, in words, for a screen reader", async () => {
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(3) }));
    await renderAt("/");

    const bell = await screen.findByRole("button", {
      name: "3 access requests waiting",
    });
    expect(bell).toHaveTextContent("3");
  });

  it("says request rather than requests when only one is waiting", async () => {
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(1) }));
    await renderAt("/");

    expect(
      await screen.findByRole("button", { name: "1 access request waiting" })
    ).toBeInTheDocument();
  });

  it("caps the badge at 99+ while still announcing the true count", async () => {
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(150) }));
    await renderAt("/");

    const bell = await screen.findByRole("button", {
      name: "150 access requests waiting",
    });
    expect(bell).toHaveTextContent("99+");
  });

  it("takes the admin to the queue when clicked", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/auth/me": adminWithPending(2),
        "/admin/users": USERS,
        "/modules": MODULES,
      })
    );
    const { router } = await renderAt("/");

    await user.click(
      await screen.findByRole("button", { name: "2 access requests waiting" })
    );

    await waitFor(() => expect(router.state.location.pathname).toBe("/users"));
  });

  it("is absent when the queue is empty", async () => {
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(0) }));
    await renderAt("/");

    await screen.findByText(/asha rao/i);
    expect(screen.queryByRole("button", { name: /waiting/ })).toBeNull();
  });

  it("is absent for a non-administrator even if a count arrives", async () => {
    get.mockImplementation(
      apiHandlers({
        "/auth/me": { ...PLAIN_USER, pending_access_requests: 4 },
      })
    );
    await renderAt("/");

    await screen.findByText(/ben carter/i);
    expect(screen.queryByRole("button", { name: /waiting/ })).toBeNull();
  });

  it("renders an empty pill when the API omits the count", async () => {
    // Characterization. Suspected defect: request-bell.tsx:24 guards with `count < 1`,
    // which is false for `undefined`, so a session payload missing
    // `pending_access_requests` renders a numberless bell labelled "undefined access
    // requests waiting". Correct behaviour would be to treat a missing count as zero
    // (e.g. `if (user.role !== "admin" || !(count >= 1)) return null`).
    get.mockImplementation(apiHandlers()); // ADMIN_USER carries no count
    await renderAt("/");

    expect(
      await screen.findByRole("button", {
        name: "undefined access requests waiting",
      })
    ).toBeInTheDocument();
  });
});

describe("the user menu", () => {
  it("offers user management to an administrator, with the waiting count", async () => {
    const user = userEvent.setup();
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(3) }));
    await renderAt("/");

    await user.click(await screen.findByRole("button", { name: /asha rao/i }));

    expect(
      await screen.findByRole("menuitem", { name: /User management \(3\)/ })
    ).toBeInTheDocument();
  });

  it("drops the count from the menu entry when nothing is waiting", async () => {
    const user = userEvent.setup();
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(0) }));
    await renderAt("/");

    await user.click(await screen.findByRole("button", { name: /asha rao/i }));

    const item = await screen.findByRole("menuitem", {
      name: /User management/,
    });
    expect(item).toHaveTextContent(/^User management$/);
  });

  it("navigates to user management from the menu", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/auth/me": adminWithPending(0),
        "/admin/users": USERS,
        "/modules": MODULES,
      })
    );
    const { router } = await renderAt("/");

    await user.click(await screen.findByRole("button", { name: /asha rao/i }));
    await user.click(
      await screen.findByRole("menuitem", { name: /User management/ })
    );

    await waitFor(() => expect(router.state.location.pathname).toBe("/users"));
  });

  it("hides user management from a plain user rather than disabling it", async () => {
    const user = userEvent.setup();
    get.mockImplementation(
      apiHandlers({
        "/auth/me": { ...PLAIN_USER, pending_access_requests: 0 },
      })
    );
    await renderAt("/");

    await user.click(
      await screen.findByRole("button", { name: /ben carter/i })
    );

    expect(
      await screen.findByRole("menuitem", { name: /Sign out/ })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: /User management/ })
    ).toBeNull();
  });

  it("signs out through the API and returns to the login page", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(null);
    get.mockImplementation(apiHandlers({ "/auth/me": adminWithPending(0) }));
    const { router } = await renderAt("/");

    await user.click(await screen.findByRole("button", { name: /asha rao/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Sign out/ }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/auth/logout"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });
});

describe("the sidebar", () => {
  it("links to the three silo-agnostic screens", async () => {
    await renderAt("/");

    const nav = await screen.findByRole("navigation");
    expect(within(nav).getByRole("link", { name: "Home" })).toBeInTheDocument();
    expect(
      within(nav).getByRole("link", { name: "New document" })
    ).toBeInTheDocument();
    expect(
      within(nav).getByRole("link", { name: "History" })
    ).toBeInTheDocument();
  });

  it("does not offer silo settings on a screen with no silo", async () => {
    await renderAt("/history");

    await screen.findByRole("link", { name: "Home" });
    expect(screen.queryByRole("link", { name: "Prompts" })).toBeNull();
  });

  it("offers the settings of the silo currently being used", async () => {
    await renderAt("/silos/bop/config");

    const prompts = await screen.findByRole("link", { name: "Prompts" });
    // Carries the silo it was rendered for, rather than a bare /silos link.
    expect(prompts).toHaveAttribute("href", "/silos/bop/config");

    // The sidebar starts collapsed, which is why its section titles are not on screen
    // until it is opened; expanding it shows the silo the group belongs to.
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /toggle panel/i }));
    const sidebar = screen.getByRole("complementary");
    expect(
      await within(sidebar).findByText("Equipment BOP")
    ).toBeInTheDocument();
  });

  it("resolves the silo from the document being viewed, not from the URL", async () => {
    // /documents/$documentId never names a silo, so `useCurrentSiloId` has to read it back
    // off the document — which is what puts BOP's settings in the sidebar here.
    get.mockImplementation(
      apiHandlers({
        "/documents/d-1": {
          id: "d-1",
          silo_id: "bop",
          title: "Filler 3 BOP",
          status: "complete",
          stage: null,
          progress_pct: 100,
          progress_message: null,
          owner: {
            id: ADMIN_USER.id,
            username: ADMIN_USER.username,
            display_name: ADMIN_USER.display_name,
          },
          started_at: "2026-08-20T09:00:00Z",
          finished_at: "2026-08-20T09:05:00Z",
          duration_ms: 300000,
          error_message: null,
          files: [],
          forked_from_run_id: null,
          can_edit: true,
        },
      })
    );
    await renderAt("/documents/d-1");

    expect(
      await screen.findByRole("link", { name: "Prompts" })
    ).toBeInTheDocument();
  });

  it("does not offer settings for a silo that contributes no settings screen", async () => {
    // ISO ships a review panel and nothing else, so there is nothing to link to.
    await renderAt("/silos/iso/new");

    await screen.findByRole("link", { name: "Home" });
    expect(screen.queryByRole("link", { name: "Prompts" })).toBeNull();
  });

  it("does not offer settings for a silo the API has not enabled", async () => {
    // `bop` contributes a settings screen, but a silo absent from GET /silos is dark.
    get.mockImplementation(apiHandlers({ "/silos": [{ ...SILO_ISO_ONLY }] }));
    await renderAt("/silos/bop/config");

    await screen.findByRole("link", { name: "Home" });
    expect(screen.queryByRole("link", { name: "Prompts" })).toBeNull();
  });
});

const SILO_ISO_ONLY = {
  id: "iso",
  label: "ISO Applicability Assessment",
  accepts: [".pdf"],
  stages: ["ingest", "build"],
};

/** Renders /users with an arbitrary roster. */
async function renderAtUsersWithRoster(roster: AdminUser[]) {
  get.mockImplementation(usersPageHandlers({ "/admin/users": roster }));
  return await renderAt("/users");
}

/**
 * Pick a role from the Unity Select, which is an Ariakit combobox rather than a native
 * `<select>`: the listbox only exists once the input has been opened.
 */
async function chooseRole(
  user: ReturnType<typeof userEvent.setup>,
  label: string
) {
  // The header's colour-mode control is a Select too, so the page's own one is picked out
  // by being inside <main> rather than by role alone.
  const roleSelect = screen
    .getAllByRole("combobox")
    .find((element) => element.closest("main") !== null);
  if (!roleSelect) throw new Error("no role Select on the page");
  await user.click(roleSelect);
  await user.click(await screen.findByRole("option", { name: label }));
}
