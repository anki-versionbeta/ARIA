import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SectionEditor } from "./section-editor";

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

/**
 * Quill drives a contenteditable that jsdom does not implement. What this screen is
 * responsible for is which body reaches the editor and whether it is writable, so the
 * stub exposes both, plus a hook to simulate typing.
 */
vi.mock("./rich-text-editor", () => ({
  RichTextEditor: ({
    initialHtml,
    readOnly,
    onChange,
  }: {
    initialHtml: string;
    readOnly?: boolean;
    onChange: (html: string) => void;
  }) => (
    <div>
      <div
        data-testid="editor"
        data-html={initialHtml}
        data-readonly={readOnly ? "true" : "false"}
      />
      <button type="button" onClick={() => onChange("<p>typed</p>")}>
        simulate typing
      </button>
    </div>
  ),
}));

const KEY = "operating_procedure.setup";
const PATH = `/documents/d1/sections/${encodeURIComponent(KEY)}`;

const SECTIONS = [
  {
    section_key: KEY,
    content_html: "<p>current</p>",
    revision: 4,
    updated_at: "2026-08-20T10:00:00Z",
  },
];

const VERSIONS = [
  { version_num: 4, label: "Edited (v4)", created_at: "2026-08-20T10:00:00Z" },
  { version_num: 1, label: "Generated", created_at: "2026-08-18T10:00:00Z" },
];

const V1_DETAIL = { ...VERSIONS[1], content_html: "<p>original</p>" };

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset().mockImplementation((url: string) => {
    if (url.endsWith("/versions")) return Promise.resolve(VERSIONS);
    if (url.endsWith("/versions/1")) return Promise.resolve(V1_DETAIL);
    if (url.endsWith("/sections")) return Promise.resolve(SECTIONS);
    return Promise.resolve(null);
  });
  post.mockReset();
  put.mockReset().mockResolvedValue({
    section_key: KEY,
    revision: 5,
    version_num: 5,
    label: "Edited (v5)",
  });
});

async function pickV1() {
  const user = userEvent.setup();
  render(wrap(<SectionEditor documentId="d1" canEdit />));
  await screen.findByTestId("editor");
  await user.click(await screen.findByRole("combobox"));
  await user.click(await screen.findByRole("option", { name: /^v1/ }));
  await waitFor(() =>
    expect(screen.getByTestId("editor").dataset.html).toBe("<p>original</p>")
  );
  return user;
}

describe("SectionEditor version history", () => {
  it("loads the picked version into the editor, writable", async () => {
    await pickV1();

    const editor = screen.getByTestId("editor");
    expect(editor.dataset.html).toBe("<p>original</p>");
    expect(editor.dataset.readonly).toBe("false");
  });

  it("writes nothing when a version is picked", async () => {
    await pickV1();

    // Reading history must not append to it. Restoring via POST is what produced the
    // phantom "Restored v1" entries.
    expect(post).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });

  it("offers no separate restore affordance", async () => {
    await pickV1();

    expect(screen.queryByRole("button", { name: /restore/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /back to current/i })
    ).toBeNull();
  });

  it("enables Save immediately, since the old body differs from what is stored", async () => {
    await pickV1();

    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("commits the picked version through the normal save, on the live revision", async () => {
    const user = await pickV1();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(PATH, {
        html: "<p>original</p>",
        revision: 4,
      })
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps edits made on top of a picked version", async () => {
    const user = await pickV1();
    await user.click(screen.getByRole("button", { name: /simulate typing/i }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(PATH, {
        html: "<p>typed</p>",
        revision: 4,
      })
    );
  });

  it("returns to the live text once the save lands", async () => {
    const user = await pickV1();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByTestId("editor").dataset.html).toBe("<p>current</p>")
    );
  });
});
