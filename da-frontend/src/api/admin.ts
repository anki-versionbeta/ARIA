import { api } from "./http";
import type { AdminUser, Module, Role } from "./types";

export function fetchAdminUsers() {
  return api.get<AdminUser[]>("/admin/users");
}

export function fetchModules() {
  return api.get<Module[]>("/admin/modules");
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
