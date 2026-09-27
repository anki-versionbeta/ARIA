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
 * Inert in this harness: nothing reads the registry until the feature is copied into
 * `da-frontend/src/silos/psa/`, at which point this file is what makes it appear.
 */
const psaSilo: SiloModule = {
  id: "psa",
  screens: { upload: CapColourSelectionPage },
};

export default psaSilo;
