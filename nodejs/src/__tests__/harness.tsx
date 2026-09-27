/**
 * Shared harness for route-level tests.
 *
 * Not a test file — no `.test.` in the name, so vitest treats it as a module.
 *
 * Route components are not exported: each route file calls
 * `createFileRoute(path)({ component: X })` and only exports `Route`. Mocking
 * `createFileRoute` to recover `X` looked simpler but does not work — the module graph
 * also needs `lazyRouteComponent`, and stubbing that leaves the tree suspended forever.
 * So these tests drive the real router over a memory history, which is TanStack's own
 * supported approach and additionally covers the generated route tree.
 *
 * Three things are load-bearing and were each found the hard way:
 *
 *  1. `await router.load()` before `render`. A nested route renders *nothing* on first
 *     paint until the match resolves, so without this the DOM is an empty div and no API
 *     call is ever made.
 *  2. `UnityProvider` must wrap the tree, mirroring `main.tsx`. Individual Unity atoms
 *     render bare, but the app shell reads theme context via `useUnityTheme`.
 *  3. Queries must be async (`await screen.findBy...`). Part of the tree suspends, so
 *     `getBy...` on the first tick sees an empty container.
 *
 * The caller still owns `vi.mock("@/api/http", ...)`, because vi.mock is hoisted per
 * file and cannot be delegated. `apiHandlers` below supplies the shell endpoints that
 * every authenticated route needs, so a test only declares what it actually cares about.
 */

import { UnityProvider } from "@abbvie-unity/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { routeTree } from "@/routeTree.gen";

export type CurrentUser = {
  id: string;
  username: string;
  display_name: string;
  email: string;
  role: string;
  modules: string[];
  is_protected_admin: boolean;
};

export const ADMIN_USER: CurrentUser = {
  id: "u-admin",
  username: "asha.rao",
  display_name: "Asha Rao",
  email: "asha.rao@abbvie.com",
  role: "admin",
  modules: ["bop", "iso", "mfg_atr"],
  is_protected_admin: false,
};

export const PLAIN_USER: CurrentUser = {
  id: "u-plain",
  username: "ben.carter",
  display_name: "Ben Carter",
  email: "ben.carter@abbvie.com",
  role: "user",
  modules: ["bop"],
  is_protected_admin: false,
};

export const SILOS = [
  {
    id: "bop",
    label: "Equipment BOP",
    accepts: [".pdf", ".docx"],
    stages: ["extract", "generate", "review", "await_review", "build"],
  },
  {
    id: "iso",
    label: "ISO Applicability Assessment",
    accepts: [".pdf"],
    stages: ["ingest", "outline", "await_range", "build"],
  },
];

/**
 * The endpoints the app shell fetches on every authenticated route: `/auth/me` gates the
 * `_app` layout (a rejected promise redirects to /login), and the sidebar and the request
 * bell read `/silos` and the access-request endpoints.
 *
 * `overrides` is consulted first, so a test can answer one path and inherit the rest.
 * An entry may be a value or a function of the path.
 */
export function apiHandlers(
  overrides: Record<string, unknown> = {},
  user: CurrentUser = ADMIN_USER
) {
  const shell: Record<string, unknown> = {
    "/auth/me": user,
    "/silos": SILOS,
    "/auth/administrators": [],
    "/access-requests/mine": null,
    "/admin/access-requests": [],
  };

  return (path: string) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path === prefix || path.startsWith(prefix)) {
        const resolved = typeof value === "function" ? value(path) : value;
        return Promise.resolve(resolved);
      }
    }
    if (path in shell) return Promise.resolve(shell[path]);
    return Promise.resolve(null);
  };
}

export function newQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/** Mount the real router at `path`. Await it: the load must finish before asserting. */
export async function renderAt(path: string) {
  const client = newQueryClient();
  const router = createRouter({
    routeTree,
    context: {},
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  await router.load();
  const result = render(
    <UnityProvider defaultMode="light" density="balanced">
      <QueryClientProvider client={client}>
        {/* biome-ignore lint/suspicious/noExplicitAny: test-only router typing */}
        <RouterProvider router={router as any} />
      </QueryClientProvider>
    </UnityProvider>
  );
  return { ...result, router, client };
}
