/**
 * The recommendation panel, driven by REAL captured engine output (`__fixtures__/live-payloads.gen.ts`).
 *
 * The load-bearing case is the honesty gate. When no same-state comparator has a recorded cap, the
 * engine returns `min_delta_e: null` for every colour — there is then no distinctness signal at all
 * and the list is only palette order. Claiming "most visually distinct first" in that state would be
 * a false statement rendered to an assessor, so it is asserted in both directions.
 *
 * The same applies to the similarity-risk grades: they are asserted as the engine's values
 * (`cap_recommend._grade_taken`), never recomputed here, and the order is asserted because "highest
 * similarity risks" is a claim about ranking. No live presentation currently grades High — that needs
 * a comparator sharing BOTH the vial size and the shade — so High is asserted absent, not invented.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  recommendAbbv066,
  recommendBonte,
  recommendNoSignal,
} from "../api/__fixtures__/live-payloads.gen";
import type { RecommendResult } from "../api/types";
import { RecommendationPanel } from "./recommendation-panel";

describe("RecommendationPanel — BoNT/E (documented known-good, real ΔE values)", () => {
  it("names the product and the presentation it is recommending for", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    expect(screen.getByText("AGN-151586 (BoNT/E)")).toBeInTheDocument();
    expect(
      screen.getByText("Presentation: 2R (2.00 mL) vial")
    ).toBeInTheDocument();
  });

  it("reports the state and co-located count the comparison was scoped by", () => {
    const { container } = render(
      <RecommendationPanel result={recommendBonte} />
    );
    // Scoping is the whole basis of the answer, so it has to be on screen, not implied.
    expect(container).toHaveTextContent("State: Lyo-Cake");
    expect(container).toHaveTextContent("Vial size: 2R (2.00 mL) vial");
    expect(container).toHaveTextContent("4 same-state co-located product(s)");
    expect(container).toHaveTextContent(
      "Allergan Pharmaceuticals, County Mayo, Westport"
    );
  });

  it("shows the existing custom cap as already selected, not as a recommendation", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    expect(
      screen.getByText("✓ Already selected (current cap):")
    ).toBeInTheDocument();
    // Exactly once: under the "already selected" heading and NOT also as a recommended colour —
    // which is the claim this test's name makes. (The heading says "already selected", so the tile
    // does not repeat it as a sublabel.)
    expect(screen.getAllByText("Magenta 2063C (L9320)")).toHaveLength(1);
  });

  it("claims a distinctness ranking only because real ΔE values came back", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    expect(
      screen.getByText("✓ Recommended (most visually distinct first)")
    ).toBeInTheDocument();
    // The "no signal" disclaimer must be absent when there IS a signal.
    expect(screen.queryByText(/in palette order/)).not.toBeInTheDocument();
  });

  it("renders every recommended colour with its ΔE, greens and turquoise first", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    expect(screen.getByText("Green 6007")).toBeInTheDocument();
    expect(screen.getByText("ΔE 96.6")).toBeInTheDocument();
    expect(screen.getByText("Turquoise 6012")).toBeInTheDocument();
    expect(screen.getByText("ΔE 89.4")).toBeInTheDocument();
    for (const name of [
      "Green 6042",
      "White 6003",
      "Lime 6013",
      "Turquoise 6066",
    ]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  it("grades every already-utilised cap as a similarity risk, with its metadata", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    expect(screen.getByText("Highest similarity risks")).toBeInTheDocument();
    // Botox's red/purple/orange are at OTHER vial sizes AND far from the magenta cap in ΔE, so the
    // engine grades all three Low — and none of them is blocked.
    expect(screen.getAllByText("Low similarity risk")).toHaveLength(3);
    expect(screen.queryByText("High similarity risk")).not.toBeInTheDocument();
    expect(screen.getAllByText("Botox")).toHaveLength(3);
    expect(screen.getByText("Purple L7250 · custom")).toBeInTheDocument();
    expect(screen.getByText("ΔE 48.6 to the current cap")).toBeInTheDocument();
    // Nothing shares this presentation's vial size, so no card may claim it does.
    expect(screen.queryByText(/same as this one/)).not.toBeInTheDocument();
    expect(screen.getByText("✗ Discouraged (same colour × vial size)"));
    expect(screen.getByText("None.")).toBeInTheDocument();
  });

  it("prints the engine's reason for each grade rather than a bare label", () => {
    const { container } = render(
      <RecommendationPanel result={recommendBonte} />
    );
    // The reason is `cap_recommend._grade_taken`'s own wording — the screen never re-derives it.
    expect(container).toHaveTextContent(
      "Different vial size (5ML vs 2R) and a clearly different shade from the current cap (ΔE 48.6)."
    );
  });
});

describe("RecommendationPanel — ABBV-066, a subject whose own cap is on file", () => {
  it("ranks the same-vial comparators above the rest", () => {
    render(<RecommendationPanel result={recommendAbbv066} />);
    // Same vial size ⇒ the container alone does not distinguish the two ⇒ Medium, sorted first.
    const grades = screen
      .getAllByText(/similarity risk$/)
      .map((node) => node.textContent);
    expect(grades).toEqual([
      "Medium similarity risk",
      "Medium similarity risk",
      "Low similarity risk",
      "Low similarity risk",
      "Low similarity risk",
    ]);
  });

  it("flags which comparators share this presentation's vial size", () => {
    render(<RecommendationPanel result={recommendAbbv066} />);
    expect(screen.getAllByText("· same as this one")).toHaveLength(2);
    expect(screen.getByText("Teal 6032")).toBeInTheDocument();
    expect(screen.getByText("ABBV-951 (Vyalev)")).toBeInTheDocument();
    expect(screen.getAllByText("Liquid")).toHaveLength(5);
  });

  it("explains a Medium grade as vial size, not shade", () => {
    const { container } = render(
      <RecommendationPanel result={recommendAbbv066} />
    );
    expect(container).toHaveTextContent(
      "Same vial size (10R), so the container alone does not distinguish the two"
    );
    expect(container).toHaveTextContent("This colour is blocked at 10R.");
  });

  it("does not badge a first choice when the engine reserved none", () => {
    render(<RecommendationPanel result={recommendBonte} />);
    // `first_unique` is null here (a cap is already selected), so nothing may claim to be reserved.
    expect(screen.queryByText("1st choice · unique")).not.toBeInTheDocument();
  });

  it("carries the engine's provisional-recommendation caveat through to the screen", () => {
    const { container } = render(
      <RecommendationPanel result={recommendBonte} />
    );
    expect(container).toHaveTextContent("SME approves the final colour");
  });
});

describe("RecommendationPanel — ABBV-151, no comparator cap on file (all ΔE null)", () => {
  it("drops the ranking claim from the heading", () => {
    render(<RecommendationPanel result={recommendNoSignal} />);
    expect(screen.getByText("✓ Recommended")).toBeInTheDocument();
    expect(
      screen.queryByText(/most visually distinct first/)
    ).not.toBeInTheDocument();
  });

  it("explains that nothing is ruled out and the order is only palette order", () => {
    const { container } = render(
      <RecommendationPanel result={recommendNoSignal} />
    );
    expect(container).toHaveTextContent(
      "No comparator cap is recorded at these site(s)"
    );
    expect(container).toHaveTextContent("these are in palette order");
  });

  it("labels each colour 'free' rather than inventing a ΔE", () => {
    render(<RecommendationPanel result={recommendNoSignal} />);
    // Counted FROM the fixture, not hard-coded. This asserted 6 — the old `DEFAULT_TOP_N` — and broke
    // when merging both suppliers' catalogues raised it to 10. The claim being made is "every returned
    // colour is labelled free", which is what this expresses; the list's length is not the point.
    expect(screen.getAllByText("free")).toHaveLength(
      recommendNoSignal.recommended?.length ?? 0
    );
    // Anchored to a swatch SUBLABEL ("ΔE 96.6"), not to the substring anywhere on screen. The loose
    // version broke once the engine's caveat began explaining that cross-supplier ΔE is weaker evidence —
    // prose that legitimately contains "ΔE". The claim here is that no COLOUR shows a distance it does
    // not have, so the assertion has to be about a value, not about the two characters.
    expect(screen.queryByText(/^ΔE\s[\d.]+$/)).not.toBeInTheDocument();
  });

  it("still badges the reserved first choice the engine did return", () => {
    render(<RecommendationPanel result={recommendNoSignal} />);
    expect(screen.getByText("Transparent 6001")).toBeInTheDocument();
    expect(screen.getByText("1st choice · unique")).toBeInTheDocument();
  });

  it("says plainly that there is nothing this presentation could be mixed up with", () => {
    const { container } = render(
      <RecommendationPanel result={recommendNoSignal} />
    );
    expect(container).toHaveTextContent(
      "No cap is recorded on any co-located, same-state presentation"
    );
    // With no comparator cap there is no grade to show either.
    expect(screen.queryByText(/similarity risk$/)).not.toBeInTheDocument();
  });

  it("shows the unresolved 'TBC' cap as the current one, not as a selection", () => {
    render(<RecommendationPanel result={recommendNoSignal} />);
    expect(screen.getByText("Current cap:")).toBeInTheDocument();
    expect(screen.getByText("TBC")).toBeInTheDocument();
    expect(
      screen.queryByText("✓ Already selected (current cap):")
    ).not.toBeInTheDocument();
  });
});

describe("RecommendationPanel — degraded engine responses", () => {
  it("surfaces the engine's error instead of an empty panel", () => {
    const result: RecommendResult = {
      error: "psa.db has not been built — click Refresh from Smartsheet.",
    };
    render(<RecommendationPanel result={result} />);
    expect(
      screen.getByText(
        "psa.db has not been built — click Refresh from Smartsheet."
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/Recommended/)).not.toBeInTheDocument();
  });

  it("explains itself when the presentation has no manufacturing site", () => {
    const result: RecommendResult = {
      program_label: "ABBV-999 (Test)",
      presentation_label: "2R",
      sites: [],
      note: "No manufacturing site is recorded for this presentation.",
      recommended: [],
    };
    render(<RecommendationPanel result={result} />);
    expect(
      screen.getByText(
        "No manufacturing site is recorded for this presentation."
      )
    ).toBeInTheDocument();
    // With nothing to compare against, offering a ranked list would be meaningless.
    expect(screen.queryByText(/^✓ Recommended/)).not.toBeInTheDocument();
  });

  it("says so when the palette has no free colour left", () => {
    const result: RecommendResult = {
      program_label: "ABBV-999 (Test)",
      presentation_label: "2R",
      sites: ["ABB (ABB)"],
      recommended: [],
      taken: [{ color: "Blue", vial_key: "2R", product: "ABBV-066" }],
    };
    render(<RecommendationPanel result={result} />);
    expect(
      screen.getByText("No off-the-shelf colour is free — review.")
    ).toBeInTheDocument();
  });
});

describe("RecommendationPanel — suppliers are ranked together", () => {
  /**
   * Since 2026-08-21 the recommendation spans both catalogues and the supplier is an OUTPUT of the
   * colour decision, so every tile has to say which supplier its colour comes from. Without that,
   * "Green 6007" and "Green 3768" read as two shades of green rather than two suppliers' greens.
   */
  it("names the supplier on every recommended colour", () => {
    render(<RecommendationPanel result={recommendBonte} />);

    const suppliers = new Set(
      (recommendBonte.recommended ?? []).map((colour) => colour.vendor)
    );
    expect(suppliers.size).toBeGreaterThan(1); // the fixture really is cross-supplier
    for (const supplier of suppliers) {
      expect(screen.getAllByText(String(supplier)).length).toBeGreaterThan(0);
    }
  });

  it("shows the same hue from both suppliers as separate options", () => {
    /**
     * 12 canonical colours exist in both catalogues, so a hue legitimately appears twice — one entry per
     * supplier. That is the point of ranking per supplier row: it lets an assessor compare the two
     * catalogues' takes on the same colour. Each must therefore be its own tile.
     */
    render(<RecommendationPanel result={recommendBonte} />);

    const byHue = new Map<string, number>();
    for (const colour of recommendBonte.recommended ?? []) {
      const hue = (colour.color ?? "").toUpperCase();
      byHue.set(hue, (byHue.get(hue) ?? 0) + 1);
    }
    const repeated = [...byHue.entries()].filter(([, n]) => n > 1);
    expect(repeated.length).toBeGreaterThan(0);

    // Every entry is rendered under its own supplier-qualified name, so none is collapsed away.
    for (const colour of recommendBonte.recommended ?? []) {
      expect(
        screen.getAllByText(colour.vendor_color_name).length
      ).toBeGreaterThan(0);
    }
  });
});
