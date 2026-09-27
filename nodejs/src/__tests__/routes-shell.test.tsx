/**
 * The app shell and the login route.
 *
 * These are the two things every other route test depends on, so they are pinned first:
 * `_app` decides whether you see the application at all, and it can only learn "not
 * signed in" from a failed `/auth/me` — the session is an httpOnly cookie, so there is no
 * token to inspect.
 *
 * Driven through the real router (see `harness.tsx` for why mocking `createFileRoute`
 * does not work here).
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ADMIN_USER,
  apiHandlers,
  PLAIN_USER,
  renderAt,
} from "@/__tests__/harness";

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

beforeEach(() => {
  get.mockReset().mockImplementation(apiHandlers());
  post.mockReset().mockResolvedValue({});
  put.mockReset().mockResolvedValue({});
  del.mockReset().mockResolvedValue(null);
});

describe("the login route", () => {
  it("offers a username and password to sign in with", async () => {
    await renderAt("/login");

    expect(await screen.findByText("ARIA")).toBeInTheDocument();
    expect(
      await screen.findByRole("textbox", { name: /username/i })
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /sign in/i })
    ).toBeInTheDocument();
  });

  it("keeps sign in disabled until both fields are filled", async () => {
    const user = userEvent.setup();
    await renderAt("/login");

    const submit = await screen.findByRole("button", { name: /sign in/i });
    expect(submit).toBeDisabled();

    await user.type(
      await screen.findByRole("textbox", { name: /username/i }),
      "asha.rao"
    );
    expect(submit).toBeDisabled();
  });

  it("posts the credentials the user typed", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(ADMIN_USER);
    await renderAt("/login");

    await user.type(
      await screen.findByRole("textbox", { name: /username/i }),
      "asha.rao"
    );
    const password = document.querySelector(
      'input[type="password"]'
    ) as HTMLInputElement;
    await user.type(password, "secret");
    await user.click(await screen.findByRole("button", { name: /sign in/i }));

    // The third argument matters: a 401 here is a wrong password, not an expired
    // session, so it must not trip the global sign-out redirect.
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/auth/login",
        { username: "asha.rao", password: "secret" },
        { ignoreUnauthorized: true }
      )
    );
  });
});

describe("the application shell", () => {
  it("shows the signed-in user once /auth/me answers", async () => {
    await renderAt("/");

    expect(await screen.findByText(/asha rao/i)).toBeInTheDocument();
  });

  it("asks the API who the caller is rather than reading a token", async () => {
    await renderAt("/");

    await waitFor(() => expect(get).toHaveBeenCalledWith("/auth/me"));
  });

  it("renders for a plain user as well as an administrator", async () => {
    get.mockImplementation(apiHandlers({}, PLAIN_USER));
    await renderAt("/");

    expect(await screen.findByText(/ben carter/i)).toBeInTheDocument();
  });
});
