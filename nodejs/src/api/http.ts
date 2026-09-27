import { getRuntimeConfig } from "@/config/runtime";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown
  ) {
    super(`API request failed with HTTP ${status}`);
    this.name = "ApiError";
  }
}

/**
 * The human-readable part of a FastAPI error, or null if there isn't one.
 *
 * FastAPI puts it in `detail`, either as a plain string or as an object carrying a
 * `message` alongside a machine-readable `reason`. Callers kept reaching for
 * `error.detail`, which does not exist — the parsed body lives on `body` — so the useful
 * text was silently dropped and the generic fallback shown instead.
 */
export function apiErrorMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = (error.body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return null;
}

type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

/**
 * Registered once by the router so an expired session anywhere in the app lands
 * the user on /login. The session is an httpOnly cookie, so expiry is only ever
 * observable as a 401 from the server.
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler) {
  unauthorizedHandler = handler;
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  /** Set on the login call, where a 401 means "bad password", not "session expired". */
  ignoreUnauthorized?: boolean;
};

async function request<T>(
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  const { method = "GET", body, ignoreUnauthorized = false } = options;

  const response = await fetch(`${getRuntimeConfig().apiBaseUrl}${path}`, {
    method,
    credentials: "include",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const payload = await readBody(response);

  if (!response.ok) {
    if (response.status === 401 && !ignoreUnauthorized) {
      unauthorizedHandler?.();
    }
    throw new ApiError(response.status, payload);
  }

  return payload as T;
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function buildQuery(
  params: Record<string, string | number | undefined>
) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body }),
};
