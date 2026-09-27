import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, buildQuery, setUnauthorizedHandler } from "./http";

vi.mock("@/config/runtime", () => ({
  getRuntimeConfig: () => ({ apiBaseUrl: "/api" }),
}));

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

describe("buildQuery", () => {
  it("omits undefined and empty values", () => {
    expect(buildQuery({ a: "x", b: undefined, c: "", d: 2 })).toBe("?a=x&d=2");
  });

  it("returns an empty string when nothing is set", () => {
    expect(buildQuery({ a: undefined })).toBe("");
  });

  it("encodes values", () => {
    expect(buildQuery({ q: "a b&c" })).toBe("?q=a+b%26c");
  });
});

describe("api requests", () => {
  beforeEach(() => {
    setUnauthorizedHandler(() => {});
    vi.restoreAllMocks();
  });

  it("sends credentials so the session cookie is included", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/auth/me");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/me");
    expect(init.credentials).toBe("include");
  });

  it("throws ApiError carrying the status and body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "nope" }, 404))
    );

    // A Response body can only be read once, so assert against a single request.
    const error = await api.get("/documents/missing").catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, body: { detail: "nope" } });
  });

  it("notifies the unauthorized handler on a 401", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, 401)));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    await expect(api.get("/documents")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("does not treat a rejected login as an expired session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, 401)));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    await expect(
      api.post("/auth/login", { username: "a" }, { ignoreUnauthorized: true })
    ).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("handles a 204 with no body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    );
    await expect(api.post("/auth/logout")).resolves.toBeNull();
  });
});
