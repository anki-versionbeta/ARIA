import { Alert } from "@abbvie-unity/react";
import { describeError, usePalette } from "../api/queries";
import { ColourSwatch, SwatchGrid } from "./colour-swatch";
import { PendingNote } from "./pending-note";

/**
 * The seal manufacturer's off-the-shelf palette. Clicking a colour shows which products already use it.
 *
 * A duplicate `Select` listing the same 40 colours was removed on request. Keyboard and screen-reader
 * access does not depend on it: each swatch is a real `<button>` carrying `aria-pressed` and a
 * focus-visible ring (`colour-swatch.tsx`), so the grid is fully operable on its own. The cost is
 * ergonomic rather than functional — the grid is ~40 tab stops with no shortcut past it, which is the
 * case for the roving-tabindex change already queued in CLAUDE.md §5.
 *
 * The colours are the shipped catalogue with any local edits applied; the editor below reports and can
 * undo those edits.
 */
export type PaletteGridProps = {
  vendor: string;
  selectedColor: string | null;
  onSelectColor: (color: string | null) => void;
};

export function PaletteGrid({
  vendor,
  selectedColor,
  onSelectColor,
}: PaletteGridProps) {
  const palette = usePalette(vendor);

  if (palette.isPending) {
    return <PendingNote>Loading the {vendor} palette…</PendingNote>;
  }

  if (palette.isError) {
    return (
      <Alert status="error" className="my-2">
        {describeError(palette.error, "Could not load the cap-colour palette.")}
      </Alert>
    );
  }

  const colors = palette.data?.colors ?? [];
  if (colors.length === 0) {
    return (
      <Alert status="info" icon="triangle-exclamation" className="my-2">
        No off-the-shelf {vendor} colours are in the palette.
      </Alert>
    );
  }

  return (
    <SwatchGrid>
      {colors.map((color) => (
        <ColourSwatch
          hex={color.hex}
          key={`${color.vendor}-${color.vendor_color_name}`}
          label={color.vendor_color_name}
          // Clicking the selected colour again clears it, so the products table can be dismissed
          // without a separate control now that the clearable Select is gone.
          onSelect={() =>
            onSelectColor(
              selectedColor === color.vendor_color_name
                ? null
                : color.vendor_color_name
            )
          }
          selected={selectedColor === color.vendor_color_name}
        />
      ))}
    </SwatchGrid>
  );
}
