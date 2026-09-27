import { api } from "./http";
import type { Administrator, AdminUser, Module, Role } from "./types";

/** Not admin-only: any signed-in user may see who could grant them access. */
export function fetchAdministrators() {
  return api.get<Administrator[]>("/auth/administrators");
}

export function fetchAdminUsers() {
  return api.get<AdminUser[]>("/admin/users");
}

/** Not admin-only: the access-request form needs it too. */
export function fetchModules() {
  return api.get<Module[]>("/modules");
}

export function fetchRoles() {
  return api.get<Module[]>("/admin/roles");
}

export function setUserRole(userId: string, role: Role) {
  return api.put<AdminUser>(`/admin/users/${userId}/role`, { role });
}

/** Sends the complete set, not a delta — mirrors what the checkboxes show. */
export function setUserModules(userId: string, modules: string[]) {
  return api.put<AdminUser>(`/admin/users/${userId}/modules`, { modules });
}
