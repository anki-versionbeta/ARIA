import {
  Alert,
  Caption,
  Subhead3,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@abbvie-unity/react";
import { describeError, useProductsByColor } from "../api/queries";
import { PendingNote } from "./pending-note";

/**
 * Column headings. Spelled out rather than abbreviated — the underlying field is still `mfr_sites`, and
 * that wire name must not change: it is the engine's key (`psa/cap_queries.py`), it is declared in
 * `api/types.ts`, and it appears in the captured live fixtures.
 */
const COLUMNS = [
  "Product",
  "Contact",
  "Strength",
  "Vial size",
  "Form",
  "Manufacturing site(s)",
] as const;

/**
 * In-scope products already using one specific palette colour.
 *
 * "Specific" is the point: the server matches on the exact vendor code, not the canonical hue, so
 * picking "Blue 6043" lists the products on 6043 rather than every blue. Clinical presentations are
 * excluded upstream by `scope.py`.
 */
export type ProductsByColourTableProps = {
  vendor: string;
  colorName: string | null;
};

export function ProductsByColourTable({
  vendor,
  colorName,
}: ProductsByColourTableProps) {
  const products = useProductsByColor(vendor, colorName);

  if (!colorName) {
    return null;
  }

  const title = (
    <Subhead3 className="mt-2">Products using {colorName}</Subhead3>
  );

  if (products.isPending) {
    return (
      <>
        {title}
        <PendingNote>Looking up products…</PendingNote>
      </>
    );
  }

  if (products.isError) {
    return (
      <>
        {title}
        <Alert status="error" className="my-2">
          {describeError(
            products.error,
            "Could not read the analysis database — click ↻ Refresh from Smartsheet, then try again."
          )}
        </Alert>
      </>
    );
  }

  const rows = products.data ?? [];
  if (rows.length === 0) {
    return (
      <>
        {title}
        <Caption className="block text-muted">
          No in-scope product currently uses {colorName}.
        </Caption>
      </>
    );
  }

  return (
    <>
      {title}
      <Table bodyBorders="row" className="text-sm" wrapperClassName="my-2">
        <TableHeader>
          <TableRow>
            {COLUMNS.map((column) => (
              <TableHead key={column}>{column}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={`${row.product}-${row.vial_size}-${row.form}`}>
              <TableCell>{row.product}</TableCell>
              <TableCell>{row.contact}</TableCell>
              <TableCell>{row.strength}</TableCell>
              <TableCell>{row.vial_size}</TableCell>
              <TableCell>{row.form}</TableCell>
              <TableCell>{row.mfr_sites}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  );
}
