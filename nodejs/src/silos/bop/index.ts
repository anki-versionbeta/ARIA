import type { SiloModule } from "../registry";
import { BopConfig } from "./bop-config";

/**
 * BOP contributes only a prompt-configuration screen. Upload, progress, section
 * editing and download all come from the shared screens.
 */
const bop: SiloModule = {
  id: "bop",
  screens: {
    config: BopConfig,
  },
};

export default bop;
