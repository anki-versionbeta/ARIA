import type { SiloModule } from "../registry";
import { MfgAtrNewDocument } from "./mfg-atr-new-document";
import { ReportEditPanel } from "./report-edit-panel";

/**
 * This silo owns both ends of its flow. Creation takes an identifier rather than a file,
 * so the shared upload screen does not apply; and the pause exists to collect AcroForm
 * field values, which the generic "Build document" button would submit as an empty map.
 */
const mfgAtr: SiloModule = {
  id: "mfg_atr",
  screens: {
    upload: MfgAtrNewDocument,
    review: ReportEditPanel,
  },
};

export default mfgAtr;
