import { Caption } from "@abbvie-unity/react";
import { cn } from "@/utils/cn";

/**
 * A cap-colour tile: the colour itself plus its name.
 *
 * The fill is a supplier hex from `cap_palette.csv` — real data, so it stays an inline style rather
 * than becoming a token. Everything around it (border, text, the missing-colour hatch) uses Unity
 * tokens so the tile themes in light and dark.
 *
 * The name is always rendered: colour alone must never be the only carrier of meaning, and the
 * shipped hex values are explicitly approximations pending measured swatches.
 */

const SIZES = {
  small: "h-9 w-9",
  medium: "h-10 w-10",
  large: "h-14 w-14",
} as const;

/**
 * Column floors, measured rather than guessed.
 *
 * A palette label is `Colour NNNN`, and the code is the colour's identity — "Blue 6043" and "Blue 6016"
 * are different products' caps — so breaking it across two lines makes the grid hard to scan. Measured
 * at the label's own 14px font, the widest shipped name ("Transparent 6001") needs **109.7px**; the
 * common ones need 75–95px. A tile spends 12px on its border and padding, so a 7.75rem (124px) floor
 * leaves ~112px of text and nothing wraps.
 *
 * The previous 5.25rem floor gave 74px of text, which fit "White 6003" (70.5px) but not "Yellow 6023"
 * (75.6px) — so some labels wrapped and some did not, which is what read as broken alignment.
 *
 * `small` stays narrow: it is used for single standalone tiles and for raw Smartsheet cap strings like
 * "Magenta 2063C (L9320)", which are long enough that wrapping is unavoidable and expected.
 *
 * Written out as whole class names so Tailwind's scanner can see them.
 */
const GRID_COLS = {
  small: "grid-cols-[repeat(auto-fill,minmax(6rem,1fr))]",
  medium: "grid-cols-[repeat(auto-fill,minmax(7.75rem,1fr))]",
  large: "grid-cols-[repeat(auto-fill,minmax(7.75rem,1fr))]",
} as const;

/**
 * Container for a set of tiles.
 *
 * A grid, not `flex flex-wrap`: with flex, each tile is only as wide as its own content and the trailing
 * gap of a row is dead space, so successive rows do not line up into columns. `auto-fill` + `1fr` divides
 * the row into equal columns instead, so every tile is the same width, the chips sit on a common vertical
 * axis, and the last row aligns with the ones above it.
 */
export function SwatchGrid({
  size = "medium",
  className,
  children,
}: {
  size?: keyof typeof SIZES;
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className={cn("grid gap-1", GRID_COLS[size], className)}>
      {children}
    </div>
  );
}

/** Diagonal hatch for a colour with no hex on file — visibly "unknown", not white. */
const NO_HEX_FILL =
  "repeating-linear-gradient(45deg, var(--un-background-02) 0 6px, var(--un-background-01) 6px 12px)";

export type ColourSwatchProps = {
  hex?: string | null;
  label: string;
  sublabel?: string | null;
  size?: keyof typeof SIZES;
  /** Draws the success-token frame used for the reserved first choice. */
  highlighted?: boolean;
  /**
   * `center` for a tile in a `SwatchGrid` — the chip sits on the column's centre line and the label
   * reserves two lines so neighbouring tiles align. `left` for a tile standing alone in a wider card,
   * where centring floats the chip away from its own label and the reserved line is a dead gap.
   */
  align?: "center" | "left";
  /** Renders the tile as a toggle button. Omit for a presentational tile. */
  onSelect?: () => void;
  selected?: boolean;
  className?: string;
  children?: React.ReactNode;
};

export function ColourSwatch({
  hex,
  label,
  sublabel,
  size = "medium",
  highlighted = false,
  align = "center",
  onSelect,
  selected = false,
  className,
  children,
}: ColourSwatchProps) {
  const chip = (
    <span
      aria-hidden="true"
      className={cn(
        SIZES[size],
        "block rounded-md border border-container border-solid",
        align === "center" && "mx-auto"
      )}
      style={
        hex
          ? { background: hex }
          : { background: NO_HEX_FILL, backgroundColor: "transparent" }
      }
    />
  );

  const body = (
    <>
      {chip}
      {/*
        `render={<span />}`: Caption is a div by default, which is not valid inside a <button>.

        `flex-1` instead of a fixed two-line reservation. Colour names are of very different lengths —
        "Red 6055" beside "Transparent 6001" — and a sublabel (the ΔE) has to line up across a row. Grid
        items already stretch to the tallest tile in their row, so letting the NAME absorb that slack
        pins every sublabel to a common bottom edge. A fixed `min-height` did the same job only when a
        name actually wrapped; when they were all one line it reserved a blank line and left a visible
        gap between each name and its ΔE (measured: 21.8px of dead space).
      */}
      <Caption className="mt-1 block flex-1 break-words" render={<span />}>
        {label}
      </Caption>
      {/*
        `children` (the reserved-first-choice badge) renders BEFORE the sublabel, so the sublabel is
        always the last line. The bottom of the tile is the common baseline, so anything after the
        sublabel would push that one tile's ΔE a line above its neighbours' — measured as a 16px step on
        the single badged tile.
      */}
      {children}
      {sublabel ? (
        <Caption className="block text-muted" render={<span />}>
          {sublabel}
        </Caption>
      ) : null}
    </>
  );

  /**
   * `w-full`: the enclosing `SwatchGrid` column sets the width, so every tile is identically wide.
   *
   * `flex flex-col` with NO `h-full`. A grid item already stretches to its row's height, which is what
   * gives the label's `flex-1` something to absorb; adding `h-full` also made the tile stretch when it
   * is NOT the grid item — inside a similarity-risk card it grew to the whole card and shoved its own
   * label to the bottom, over the metadata list. With height left to the layout, a tile nested in a
   * card stays content-sized and a tile in a grid still aligns with its neighbours.
   *
   * The border is always 2px and only its colour changes — a border that appears with `highlighted` or
   * `selected` would inset that one tile's contents and break the row's alignment.
   */
  const frame = cn(
    "flex w-full flex-col rounded-md border-2 border-transparent border-solid p-1",
    align === "center" ? "text-center" : "text-left",
    highlighted && "border-success",
    className
  );

  if (!onSelect) {
    return <div className={frame}>{body}</div>;
  }

  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        frame,
        "cursor-pointer bg-transparent",
        selected && "border-primary bg-selected",
        "hover:bg-02 focus-visible:outline-2 focus-visible:outline-focus focus-visible:outline-offset-2"
      )}
    >
      {body}
    </button>
  );
}
