import { describe, expect, it } from "vitest";
import { givenName } from "./given-name";

describe("givenName", () => {
  it("takes the name after the comma for the AD 'Surname, Firstname' form", () => {
    // The real case: taking the first word instead greets you as "Mohankumar,".
    expect(givenName("Mohankumar, Arjun")).toBe("Arjun");
  });

  it("handles a plain 'Firstname Surname'", () => {
    expect(givenName("Asha Rao")).toBe("Asha");
  });

  it("keeps only the first given name when there are several", () => {
    expect(givenName("Rao, Asha Priya")).toBe("Asha");
  });

  it("tolerates missing space after the comma", () => {
    expect(givenName("Mohankumar,Arjun")).toBe("Arjun");
  });

  it("falls back to the surname when nothing follows the comma", () => {
    expect(givenName("Mohankumar,")).toBe("Mohankumar");
  });

  it("returns a single word unchanged", () => {
    // display_name is the username when the directory read fails, so this is reachable.
    expect(givenName("mohanax25")).toBe("mohanax25");
  });

  it("gives nothing back for empty or absent input", () => {
    // The caller drops the name entirely rather than greeting a blank.
    expect(givenName("")).toBe("");
    expect(givenName("   ")).toBe("");
    expect(givenName(undefined)).toBe("");
    expect(givenName(null)).toBe("");
  });
});
