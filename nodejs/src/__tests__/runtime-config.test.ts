/**
 * Boot-time configuration, which is what lets one built image be promoted between
 * environments.
 *
 * Vite inlines `import.meta.env` into the bundle, so reading the API base URL that way
 * would force a rebuild per environment. Fetching /config.json at startup instead means
 * the container's entrypoint can write the file and the same image serves dev and prod.
 * Two details carry that design and are asserted here: the fetch must not be cached, and
 * reading the config before it has loaded must fail loudly rather than return a default
 * that silently points at the wrong API.
 *
 * The module caches in a closure, so each test re-imports it with resetModules.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const CONFIG = { apiBaseUrl: "/api" };

async function freshModule() {
  vi.resetModules();
  return import("@/config/runtime");
}

function mockFetch(response: Partial<Response> & { json?: () => unknown }) {
  const spy = vi.fn().mockResolvedValue(response as Response);
  vi.stubGlobal("fetch", spy);
  return spy;
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("loadRuntimeConfig", () => {
  it("fetches /config.json and returns the parsed body", async () => {
    mockFetch({ ok: true, status: 200, json: async () => CONFIG });
    const { loadRuntimeConfig } = await freshModule();

    await expect(loadRuntimeConfig()).resolves.toEqual(CONFIG);
  });

  it("requests the file with caching disabled", async () => {
    // nginx also serves it with Cache-Control: no-store. Both halves matter: a cached
    // copy would point a promoted image at the previous environment's API.
    const spy = mockFetch({ ok: true, status: 200, json: async () => CONFIG });
    const { loadRuntimeConfig } = await freshModule();

    await loadRuntimeConfig();

    expect(spy).toHaveBeenCalledWith("/config.json", { cache: "no-store" });
  });

  it("throws with the status when the file is missing", async () => {
    mockFetch({ ok: false, status: 404, json: async () => ({}) });
    const { loadRuntimeConfig } = await freshModule();

    // A missing config is unrecoverable, so it must be obvious in the console rather
    // than leaving the app requesting a relative URL that happens to 404.
    await expect(loadRuntimeConfig()).rejects.toThrow(/404/);
  });

  it("names the file it could not load", async () => {
    mockFetch({ ok: false, status: 500, json: async () => ({}) });
    const { loadRuntimeConfig } = await freshModule();

    await expect(loadRuntimeConfig()).rejects.toThrow(/config\.json/);
  });
});

describe("getRuntimeConfig", () => {
  it("throws when read before the config has loaded", async () => {
    const { getRuntimeConfig } = await freshModule();

    // Every api call goes through this. Returning a default instead would send requests
    // somewhere plausible-looking and fail much later.
    expect(() => getRuntimeConfig()).toThrow(/before loadRuntimeConfig/);
  });

  it("returns the loaded config afterwards", async () => {
    mockFetch({ ok: true, status: 200, json: async () => CONFIG });
    const { loadRuntimeConfig, getRuntimeConfig } = await freshModule();

    await loadRuntimeConfig();

    expect(getRuntimeConfig()).toEqual(CONFIG);
  });

  it("keeps returning the config without re-fetching", async () => {
    const spy = mockFetch({ ok: true, status: 200, json: async () => CONFIG });
    const { loadRuntimeConfig, getRuntimeConfig } = await freshModule();
    await loadRuntimeConfig();

    getRuntimeConfig();
    getRuntimeConfig();

    // Read on every request, so it must be a cache read and not a network call.
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("stays unloaded after a failed load", async () => {
    mockFetch({ ok: false, status: 503, json: async () => ({}) });
    const { loadRuntimeConfig, getRuntimeConfig } = await freshModule();

    await expect(loadRuntimeConfig()).rejects.toThrow();

    // Must not be left half-initialised with a partially assigned value.
    expect(() => getRuntimeConfig()).toThrow(/before loadRuntimeConfig/);
  });
});
