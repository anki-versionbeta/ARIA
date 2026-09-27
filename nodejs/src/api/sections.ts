import { api } from "./http";
import type {
  Section,
  SectionSaved,
  SectionVersion,
  SectionVersionDetail,
} from "./types";

export function fetchSections(documentId: string) {
  return api.get<Section[]>(`/documents/${documentId}/sections`);
}

export function saveSection(
  documentId: string,
  sectionKey: string,
  html: string,
  revision: number
) {
  return api.put<SectionSaved>(
    `/documents/${documentId}/sections/${encodeURIComponent(sectionKey)}`,
    { html, revision }
  );
}

export function fetchVersions(documentId: string, sectionKey: string) {
  return api.get<SectionVersion[]>(
    `/documents/${documentId}/sections/${encodeURIComponent(sectionKey)}/versions`
  );
}

export function fetchVersion(
  documentId: string,
  sectionKey: string,
  versionNum: number
) {
  return api.get<SectionVersionDetail>(
    `/documents/${documentId}/sections/${encodeURIComponent(sectionKey)}/versions/${versionNum}`
  );
}

export function restoreVersion(
  documentId: string,
  sectionKey: string,
  versionNum: number
) {
  return api.post<SectionSaved>(
    `/documents/${documentId}/sections/${encodeURIComponent(sectionKey)}/restore/${versionNum}`
  );
}
