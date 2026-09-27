import { Alert } from "@abbvie-unity/react";

/**
 * Surfaces the report's review flags (null method refs, missing values, ambiguous
 * Compendial, undecoded units, etc.). These are also written to the audit record;
 * showing them helps the author review before finalizing. `warn` → info alert,
 * `info` → subtle info alert.
 */
export type ReviewFlag = {
  severity: string; // "warn" | "info"
  area: string;
  message: string;
};

export function ReviewFlags({ flags }: { flags: ReviewFlag[] }) {
  if (!flags?.length) return null;
  return (
    <div className="flex flex-col gap-2">
      {flags.map((f) => (
        <Alert
          key={`${f.area}:${f.severity}:${f.message}`}
          status="info"
          subtitle={f.message}
        >
          {f.severity === "warn" ? "Review needed" : "Note"} · {f.area}
        </Alert>
      ))}
    </div>
  );
}
