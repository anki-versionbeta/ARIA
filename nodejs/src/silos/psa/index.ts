import type { SiloModule } from "../registry";
import { CapColourSelectionPage } from "./cap-colour-selection-page";

/**
 * Silo registration for the platform's registry, which eager-globs every sibling folder's
 * `index.ts` and keys the result by the `id` below.
 *
 * PSA starts from an identifier (a product + presentation from the Smartsheet), not a file, so it
 * replaces the shared upload screen entirely — the same shape as the `mfg_atr` silo. `screens.upload`
 * is the platform's slot for "the first screen of this silo"; the name is the platform's, not a claim
 * that anything is uploaded here.
 *
 * Live: `registry.ts` eager-globs every sibling folder's `index.ts` in this app, so this file is
 * what makes PSA appear. Intersected with `GET /api/silos` at the call site, so PSA stays hidden
 * until the backend both registers the silo and grants the caller its module access.
 */
const psaSilo: SiloModule = {
  id: "psa",
  screens: { upload: CapColourSelectionPage },
};

export default psaSilo;
