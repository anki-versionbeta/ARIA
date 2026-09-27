import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReviewFlags } from "@/silos/mfg_atr/review-flags";

/**
 * ReviewFlags surfaces the generate-stage review remarks the author reads before
 * finalizing/downloading. It had no dedicated test — it was only exercised indirectly
 * through the edit panel — so this pins its rendering rules directly.
 *
 * It also covers the FB4 change: the "Actual Value" note is no longer amber-shaded in the
 * PDF; it is delivered as a review flag and must show up here as a pre-download remark.
 *
 * Assertions read the container's text content rather than matching individual nodes,
 * because the Alert renders the label, area and message as sibling nodes and a regex
 * getByText would match both the leaf and its ancestor.
 */
describe("ReviewFlags", () => {
  it("renders nothing when there are no flags", () => {
    const { container } = render(<ReviewFlags flags={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when flags is undefined", () => {
    // The panel may hand it an absent list before the review payload has loaded.
    const { container } = render(
      <ReviewFlags flags={undefined as unknown as never} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("labels a warn flag as needing review and shows its area and message", () => {
    const { container } = render(
      <ReviewFlags
        flags={[
          { severity: "warn", area: "results", message: "Missing value for pH value" },
        ]}
      />
    );
    expect(container).toHaveTextContent("Review needed");
    expect(container).toHaveTextContent("results");
    expect(container).toHaveTextContent("Missing value for pH value");
  });

  it("labels a non-warn flag as a note rather than needing review", () => {
    const { container } = render(
      <ReviewFlags
        flags={[{ severity: "info", area: "methods", message: "No method reference" }]}
      />
    );
    expect(container).toHaveTextContent("Note");
    expect(container).not.toHaveTextContent("Review needed");
    expect(container).toHaveTextContent("No method reference");
  });

  it("surfaces the Actual-Value remark as a pre-download note (FB4)", () => {
    // FB4: the amber Actual-Value shading was removed from the PDF and re-issued as a
    // review remark. The author must see it here before downloading the report.
    const { container } = render(
      <ReviewFlags
        flags={[
          {
            severity: "warn",
            area: "results",
            message:
              "Actual Value used where a Reported Value was unavailable — verify before finalizing.",
          },
        ]}
      />
    );
    expect(container).toHaveTextContent("Actual Value used");
    expect(container).toHaveTextContent("Review needed");
  });

  it("renders one alert per flag", () => {
    render(
      <ReviewFlags
        flags={[
          { severity: "warn", area: "results", message: "First remark" },
          { severity: "info", area: "methods", message: "Second remark" },
          { severity: "warn", area: "units", message: "Third remark" },
        ]}
      />
    );
    expect(screen.getByText("First remark")).toBeInTheDocument();
    expect(screen.getByText("Second remark")).toBeInTheDocument();
    expect(screen.getByText("Third remark")).toBeInTheDocument();
  });
});
