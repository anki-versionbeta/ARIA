import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveAccessRequest,
  fetchAccessRequests,
  fetchMyAccessRequest,
  rejectAccessRequest,
  requestAccess,
} from "./access";
import {
  fetchAdministrators,
  fetchAdminUsers,
  fetchModules,
  setUserModules,
  setUserRole,
} from "./admin";
import { fetchCurrentUser, login, logout } from "./auth";
import {
  buildDocument,
  createDocument,
  fetchDocument,
  fetchDocuments,
  forkDocument,
  retryDocument,
} from "./documents";
import { ApiError } from "./http";
import {
  fetchSections,
  fetchVersion,
  fetchVersions,
  restoreVersion,
  saveSection,
} from "./sections";
import { fetchSilos } from "./silos";
import type { DocumentQuery } from "./types";

export const queryKeys = {
  currentUser: ["auth", "me"] as const,
  adminUsers: ["admin", "users"] as const,
  administrators: ["administrators"] as const,
  myAccessRequest: ["access-requests", "mine"] as const,
  accessRequests: ["admin", "access-requests"] as const,
  adminModules: ["admin", "modules"] as const,
  silos: ["silos"] as const,
  documents: (query: DocumentQuery) => ["documents", query] as const,
  document: (id: string) => ["documents", id] as const,
  sections: (id: string) => ["documents", id, "sections"] as const,
  versions: (id: string, key: string) =>
    ["documents", id, "sections", key, "versions"] as const,
  // Nested under versions() so invalidating the list also drops the cached bodies.
  version: (id: string, key: string, num: number) =>
    ["documents", id, "sections", key, "versions", num] as const,
};

export function useCurrentUser() {
  return useQuery({
    queryKey: queryKeys.currentUser,
    queryFn: fetchCurrentUser,
    // A 401 is a definitive "not logged in", so retrying only delays the redirect.
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      username,
      password,
    }: {
      username: string;
      password: string;
    }) => login(username, password),
    onSuccess: (user) => {
      queryClient.setQueryData(queryKeys.currentUser, user);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      queryClient.clear();
    },
  });
}

export function useSilos() {
  return useQuery({ queryKey: queryKeys.silos, queryFn: fetchSilos });
}

export function useAdminUsers(enabled = true) {
  // `enabled` keeps a non-admin from firing a request that can only 403.
  return useQuery({
    queryKey: queryKeys.adminUsers,
    queryFn: fetchAdminUsers,
    enabled,
  });
}

/** Who to ask for access. Shown to users with no modules, so it must not require any. */
export function useAdministrators(enabled = true) {
  return useQuery({
    queryKey: queryKeys.administrators,
    queryFn: fetchAdministrators,
    enabled,
    staleTime: 10 * 60 * 1000,
  });
}

export function useMyAccessRequest(enabled = true) {
  return useQuery({
    queryKey: queryKeys.myAccessRequest,
    queryFn: fetchMyAccessRequest,
    enabled,
  });
}

export function useRequestAccess() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ modules, note }: { modules: string[]; note?: string }) =>
      requestAccess(modules, note),
    onSuccess: (created) => {
      queryClient.setQueryData(queryKeys.myAccessRequest, created);
    },
  });
}

/** The admin queue. Refetched on window focus so a decision made elsewhere shows up. */
export function useAccessRequests(enabled = true) {
  return useQuery({
    queryKey: queryKeys.accessRequests,
    queryFn: fetchAccessRequests,
    enabled,
    refetchOnWindowFocus: true,
  });
}

function useDecisionMutation<TArgs, TResult>(
  mutationFn: (args: TArgs) => Promise<TResult>
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      // The queue shrinks, the badge on the menu drops, and an approval changed
      // somebody's modules — so the user list is stale too.
      queryClient.invalidateQueries({ queryKey: queryKeys.accessRequests });
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers });
      queryClient.invalidateQueries({ queryKey: queryKeys.currentUser });
    },
  });
}

export function useApproveAccessRequest() {
  return useDecisionMutation(
    ({
      requestId,
      modules,
      note,
    }: {
      requestId: string;
      modules?: string[];
      note?: string;
    }) => approveAccessRequest(requestId, modules, note)
  );
}

export function useRejectAccessRequest() {
  return useDecisionMutation(
    ({ requestId, note }: { requestId: string; note?: string }) =>
      rejectAccessRequest(requestId, note)
  );
}

export function useModules(enabled = true) {
  return useQuery({
    queryKey: queryKeys.adminModules,
    queryFn: fetchModules,
    enabled,
    staleTime: 60 * 60 * 1000,
  });
}

/** Both admin mutations return the updated row, so the list is patched in place
 *  rather than refetched — the table does not flicker while you tick boxes. */
function useAdminUserMutation<TArgs>(
  mutationFn: (args: TArgs) => Promise<import("./types").AdminUser>
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: (updated) => {
      queryClient.setQueryData<import("./types").AdminUser[]>(
        queryKeys.adminUsers,
        (rows) => rows?.map((row) => (row.id === updated.id ? updated : row))
      );
      // The change may have been to the admin's own access, and it decides what the
      // shell renders, so re-read the session too.
      queryClient.invalidateQueries({ queryKey: queryKeys.currentUser });
      queryClient.invalidateQueries({ queryKey: queryKeys.silos });
    },
  });
}

export function useSetUserRole() {
  return useAdminUserMutation(
    ({ userId, role }: { userId: string; role: import("./types").Role }) =>
      setUserRole(userId, role)
  );
}

export function useSetUserModules() {
  return useAdminUserMutation(
    ({ userId, modules }: { userId: string; modules: string[] }) =>
      setUserModules(userId, modules)
  );
}

export function useDocuments(query: DocumentQuery) {
  return useQuery({
    queryKey: queryKeys.documents(query),
    queryFn: () => fetchDocuments(query),
    placeholderData: (previous) => previous,
  });
}

export function useDocument(id: string) {
  return useQuery({
    queryKey: queryKeys.document(id),
    queryFn: () => fetchDocument(id),
    // Callers may not have an id yet — the shell asks before a document route is
    // matched — and requesting an empty id would hit the wrong endpoint.
    enabled: Boolean(id),
    retry: false,
    // Spec section 7: poll every 2s while work is outstanding, and stop once the
    // run reaches a terminal state so a finished document costs nothing.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 2000 : false;
    },
  });
}

export function useForkDocument() {
  return useMutation({ mutationFn: forkDocument });
}

export function useCreateDocument(siloId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => createDocument(siloId, file),
    onSuccess: () => {
      // The new run belongs in the history immediately.
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

export function useBuildDocument(documentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input?: unknown) => buildDocument(documentId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.document(documentId),
      });
    },
  });
}

export function useRetryDocument(documentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => retryDocument(documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.document(documentId),
      });
    },
  });
}

export function useSections(documentId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.sections(documentId),
    queryFn: () => fetchSections(documentId),
    enabled,
  });
}

export function useSaveSection(documentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sectionKey,
      html,
      revision,
    }: {
      sectionKey: string;
      html: string;
      revision: number;
    }) => saveSection(documentId, sectionKey, html, revision),
    onSuccess: (_saved, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.sections(documentId),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.versions(documentId, variables.sectionKey),
      });
    },
  });
}

export function useVersions(documentId: string, sectionKey: string | null) {
  return useQuery({
    queryKey: queryKeys.versions(documentId, sectionKey ?? ""),
    queryFn: () => fetchVersions(documentId, sectionKey as string),
    enabled: Boolean(sectionKey),
  });
}

/**
 * A single version's body, loaded into the editor so an older text can be read and
 * edited. Reading history never alters it: saving is what appends a version, through
 * the ordinary `useSaveSection` path.
 */
export function useVersion(
  documentId: string,
  sectionKey: string | null,
  versionNum: number | null
) {
  return useQuery({
    queryKey: queryKeys.version(documentId, sectionKey ?? "", versionNum ?? 0),
    queryFn: () =>
      fetchVersion(documentId, sectionKey as string, versionNum as number),
    enabled: Boolean(sectionKey) && versionNum !== null,
  });
}

export function useRestoreVersion(documentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sectionKey,
      versionNum,
    }: {
      sectionKey: string;
      versionNum: number;
    }) => restoreVersion(documentId, sectionKey, versionNum),
    onSuccess: (_saved, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.sections(documentId),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.versions(documentId, variables.sectionKey),
      });
    },
  });
}

export function isStaleRevision(error: unknown) {
  return error instanceof ApiError && error.status === 409;
}

export function isForbidden(error: unknown) {
  return error instanceof ApiError && error.status === 403;
}

export function isNotFound(error: unknown) {
  return error instanceof ApiError && error.status === 404;
}
