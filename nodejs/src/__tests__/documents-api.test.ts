/**
 * The documents API client, previously at 0% coverage.
 *
 * These are thin wrappers, so what is worth asserting is the wiring: the exact path and
 * method each one issues, and that the list call omits empty filters instead of sending
 * `?silo=&status=` — which the backend would treat as a filter for the empty string.
 *
 * `createDocument` is the exception. It bypasses the JSON client because the browser must
 * set the multipart boundary itself, so it repeats the credentials and error handling by
 * hand and is the one most likely to drift.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildDocument,
  createDocument,
  fetchDocument,
  fetchDocumentStatus,
  fetchDocuments,
  forkDocument,
  retryDocument,
} from "@/api/documents";
import { ApiError } from "@/api/http";

const get = vi.fn();
const post = vi.fn();

vi.mock("@/api/http", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/http")>("@/api/http");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: vi.fn(),
    },
  };
});

vi.mock("@/config/runtime", () => ({
  getRuntimeConfig: () => ({ apiBaseUrl: "/api" }),
}));

beforeEach(() => {
  get.mockReset().mockResolvedValue(null);
  post.mockReset().mockResolvedValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchDocuments", () => {
  it("sends no query string when nothing is filtered", () => {
    fetchDocuments({} as never);

    expect(get).toHaveBeenCalledWith("/documents");
  });

  it("includes only the filters that have a value", () => {
    fetchDocuments({ silo: "bop", status: "", q: undefined, page: 2 } as never);

    // An empty string must be dropped, not sent: the backend would filter on "".
    expect(get).toHaveBeenCalledWith("/documents?silo=bop&page=2");
  });

  it("passes sorting through", () => {
    fetchDocuments({ sort: "date", order: "desc" } as never);

    const [path] = get.mock.calls[0];
    expect(path).toContain("sort=date");
    expect(path).toContain("order=desc");
  });

  it("url-encodes a search term", () => {
    fetchDocuments({ q: "helix manual & more" } as never);

    const [path] = get.mock.calls[0];
    expect(path).not.toContain(" ");
    expect(path).toContain("q=");
  });
});

describe("the single-document reads", () => {
  it("fetches a document by id", () => {
    fetchDocument("d1");

    expect(get).toHaveBeenCalledWith("/documents/d1");
  });

  it("fetches the status separately from the detail", () => {
    // The status endpoint is polled while a run is in flight, so it must not be the
    // heavier detail payload.
    fetchDocumentStatus("d1");

    expect(get).toHaveBeenCalledWith("/documents/d1/status");
  });
});

describe("the document actions", () => {
  it("forks with a POST and no body", () => {
    forkDocument("d1");

    expect(post).toHaveBeenCalledWith("/documents/d1/fork");
  });

  it("retries with a POST and no body", () => {
    retryDocument("d1");

    expect(post).toHaveBeenCalledWith("/documents/d1/retry");
  });

  it("builds with an empty object when given no input", () => {
    // The backend stores the body as the paused stage's checkpoint, so it must be a
    // JSON object rather than undefined.
    buildDocument("d1");

    expect(post).toHaveBeenCalledWith("/documents/d1/build", {});
  });

  it("builds with the supplied input as the checkpoint", () => {
    buildDocument("d1", { field_values: { a: "b" } });

    expect(post).toHaveBeenCalledWith("/documents/d1/build", {
      field_values: { a: "b" },
    });
  });
});

describe("createDocument", () => {
  function stubFetch(
    response: Partial<Response> & { text?: () => Promise<string> }
  ) {
    const spy = vi.fn().mockResolvedValue(response as Response);
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  const file = () =>
    new File(["content"], "manual.pdf", { type: "application/pdf" });

  it("posts multipart form data to the silo's endpoint", async () => {
    const spy = stubFetch({
      ok: true,
      status: 202,
      text: async () => '{"id":"d9"}',
    });

    await createDocument("bop", file());

    const [url, init] = spy.mock.calls[0];
    expect(url).toBe("/api/silos/bop/documents");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("does not set a Content-Type header", async () => {
    // The browser has to add the multipart boundary itself; setting the header by hand
    // produces a body the backend cannot parse.
    const spy = stubFetch({
      ok: true,
      status: 202,
      text: async () => '{"id":"d9"}',
    });

    await createDocument("bop", file());

    expect(spy.mock.calls[0][1].headers).toBeUndefined();
  });

  it("sends the session cookie", async () => {
    // httpOnly cookie, so omitting credentials makes every upload a 401.
    const spy = stubFetch({
      ok: true,
      status: 202,
      text: async () => '{"id":"d9"}',
    });

    await createDocument("bop", file());

    expect(spy.mock.calls[0][1].credentials).toBe("include");
  });

  it("names the uploaded file under the field the backend expects", async () => {
    const spy = stubFetch({
      ok: true,
      status: 202,
      text: async () => '{"id":"d9"}',
    });

    await createDocument("bop", file());

    const body = spy.mock.calls[0][1].body as FormData;
    expect(body.get("file")).toBeInstanceOf(File);
  });

  it("returns the created document id", async () => {
    stubFetch({ ok: true, status: 202, text: async () => '{"id":"d9"}' });

    await expect(createDocument("bop", file())).resolves.toEqual({ id: "d9" });
  });

  it("raises an ApiError carrying the status and parsed body", async () => {
    // 415 with a message is the real response for an unsupported file type, and the UI
    // shows that message.
    stubFetch({
      ok: false,
      status: 415,
      text: async () => '{"detail":"Equipment BOP accepts .pdf, .docx"}',
    });

    await expect(createDocument("bop", file())).rejects.toMatchObject({
      status: 415,
      body: { detail: "Equipment BOP accepts .pdf, .docx" },
    });
  });

  it("raises an ApiError instance so callers can narrow on it", async () => {
    stubFetch({ ok: false, status: 413, text: async () => "" });

    await expect(createDocument("bop", file())).rejects.toBeInstanceOf(
      ApiError
    );
  });

  it("tolerates an error response with an empty body", async () => {
    // nginx returns a bare 413 with no JSON when the upload exceeds its limit.
    stubFetch({ ok: false, status: 413, text: async () => "" });

    await expect(createDocument("bop", file())).rejects.toMatchObject({
      status: 413,
      body: null,
    });
  });
});
