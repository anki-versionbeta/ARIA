/**
 * Test-only harness for the PSA feature: a `fetch` router plus a provider wrapper.
 *
 * The mock replaces `fetch`, not the client module, so every test drives the REAL chain —
 * `queries.ts` → `client.ts` → `@/api/http` → URL construction. A wrong path or a wrong method is
 * therefore a test failure rather than something only a running server would catch.
 *
 * No `UnityProvider`: Unity components render without one (see `mode-select.test.tsx`), and jsdom has
 * no layout or CSS engine, so density and theming are not observable here. They belong to the visual
 * pass, which this file deliberately does not pretend to cover.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";
import { loadRuntimeConfig } from "@/config/runtime";

/**
 * How long a PSA `waitFor` may take, passed explicitly where it is needed.
 *
 * **Deliberately NOT `configure({ asyncUtilTimeout })`.** That call is global to the whole Vitest run, so
 * inside ARIA it silently rewrote the timeout for `bop`, `iso`, `mfg_atr` and the shared components too.
 * Measured consequence: ARIA's own `section-editor`, `iso-range-picker` and `mfg-atr-new-document` tests
 * began failing intermittently with `Test timed out in 5000ms` — never a wrong assertion, always the
 * clock — because they were written against testing-library's 1 s default and a 4 s wait ate the 5 s
 * per-test budget. Two of three consecutive runs were clean, which is exactly what makes that kind of
 * damage expensive to trace back.
 *
 * A silo's tests must not reconfigure the shared test environment. So the value is a constant the PSA
 * tests opt into at the one place that needs it — the page's readiness helper, which pays module compile
 * and jsdom warm-up on the first test in its file and was the original 1 s flake.
 */
export const PSA_WAIT = 10_000;

/** What the harness serves at `/config.json`; matches `web/public/config.json`. */
const API_BASE = "/api";

export type MockRequest = {
  method: string;
  url: string;
  /** Parsed JSON request body, or undefined for a GET. */
  body: unknown;
};

type MockResponse = { __status: number; body: unknown };

/** Reply with a non-200. `detail` is what FastAPI sends and what `describeError` surfaces. */
export function respond(status: number, body: unknown): MockResponse {
  return { __status: status, body };
}

function isMockResponse(value: unknown): value is MockResponse {
  return typeof value === "object" && value !== null && "__status" in value;
}

/**
 * Route table. Keys are `"<METHOD> <path>"`, matched first with the query string and then without,
 * so `"GET /api/silos/psa/palette"` catches every vendor while an exact key can override one.
 *
 * A value is either the payload itself or a function of the request (use it to branch on the body,
 * e.g. the two `recommend` fixtures).
 */
export type Routes = Record<
  string,
  unknown | ((request: MockRequest) => unknown)
>;

export type ApiMock = {
  /** Every request the component tree actually made, in order. */
  calls: MockRequest[];
  /** Requests with no matching route. Assert this is empty to catch a silently-wrong URL. */
  unmatched: string[];
  calledPaths: () => string[];
};

/**
 * Install the `fetch` mock. Call inside the test (or a `beforeEach`) before rendering.
 *
 * An unmatched request resolves as HTTP 501 with a `detail` naming the missing stub, so the failure
 * shows up as a readable message in the rendered error alert instead of an unexplained hang.
 */
export function mockApi(routes: Routes): ApiMock {
  const calls: MockRequest[] = [];
  const unmatched: string[] = [];

  const fetchMock = vi.fn(
    (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      const body =
        typeof init?.body === "string"
          ? (JSON.parse(init.body) as unknown)
          : undefined;

      if (url === "/config.json") {
        return Promise.resolve(
          jsonResponse(200, { apiBaseUrl: API_BASE }) as Response
        );
      }

      const request: MockRequest = { method, url, body };
      calls.push(request);

      const pathOnly = url.split("?")[0];
      const handler =
        routes[`${method} ${url}`] ?? routes[`${method} ${pathOnly}`];

      if (handler === undefined) {
        unmatched.push(`${method} ${url}`);
        return Promise.resolve(
          jsonResponse(501, {
            detail: `No stub for ${method} ${url}`,
          }) as Response
        );
      }

      const result =
        typeof handler === "function"
          ? (handler as (r: MockRequest) => unknown)(request)
          : handler;

      return Promise.resolve(
        isMockResponse(result)
          ? (jsonResponse(result.__status, result.body) as Response)
          : (jsonResponse(200, result) as Response)
      );
    }
  );

  vi.stubGlobal("fetch", fetchMock);

  return {
    calls,
    unmatched,
    calledPaths: () => calls.map((call) => `${call.method} ${call.url}`),
  };
}

/**
 * Minimal stand-in for `Response`, covering every accessor the code under test actually uses:
 * `@/api/http` reads `.ok`/`.status`/`.text()`, `loadRuntimeConfig` reads `.json()`, and
 * `downloadGeneratedFile` reads `.blob()`. Nothing else is touched, so nothing else is implemented.
 */
function jsonResponse(status: number, body: unknown) {
  const text = body === undefined ? "" : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(body),
    blob: () => Promise.resolve(new Blob([text])),
  } as unknown as Response;
}

/** `@/api/http` reads the base URL synchronously, so the config must be loaded before any render. */
export async function loadConfigForTests() {
  await loadRuntimeConfig();
}

/**
 * Render inside a QueryClient with retries off, so an error path resolves on the first response
 * instead of after TanStack's default backoff. `staleTime: 0` keeps each test's cache independent.
 */
export function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  );
}
