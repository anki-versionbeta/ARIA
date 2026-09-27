import type { Silo } from "@/api/types";

/**
 * Does this document type match what the user typed?
 *
 * Matches against the label *and* the id, because the ids are what people say out loud —
 * "iso", "bop", "atr" — while the labels are longer and formal ("ATR / MFGR — Analytical &
 * Manufacturing Reports"). Searching the label alone would miss "mfg_atr", and searching
 * the id alone would miss "manufacturing".
 *
 * Every whitespace-separated term must match somewhere, so "atr report" narrows rather than
 * widens. Punctuation in the labels (—, /, &) means term-wise matching is much more
 * forgiving than comparing whole strings.
 */
export function matchesTypeSearch(silo: Silo, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;

  const haystack = `${silo.label} ${silo.id}`.toLowerCase();
  return terms.every((term) => haystack.includes(term));
}
