import { describe, expect, it } from "vitest";
import type { Silo } from "@/api/types";
import { matchesTypeSearch } from "./silo-search";

function silo(id: string, label: string, accepts: string[] = [".pdf"]): Silo {
  return { id, label, accepts, stages: [] };
}

const ISO = silo("iso", "ISO Standard Assessment");
const BOP = silo("bop", "Batch Operating Procedure");
const ATR = silo(
  "mfg_atr",
  "ATR / MFGR — Analytical & Manufacturing Reports",
  []
);

describe("matchesTypeSearch", () => {
  it("matches everything when nothing is typed", () => {
    for (const s of [ISO, BOP, ATR]) {
      expect(matchesTypeSearch(s, "")).toBe(true);
      expect(matchesTypeSearch(s, "   ")).toBe(true);
    }
  });

  it("matches on the label, case-insensitively", () => {
    expect(matchesTypeSearch(BOP, "batch")).toBe(true);
    expect(matchesTypeSearch(BOP, "BATCH")).toBe(true);
    expect(matchesTypeSearch(BOP, "Operating")).toBe(true);
  });

  it("matches on the id, which is what people actually say", () => {
    // The label is "ATR / MFGR — Analytical & Manufacturing Reports"; searching the label
    // alone would miss the id people type.
    expect(matchesTypeSearch(ATR, "mfg_atr")).toBe(true);
    expect(matchesTypeSearch(ISO, "iso")).toBe(true);
  });

  it("narrows as more terms are added", () => {
    expect(matchesTypeSearch(ATR, "atr manufacturing")).toBe(true);
    expect(matchesTypeSearch(ATR, "atr batch")).toBe(false);
  });

  it("is not confused by punctuation between terms", () => {
    // "—", "/" and "&" sit between words in the label, so whole-string comparison would
    // fail where term-wise matching succeeds.
    expect(matchesTypeSearch(ATR, "atr mfgr")).toBe(true);
    expect(matchesTypeSearch(ATR, "analytical reports")).toBe(true);
  });

  it("excludes what does not match", () => {
    expect(matchesTypeSearch(ISO, "batch")).toBe(false);
    expect(matchesTypeSearch(BOP, "iso")).toBe(false);
    expect(matchesTypeSearch(ATR, "zzz")).toBe(false);
  });

  it("ignores surrounding whitespace in the query", () => {
    expect(matchesTypeSearch(ISO, "  iso  ")).toBe(true);
  });
});
