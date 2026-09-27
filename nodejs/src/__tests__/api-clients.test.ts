/**
 * The remaining API clients, all previously at 0% coverage: auth, admin, access requests
 * and silos.
 *
 * They are one-line wrappers, so the value here is pinning the contract with the backend:
 * the path, the method, and the body shape. A typo in any of these is invisible in
 * TypeScript — the string is still a string — and only shows up as a 404 or 422 at runtime.
 *
 * Paths are checked against the routes the backend actually mounts:
 *   POST /api/auth/login           GET  /api/auth/me         POST /api/auth/logout
 *   GET  /api/admin/users          PUT  /api/admin/users/{id}/role
 *   PUT  /api/admin/users/{id}/modules
 *   POST /api/access-requests      GET  /api/access-requests/mine
 *   GET  /api/silos                GET  /api/modules
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();

vi.mock("@/api/http", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/http")>("@/api/http");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
    },
  };
});

vi.mock("@/config/runtime", () => ({
  getRuntimeConfig: () => ({ apiBaseUrl: "/api" }),
}));

beforeEach(() => {
  get.mockReset().mockResolvedValue(null);
  post.mockReset().mockResolvedValue(null);
  put.mockReset().mockResolvedValue(null);
});

function pathsFrom(spy: typeof get): string[] {
  return spy.mock.calls.map((call) => String(call[0]));
}

describe("auth", () => {
  it("logs in by posting credentials", async () => {
    const auth = await import("@/api/auth");

    auth.login("asha.rao", "secret");

    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/auth/login");
    expect(body).toEqual({ username: "asha.rao", password: "secret" });
  });

  it("marks the login call as one where a 401 is expected", async () => {
    // A 401 here means "wrong password", not "session expired", so it must not trigger
    // the global redirect to /login that every other 401 does.
    const auth = await import("@/api/auth");

    auth.login("asha.rao", "wrong");

    const options = post.mock.calls[0][2];
    expect(options?.ignoreUnauthorized).toBe(true);
  });

  it("reads the current user", async () => {
    const auth = await import("@/api/auth");

    auth.fetchCurrentUser();

    expect(pathsFrom(get)).toContain("/auth/me");
  });

  it("logs out with a POST", async () => {
    const auth = await import("@/api/auth");

    auth.logout();

    expect(pathsFrom(post)).toContain("/auth/logout");
  });
});

describe("silos", () => {
  it("lists the silos the caller may reach", async () => {
    const silos = await import("@/api/silos");

    silos.fetchSilos();

    expect(pathsFrom(get)).toContain("/silos");
  });
});

describe("admin", () => {
  it("lists users", async () => {
    const admin = await import("@/api/admin");

    admin.fetchAdminUsers();

    expect(pathsFrom(get)).toContain("/admin/users");
  });

  it("changes a role with a PUT to that user", async () => {
    const admin = await import("@/api/admin");

    admin.setUserRole("u1", "super_user");

    const [path, body] = put.mock.calls[0];
    expect(path).toBe("/admin/users/u1/role");
    expect(body).toEqual({ role: "super_user" });
  });

  it("replaces a user's modules wholesale", async () => {
    // A PUT of the full list, not a POST per grant, so unticking a box actually removes
    // the grant rather than leaving it behind.
    const admin = await import("@/api/admin");

    admin.setUserModules("u1", ["bop", "iso"]);

    const [path, body] = put.mock.calls[0];
    expect(path).toBe("/admin/users/u1/modules");
    expect(body).toEqual({ modules: ["bop", "iso"] });
  });

  it("sends an empty list when every module is removed", async () => {
    const admin = await import("@/api/admin");

    admin.setUserModules("u1", []);

    expect(put.mock.calls[0][1]).toEqual({ modules: [] });
  });

  it("lists the modules that can be granted", async () => {
    const admin = await import("@/api/admin");

    admin.fetchModules();

    expect(pathsFrom(get)).toContain("/modules");
  });

  it("reads the role list from the admin namespace, not the public one", async () => {
    // /modules is readable by any signed-in user; /admin/roles is not.
    const admin = await import("@/api/admin");

    admin.fetchRoles();

    expect(pathsFrom(get)).toContain("/admin/roles");
  });

  it("lists administrators from the auth namespace", async () => {
    // Used to tell a user who to ask, so it must be reachable before any grant exists.
    const admin = await import("@/api/admin");

    admin.fetchAdministrators();

    expect(pathsFrom(get)).toContain("/auth/administrators");
  });
});

describe("access requests", () => {
  it("submits a request for the named modules", async () => {
    const access = await import("@/api/access");

    access.requestAccess(["bop"], "I run the vessel line");

    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/access-requests");
    expect(body).toEqual({ modules: ["bop"], note: "I run the vessel line" });
  });

  it("submits a request without a note", async () => {
    const access = await import("@/api/access");

    access.requestAccess(["iso"]);

    expect(post.mock.calls[0][1]).toEqual({
      modules: ["iso"],
      note: undefined,
    });
  });

  it("can approve with a narrowed set of modules", async () => {
    // An admin may grant less than was asked for, so the approval carries its own list
    // rather than blindly accepting the request's.
    const access = await import("@/api/access");

    access.approveAccessRequest("r1", ["bop"], "iso not needed");

    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/admin/access-requests/r1/approve");
    expect(body).toEqual({ modules: ["bop"], note: "iso not needed" });
  });

  it("reads the caller's own pending request", async () => {
    const access = await import("@/api/access");

    access.fetchMyAccessRequest();

    expect(pathsFrom(get)).toContain("/access-requests/mine");
  });

  it("lists the requests awaiting a decision", async () => {
    const access = await import("@/api/access");

    access.fetchAccessRequests();

    expect(pathsFrom(get)).toContain("/admin/access-requests");
  });

  it("approves and rejects through distinct endpoints", async () => {
    const access = await import("@/api/access");

    access.approveAccessRequest("r1");
    access.rejectAccessRequest("r2");

    const paths = pathsFrom(post);
    expect(paths).toContain("/admin/access-requests/r1/approve");
    expect(paths).toContain("/admin/access-requests/r2/reject");
  });
});
