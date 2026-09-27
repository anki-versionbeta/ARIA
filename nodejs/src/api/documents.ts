import { getRuntimeConfig } from "@/config/runtime";
import { ApiError, api, buildQuery } from "./http";
import type {
  DocumentDetail,
  DocumentQuery,
  DocumentStatus,
  DocumentSummary,
  Paginated,
} from "./types";

export function fetchDocuments(query: DocumentQuery) {
  const search = buildQuery({
    user: query.user,
    silo: query.silo,
    status: query.status,
    q: query.q,
    sort: query.sort,
    order: query.order,
    page: query.page,
  });
  return api.get<Paginated<DocumentSummary>>(`/documents${search}`);
}

export function fetchDocument(id: string) {
  return api.get<DocumentDetail>(`/documents/${id}`);
}

export function forkDocument(id: string) {
  return api.post<{ id: string }>(`/documents/${id}/fork`);
}

export function fetchDocumentStatus(id: string) {
  return api.get<DocumentStatus>(`/documents/${id}/status`);
}

/** Resume a run parked for user action; the body becomes that stage's checkpoint. */
export function buildDocument(id: string, input?: unknown) {
  return api.post<{ id: string; status: string }>(
    `/documents/${id}/build`,
    input ?? {}
  );
}

export function retryDocument(id: string) {
  return api.post<{ id: string; status: string }>(`/documents/${id}/retry`);
}

/**
 * Multipart upload. Not routed through the JSON client because the browser must set
 * the multipart boundary itself.
 */
export async function createDocument(
  siloId: string,
  file: File
): Promise<{ id: string }> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(
    `${getRuntimeConfig().apiBaseUrl}/silos/${siloId}/documents`,
    { method: "POST", credentials: "include", body }
  );
  const payload = await response.text();
  if (!response.ok) {
    throw new ApiError(response.status, payload ? JSON.parse(payload) : null);
  }
  return JSON.parse(payload) as { id: string };
}
