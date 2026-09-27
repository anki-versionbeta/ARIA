"""Scope rules for the PSA similarity universe.

Business decision (2026-07-17 meeting): prioritise COMMERCIAL and PIPELINE
activities and EXCLUDE clinical-stage products from the current assessment scope.

Where "clinical" lives in the data (confirmed by inspection, 2026-07-17):
the clinical/commercial axis is `product.batch_type` — values seen are
`Commercial`, `Pipeline`, `Clinical`, and NULL — at the PRESENTATION-ROW level.
It is NOT `product.status` (Pipeline/Commercial/Discontinued): a single program
(e.g. ABBV-400) carries BOTH Commercial and Clinical rows that differ only by
batch_type + cap colour. So scope is a per-presentation filter on batch_type.

Centralised here so the risk engine, any product listing, and the future
cap-colour logic all define "in scope" the same way. If the LIVE Smartsheet ever
uses different batch_type wording, adjust EXCLUDED_BATCH_TYPES only.
"""

# Clinical-stage presentations are out of scope. Matched case-insensitively.
EXCLUDED_BATCH_TYPES = {"CLINICAL"}


def is_in_scope(batch_type):
    """True unless the presentation is clinical-stage.
    NULL/blank counts as in scope (unknown ≠ clinical — conservative: keep it)."""
    return (batch_type or "").strip().upper() not in EXCLUDED_BATCH_TYPES


def scope_sql(alias="p"):
    """SQL predicate selecting in-scope presentations for a `product` row aliased `alias`.
    Usage: f"... WHERE {scope_sql('p2')}"."""
    col = f"{alias}.batch_type"
    excl = ",".join(f"'{b}'" for b in sorted(EXCLUDED_BATCH_TYPES))
    return f"({col} IS NULL OR UPPER(TRIM({col})) NOT IN ({excl}))"


def scope_reason(batch_type):
    """Human-readable explanation of the in/out decision (audit trail / G-5)."""
    if is_in_scope(batch_type):
        return f"in scope (batch_type={batch_type or 'unspecified'})"
    return f"excluded: clinical-stage presentation (batch_type={batch_type})"


def late_stage_sql(alias="p"):
    """SQL predicate for the Part D comparator universe (late-stage pipeline products).

    QPP11-04-001-G004 §1.3 defines a "late stage pipeline product" verbatim as:
        "Products that have completed primary stability lots manufacturing with
         approved commercial product image."
    The Smartsheet exposes no "completed primary stability lots" milestone, so this is a
    PROVISIONAL PROXY — in scope (non-clinical) AND not discontinued — a superset of the
    true definition. Tighten it (add a primary-stability / approved-image flag) once the
    data owners surface that signal; tracked in CLAUDE.md §5. See [[qpp-governing-procedure]]."""
    return f"({scope_sql(alias)} AND UPPER(TRIM(COALESCE({alias}.status,''))) <> 'DISCONTINUED')"
