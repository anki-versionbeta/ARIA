import { Alert, Caption, VisuallyHidden } from "@abbvie-unity/react";
import { describeError, useSiteCounts } from "../api/queries";
import { PendingNote } from "./pending-note";

/**
 * In-scope products per manufacturing site.
 *
 * A list, not a chart component: the bar is decoration over text that already reads
 * "AP16 — 7 products", so it is `aria-hidden` and the row stays readable with styles off. Only the
 * bar length is data-driven (hence inline), and the fill is a Unity token rather than the Dash
 * screen's hard-coded blue.
 */
export function SiteBarChart() {
  const counts = useSiteCounts();

  if (counts.isPending) {
    return <PendingNote>Counting products per site…</PendingNote>;
  }

  if (counts.isError) {
    return (
      <Caption className="block text-muted">
        {describeError(
          counts.error,
          "No local data yet — click ↻ Refresh from Smartsheet."
        )}
      </Caption>
    );
  }

  const rows = counts.data ?? [];
  if (rows.length === 0) {
    return (
      <Alert status="info" icon="triangle-exclamation" className="my-2">
        No manufacturing-site data yet — click ↻ Refresh from Smartsheet.
      </Alert>
    );
  }

  const max = Math.max(...rows.map((row) => row.n_products)) || 1;

  return (
    <ul className="m-0 list-none p-0">
      {rows.map((row) => (
        <li className="my-1 flex items-center gap-2" key={row.site_code}>
          {/*
           * `title` because a "site code" is sometimes a full postal address — `split_sites`
           * collapses an address cell to one site node — and the label then truncates.
           * Widths flex so the row still fits a 320px viewport (a fixed w-36 + w-56 does not).
           */}
          <span
            className="w-20 shrink-0 truncate text-right text-xs sm:w-36"
            title={row.site_code}
          >
            {row.site_code}
          </span>
          <span aria-hidden="true" className="block min-w-0 max-w-56 flex-1">
            <span
              className="block h-4 rounded-sm bg-primary"
              style={{
                width: `${Math.max(2, (row.n_products / max) * 100)}%`,
              }}
            />
          </span>
          <span className="text-xs">
            {row.n_products}
            <VisuallyHidden> products</VisuallyHidden>
          </span>
        </li>
      ))}
    </ul>
  );
}
