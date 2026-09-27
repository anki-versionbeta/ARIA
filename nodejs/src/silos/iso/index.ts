import type { SiloModule } from "../registry";
import { IsoRangePicker } from "./iso-range-picker";

/**
 * ISO contributes only the clause range picker. Upload, progress and download come from
 * the shared screens, and there is nothing to edit — the app being replaced has no
 * section editor, so adding one would be a behaviour change rather than a port.
 */
const iso: SiloModule = {
  id: "iso",
  screens: {
    review: IsoRangePicker,
  },
};

export default iso;
