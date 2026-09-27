import { api } from "./http";
import type { CurrentUser } from "./types";

export function login(username: string, password: string) {
  // A 401 here means bad credentials, so it must not trigger the global
  // session-expired redirect.
  return api.post<CurrentUser>(
    "/auth/login",
    { username, password },
    { ignoreUnauthorized: true }
  );
}

export function logout() {
  return api.post<null>("/auth/logout");
}

export function fetchCurrentUser() {
  return api.get<CurrentUser>("/auth/me");
}
