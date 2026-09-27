import { Step, Stepper } from "@abbvie-unity/react";
import type { RunStatus } from "@/api/types";
import { humanizeSegment } from "@/utils/format";

type Props = {
  /** The silo's declared stage list, in order. */
  stages: string[];
  currentStage: string | null;
  status: RunStatus;
};

/**
 * Driven entirely by the silo's `STAGES` and the run's current stage, so no silo
 * supplies its own progress UI.
 */
export function StepProgress({ stages, currentStage, status }: Props) {
  const currentIndex = currentStage ? stages.indexOf(currentStage) : -1;

  return (
    <Stepper orientation="horizontal">
      {stages.map((stage, index) => (
        <Step
          key={stage}
          value={stage}
          status={stepStatus({ index, currentIndex, status })}
        >
          {humanizeSegment(stage)}
        </Step>
      ))}
    </Stepper>
  );
}

function stepStatus({
  index,
  currentIndex,
  status,
}: {
  index: number;
  currentIndex: number;
  status: RunStatus;
}): "complete" | "invalid" | "incomplete" {
  if (status === "complete") return "complete";
  // A failure is attributed to the stage that was running, not to the whole run.
  if (status === "failed" && index === currentIndex) return "invalid";
  if (currentIndex >= 0 && index < currentIndex) return "complete";
  return "incomplete";
}
