import { Tag } from "@abbvie-unity/react";
import type { RunStatus } from "@/api/types";

const PRESENTATION: Record<
  RunStatus,
  {
    label: string;
    kind: "neutral" | "primary" | "warning" | "success" | "error";
    icon: string;
  }
> = {
  queued: { label: "Queued", kind: "neutral", icon: "clock" },
  running: { label: "Running", kind: "primary", icon: "spinner" },
  awaiting_user: { label: "Needs review", kind: "warning", icon: "user-pen" },
  complete: { label: "Complete", kind: "success", icon: "circle-check" },
  failed: { label: "Failed", kind: "error", icon: "circle-exclamation" },
};

export function StatusBadge({ status }: { status: RunStatus }) {
  const { label, kind, icon } = PRESENTATION[status];
  return (
    <Tag kind={kind} variant="filled" startIcon={icon}>
      {label}
    </Tag>
  );
}
