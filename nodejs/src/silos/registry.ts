import type { ComponentType } from "react";

/**
 * A silo may contribute extra screens. Everything a silo does *not* override falls
 * back to the shared upload / progress / editor / result screens, so a simple silo can
 * ship no screens at all.
 */
export type SiloScreens = {
  /** Replaces the shared upload panel. */
  upload?: ComponentType<{ siloId: string }>;
  /** Extra panels shown on the document page while awaiting user action. */
  review?: ComponentType<{ documentId: string; canEdit: boolean }>;
  /** A settings screen reachable from the sidebar. */
  config?: ComponentType;
};

export type SiloModule = {
  id: string;
  screens?: SiloScreens;
};

/**
 * Discovered by glob, so adding a silo means adding a folder — no file here changes.
 * Intersected with GET /api/silos at the call site, so a silo shipped dark via
 * ENABLED_SILOS never appears even if its screens are bundled.
 */
const modules = import.meta.glob<{ default: SiloModule }>("./*/index.ts", {
  eager: true,
});

export const siloModules: Record<string, SiloModule> = Object.fromEntries(
  Object.values(modules).map((module) => [module.default.id, module.default])
);

export function getSiloModule(siloId: string): SiloModule | undefined {
  return siloModules[siloId];
}
