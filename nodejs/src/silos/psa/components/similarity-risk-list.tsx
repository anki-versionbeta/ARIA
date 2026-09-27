import { Caption, Tag } from "@abbvie-unity/react";
import type { TakenColor } from "../api/types";
import { ColourSwatch } from "./colour-swatch";

/**
 * The caps already in use at the subject's manufacturing site(s) — i.e. the presentations this one
 * could be mixed up with — as risk cards, highest risk first.
 *
 * The grading is the engine's (`cap_recommend._grade_taken`): same vial size + a similar shade of cap
 * colour ⇒ High, either one ⇒ Medium, neither ⇒ Low, with a written reason. Nothing is recomputed here —
 * ΔE is CIELAB maths in `cap_colors.delta_e`, and the thresholds are SME-tunable module constants.
 *
 * `risk`/`risk_reason` are optional-typed like every other engine field, so an older engine simply
 * renders the same cards without a grade rather than crashing or showing "undefined".
 */

const RISK_KIND: Record<string, "error" | "warning" | "success"> = {
  High: "error",
  Medium: "warning",
  Low: "success",
};

/**
 * Column floor is much wider than `SwatchGrid`'s: these cards carry the metadata (presentation, vial
 * size, cap colour, state, shade distance) that the plain 6rem chip could not fit.
 */
const CARD_GRID =
  "grid list-none grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-2 p-0";

export function SimilarityRiskList({ entries }: { entries: TakenColor[] }) {
  return (
    <ul className={CARD_GRID}>
      {entries.map((entry) => (
        <SimilarityRiskCard
          entry={entry}
          // Keyed on the Smartsheet row plus the cap: duplicate Smartsheet rows yield entries whose
          // product/colour/vial size are all identical, so `source_row` is what actually tells them apart.
          key={`${entry.source_row ?? entry.product}|${entry.raw ?? entry.color}|${entry.vial_key}`}
        />
      ))}
    </ul>
  );
}

function SimilarityRiskCard({ entry }: { entry: TakenColor }) {
  const kind = (entry.risk && RISK_KIND[entry.risk]) || "neutral";
  const vial = entry.vial_size || entry.vial_key || "—";
  const shade =
    entry.delta_e_to_subject === null || entry.delta_e_to_subject === undefined
      ? "not compared"
      : `ΔE ${entry.delta_e_to_subject} to the current cap`;

  return (
    <li className="rounded-md border border-container border-solid p-2">
      {/* Left-aligned: this tile stands alone in a card ~3x its width, and the metadata list
          below it is left-aligned — a centred chip reads as detached from its own label. */}
      <ColourSwatch
        align="left"
        hex={entry.hex}
        label={entry.color || "—"}
        size="small"
        // The Smartsheet text behind the canonical colour, when it says something more (a vendor code,
        // or an off-palette colour the normaliser had to map).
        sublabel={
          entry.raw && entry.raw !== entry.color
            ? `${entry.raw}${entry.is_custom ? " · custom" : ""}`
            : entry.is_custom
              ? "custom"
              : null
        }
      />

      {entry.risk ? (
        <div className="mt-1">
          <Tag kind={kind} variant="filled">
            {entry.risk} similarity risk
          </Tag>
        </div>
      ) : null}

      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 text-sm">
        <dt className="text-muted">Presentation</dt>
        <dd className="m-0 break-words font-semibold">{entry.product}</dd>

        <dt className="text-muted">Vial size</dt>
        <dd className="m-0">
          {vial}
          {entry.same_vial ? (
            <strong className="font-semibold"> · same as this one</strong>
          ) : null}
        </dd>

        {entry.state ? (
          <>
            <dt className="text-muted">State</dt>
            <dd className="m-0">{entry.state}</dd>
          </>
        ) : null}

        <dt className="text-muted">Shade</dt>
        <dd className="m-0">{shade}</dd>
      </dl>

      {entry.risk_reason ? (
        <Caption className="mt-2 block text-muted">{entry.risk_reason}</Caption>
      ) : null}
    </li>
  );
}
