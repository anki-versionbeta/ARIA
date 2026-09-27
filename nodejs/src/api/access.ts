import { api } from "./http";
import type { AccessRequest, AdminUser } from "./types";

/** The caller's own open request, or null when they have not asked for anything. */
export function fetchMyAccessRequest() {
  return api.get<AccessRequest | null>("/access-requests/mine");
}

export function requestAccess(modules: string[], note?: string) {
  return api.post<AccessRequest>("/access-requests", { modules, note });
}

export function fetchAccessRequests() {
  return api.get<AccessRequest[]>("/admin/access-requests");
}

/** Returns the requester's updated access, so the user list can be patched in place. */
export function approveAccessRequest(
  requestId: string,
  modules?: string[],
  note?: string
) {
  return api.post<AdminUser>(`/admin/access-requests/${requestId}/approve`, {
    modules,
    note,
  });
}

export function rejectAccessRequest(requestId: string, note?: string) {
  return api.post<AccessRequest>(`/admin/access-requests/${requestId}/reject`, {
    note,
  });
}
