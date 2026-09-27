import { useParams } from "@tanstack/react-router";
import { useDocument } from "@/api/queries";

/**
 * Which silo the user is currently looking at, or null on silo-agnostic screens
 * such as the history list.
 *
 * Derived from the route rather than held as state: `/silos/$siloId/*` names it
 * directly, and a document page resolves it from the document itself. That keeps
 * silo-specific navigation out of the way everywhere else — otherwise every silo's
 * settings would sit in the sidebar permanently.
 */
export function useCurrentSiloId(): string | null {
  // `strict: false` so this works from the shell, which is not inside either route.
  const params = useParams({ strict: false }) as {
    siloId?: string;
    documentId?: string;
  };

  // Reuses the document page's cached query, so this costs no extra request.
  const { data } = useDocument(params.documentId ?? "");

  if (params.siloId) return params.siloId;
  if (params.documentId) return data?.silo_id ?? null;
  return null;
}
