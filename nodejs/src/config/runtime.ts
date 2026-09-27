/**
 * Runtime configuration, fetched at boot rather than baked in at build time.
 *
 * Vite inlines `import.meta.env` values into the bundle, which would force one
 * image per environment. Reading this file at startup instead lets a single
 * built image be promoted from dev to production with only the file swapped.
 */
export type RuntimeConfig = {
  apiBaseUrl: string;
};

let loaded: RuntimeConfig | null = null;

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch("/config.json", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Unable to load /config.json (HTTP ${response.status})`);
  }
  loaded = (await response.json()) as RuntimeConfig;
  return loaded;
}

export function getRuntimeConfig(): RuntimeConfig {
  if (!loaded) {
    throw new Error("Runtime config read before loadRuntimeConfig() resolved");
  }
  return loaded;
}
