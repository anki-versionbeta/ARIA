import { Alert, Caption, H3, Subhead3 } from "@abbvie-unity/react";
import type { RecommendResult } from "../api/types";
import { ColourSwatch, SwatchGrid } from "./colour-swatch";
import { SimilarityRiskList } from "./similarity-risk-list";

/**
 * The cap-colour recommendation for one presentation.
 *
 * Reads the engine's dict as-is (`cap_recommend.recommend_program_presentations`), which is why every
 * field is treated as optional — the grading knobs are SME-tunable and the shape is allowed to grow.
 */

export type RecommendationPanelProps = {
  result: RecommendResult;
};

export function RecommendationPanel({ result }: RecommendationPanelProps) {
  if (result.error) {
    return (
      <Alert className="my-2" status="error">
        {result.error}
      </Alert>
    );
  }

  const subject = result.subject ?? {};
  const sites = result.sites ?? [];
  const recommended = result.recommended ?? [];
  const taken = result.taken ?? [];
  const discouraged = result.discouraged ?? [];

  /**
   * `min_delta_e` is null for every colour when no same-state comparator has a recorded cap. There is
   * then no distinctness signal at all, so the list is just palette order and the heading must not
   * claim a ranking.
   */
  const hasRankingSignal = recommended.some(
    (colour) => colour.min_delta_e !== null && colour.min_delta_e !== undefined
  );

  const header = (
    <>
      <Caption className="block text-muted">
        Vial size: {subject.vial_size || "—"} · State: {subject.state || "—"} ·
        Manufacturing site(s): {sites.join(", ") || "—"} ·{" "}
        {result.n_colocated ?? 0} same-state co-located product(s)
      </Caption>
      <CurrentCap result={result} />
    </>
  );

  return (
    <section className="my-4 border-primary border-l-4 border-solid pl-4">
      <H3 styledAs="h4" className="m-0">
        {result.program_label}
      </H3>
      <div className="my-2 rounded-md bg-02 px-3 py-2 font-semibold">
        Presentation: {result.presentation_label}
      </div>

      {header}

      {sites.length === 0 ? (
        <Alert className="my-2" icon="triangle-exclamation" status="info">
          {result.note ||
            "No manufacturing site is recorded for this presentation, so there is nothing to compare against."}
        </Alert>
      ) : (
        <>
          <Subhead3 className="mt-4 block">
            {hasRankingSignal
              ? "✓ Recommended (most visually distinct first)"
              : "✓ Recommended"}
          </Subhead3>
          {recommended.length === 0 ? (
            <Alert className="my-2" icon="triangle-exclamation" status="info">
              No off-the-shelf colour is free — review.
            </Alert>
          ) : (
            <>
              {!hasRankingSignal && (
                <Caption className="block text-muted">
                  No comparator cap is recorded at these site(s), so nothing is
                  ruled out and there is no distinctness ranking — these are in
                  palette order.
                </Caption>
              )}
              <SwatchGrid size="large">
                {recommended.map((colour, index) => {
                  const isReserved =
                    index === 0 &&
                    Boolean(result.first_unique) &&
                    colour.vendor_color_name === result.first_unique;
                  return (
                    <ColourSwatch
                      hex={colour.hex}
                      highlighted={isReserved}
                      // Both catalogues are ranked together, so the colour name alone is no longer a
                      // unique identity — the supplier is part of it.
                      key={`${colour.vendor ?? "?"}-${colour.vendor_color_name}`}
                      label={colour.vendor_color_name}
                      size="large"
                      sublabel={
                        colour.min_delta_e === null ||
                        colour.min_delta_e === undefined
                          ? "free"
                          : `ΔE ${colour.min_delta_e}`
                      }
                    >
                      {/*
                        The supplier is an OUTPUT of the colour decision now that both catalogues compete,
                        so every tile has to say which one it comes from — otherwise "Green 6007" and
                        "Green 3768" look like two shades rather than two suppliers. Rendered on every
                        tile, not just the ambiguous ones, so the rows stay aligned.
                      */}
                      {colour.vendor ? (
                        <Caption className="block text-muted">
                          {colour.vendor}
                          {colour.off_the_shelf === false ? " · not stock" : ""}
                        </Caption>
                      ) : null}
                      {isReserved ? (
                        <Caption className="block font-bold text-success">
                          1st choice · unique
                        </Caption>
                      ) : null}
                    </ColourSwatch>
                  );
                })}
              </SwatchGrid>
            </>
          )}

          <Subhead3 className="mt-4 block">Highest similarity risks</Subhead3>
          {taken.length === 0 ? (
            <Caption className="block text-muted">
              No cap is recorded on any co-located, same-state presentation, so
              there is nothing this one could be mixed up with.
            </Caption>
          ) : (
            <>
              <Caption className="block text-muted">
                Caps already in use at these site(s), ranked by how easily each
                could be mixed up with this presentation — the two drivers are
                the same vial size and a similar shade of cap colour.
              </Caption>
              <SimilarityRiskList entries={taken} />
            </>
          )}

          <Subhead3 className="mt-4 block">
            ✗ Discouraged (same colour × vial size)
          </Subhead3>
          {discouraged.length === 0 ? (
            <Caption className="block text-muted">None.</Caption>
          ) : (
            <ul className="my-1 text-sm">
              {discouraged.map((entry) => (
                <li key={entry.vendor_color_name}>
                  {entry.vendor_color_name} — {entry.reason}
                </li>
              ))}
            </ul>
          )}

          {result.note ? (
            <Caption className="mt-4 block text-muted">{result.note}</Caption>
          ) : null}
        </>
      )}
    </section>
  );
}

/** The subject's own cap: "already selected" when one is chosen, otherwise whatever is on file. */
function CurrentCap({ result }: { result: RecommendResult }) {
  const selected = result.subject_selected;
  const caps = result.subject?.caps_display ?? [];

  /*
    `align="left"` throughout: these are one or two tiles under a left-aligned heading, not a full grid.
    Centred, the chip floats into the middle of its column — measured 40px to the right of the heading
    and its own label — which reads as belonging to nothing.
  */
  if (selected) {
    return (
      <>
        <Caption className="mt-2 block font-bold text-success">
          ✓ Already selected (current cap):
        </Caption>
        <SwatchGrid size="small">
          <ColourSwatch
            align="left"
            hex={selected.hex}
            label={selected.raw || selected.canonical || "—"}
            size="small"
          />
        </SwatchGrid>
      </>
    );
  }

  return (
    <>
      <Caption className="mt-2 block font-semibold">Current cap:</Caption>
      <SwatchGrid size="small">
        {caps.length === 0 ? (
          <ColourSwatch
            align="left"
            label="none recorded"
            size="small"
            sublabel="current"
          />
        ) : (
          caps.map((cap) => (
            <ColourSwatch
              align="left"
              hex={cap.hex}
              // Keyed on the content: two identical caps would render identically anyway.
              key={`${cap.raw ?? ""}|${cap.canonical ?? ""}|${cap.hex ?? ""}`}
              label={cap.raw || "—"}
              size="small"
              sublabel="current"
            />
          ))
        )}
      </SwatchGrid>
    </>
  );
}
