import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  fetchVersions,
  restoreVersion,
  saveSection,
} from "./sections";
import { fetchSilos } from "./silos";
import type { DocumentQuery } from "./types";

export const queryKeys = {
  currentUser: ["auth", "me"] as const,
  silos: ["silos"] as const,
  documents: (query: DocumentQuery) => ["documents", query] as const,
  document: (id: string) => ["documents", id] as const,
  sections: (id: string) => ["documents", id, "sections"] as const,
  versions: (id: string, key: string) =>
    ["documents", id, "sections", key, "versions"] as const,
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
