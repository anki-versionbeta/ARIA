/**
 * The display formatters, used across the document list, the file table and the section
 * tree. All pure, and all previously at 40% line coverage.
 *
 * `humanizeKey` and `humanizeSegment` are what turn a dotted section key into the labels
 * in the review screen's Sections panel, so their output is user-visible text rather than
 * an internal detail.
 */
import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatDateTime,
  humanizeKey,
  humanizeSegment,
} from "@/utils/format";

describe("formatBytes", () => {
  it("reports bytes below a kilobyte unchanged", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("switches to kilobytes at exactly 1024", () => {
    expect(formatBytes(1024)).toBe("1 KB");
  });

  it("rounds kilobytes to a whole number", () => {
    // 1536 is 1.5 KB; the list has no room for decimals at this scale.
    expect(formatBytes(1536)).toBe("2 KB");
    expect(formatBytes(2048)).toBe("2 KB");
  });

  it("switches to megabytes at exactly a megabyte", () => {
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
  });

  it("keeps one decimal place for megabytes", () => {
    // A real BOP upload is ~37 MB, so this is the branch users actually see.
    expect(formatBytes(37 * 1024 * 1024)).toBe("37.0 MB");
    expect(formatBytes(Math.round(2.5 * 1024 * 1024))).toBe("2.5 MB");
  });

  it("does not fall back to bytes for very large files", () => {
    expect(formatBytes(1024 * 1024 * 1024)).toBe("1024.0 MB");
  });
});

describe("formatDateTime", () => {
  it("renders an ISO timestamp as a readable local date and time", () => {
    const rendered = formatDateTime("2026-08-25T14:30:00Z");

    // The exact wording is locale-dependent, so assert on what must be present
    // rather than pinning a locale the CI machine may not share.
    expect(rendered).toMatch(/2026/);
    expect(rendered).not.toBe("Invalid Date");
    expect(rendered.length).toBeGreaterThan(8);
  });

  it("includes a time as well as a date", () => {
    const rendered = formatDateTime("2026-08-25T14:30:00Z");

    // dateStyle medium + timeStyle short, so there is a digit-separated clock value.
    expect(rendered).toMatch(/\d{1,2}[:.]\d{2}/);
  });

  it("renders the timestamps the API actually returns", () => {
    // The backend serialises without a Z suffix, e.g. "2026-08-25T04:10:29.651006".
    const rendered = formatDateTime("2026-08-25T04:10:29.651006");

    expect(rendered).not.toBe("Invalid Date");
    expect(rendered).toMatch(/2026/);
  });

  it("throws on an unparseable value rather than degrading", () => {
    // Documenting the real behaviour, not endorsing it: Intl.DateTimeFormat.format
    // raises RangeError on an invalid Date, so this would take down the component that
    // rendered it rather than showing a blank cell.
    //
    // Not reachable today - the only caller is section-editor.tsx passing
    // version.created_at, which the API types as a non-nullable string. It is worth
    // knowing because finished_at IS `string | null`, so formatting that field without
    // a guard would crash the row for every document still running.
    expect(() => formatDateTime("not a date")).toThrow();
    expect(() => formatDateTime("")).toThrow();
  });
});

describe("humanizeSegment", () => {
  it("replaces underscores and capitalises each word", () => {
    expect(humanizeSegment("spare_parts")).toBe("Spare Parts");
  });

  it("capitalises a single word", () => {
    expect(humanizeSegment("purpose")).toBe("Purpose");
  });

  it("leaves an already-capitalised word alone", () => {
    expect(humanizeSegment("Purpose")).toBe("Purpose");
  });

  it("handles several underscores", () => {
    expect(humanizeSegment("temperature_operating_range")).toBe(
      "Temperature Operating Range"
    );
  });

  it("returns an empty string unchanged", () => {
    expect(humanizeSegment("")).toBe("");
  });

  it("capitalises after a digit boundary", () => {
    // \b\w matches the start of each word, so trailing numbers survive.
    expect(humanizeSegment("iso_10993_1")).toBe("Iso 10993 1");
  });
});

describe("humanizeKey", () => {
  it("drops the top-level segment and joins the rest with a separator", () => {
    // The group heading already shows "Operating Procedure", so repeating it in every
    // item would be noise.
    expect(humanizeKey("operating_procedure.setup.equipment")).toBe(
      "Setup › Equipment"
    );
  });

  it("keeps a two-segment key readable", () => {
    expect(humanizeKey("description.spare_parts")).toBe("Spare Parts");
  });

  it("falls back to the whole key when there is only one segment", () => {
    // `purpose` and `scope` are single-segment leaves; dropping the first segment would
    // leave nothing to show.
    expect(humanizeKey("purpose")).toBe("Purpose");
    expect(humanizeKey("related_documents")).toBe("Related Documents");
  });

  it("handles a deeply nested key", () => {
    expect(humanizeKey("a.b.c.d")).toBe("B › C › D");
  });

  it("returns an empty string for an empty key", () => {
    expect(humanizeKey("")).toBe("");
  });
});
