import {
  Accordion,
  AccordionItem,
  Alert,
  Button,
  Caption,
  P,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tag,
} from "@abbvie-unity/react";
import { useState } from "react";
import { downloadGeneratedFile, downloadRunFile } from "../api/client";
import {
  describeError,
  useCreateRun,
  useGenerateReport,
  useRunResult,
  useRunStatus,
} from "../api/queries";
import type { ReportResult, RiskComparator, RiskResult } from "../api/types";
import { PendingNote } from "./pending-note";

/**
 * Generate and download the PSA similarity report for the product + presentation picked above.
 *
 * Fully automated: there is no file to upload — the product and presentation picked above are the whole
 * input.
 *
 * **Two paths to the same result, and which one runs depends on where the screen is hosted.** Inside ARIA
 * this queues a real job, which is what makes the assessment appear in the history tab; the worker does the
 * work and the finished detail comes back from the run. Standalone there is no platform to queue on —
 * `psa/runs.py` needs platform types, so the shim has no `/runs` route at all — and a 404 there falls back
 * to the synchronous route the screen has always used.
 *
 * The fallback is not defensive coding for its own sake: the standalone harness is how this feature is
 * developed and tested, so it has to keep working. Both paths return the same `ReportResult` shape, which
 * is why everything below renders from one code path.
 */

/** `workflow.identify()`'s reason codes, in the words the Dash screen used. */
const BASIS: Record<string, string> = {
  "code-in-filename": "product code in the filename",
  "code-in-filename-not-in-catalogue":
    "product code in the filename (new product)",
  "name-in-filename": "product name in the filename",
  "code-in-body-not-in-catalogue":
    "product code in the document body (new product)",
};

/** The Dash screen's risk colours (#b32424 / #9a6700 / #1a7f37), as Unity Tag kinds. */
function riskKind(level: string): "error" | "warning" | "success" | "neutral" {
  if (level === "High") return "error";
  if (level === "Med") return "warning";
  if (level === "Low") return "success";
  return "neutral";
}

const COMPARATOR_COLUMNS = [
  "Comparator",
  "Risk",
  "Diffs",
  "Shared site(s)",
  "Distinguished by",
] as const;

/** Statuses that mean the worker is finished with the job, one way or another. */
const DONE = new Set(["complete", "completed", "failed", "cancelled"]);

export type ReportPanelProps = {
  program: string | null;
  presentation: number | string | null;
};

export function ReportPanel({ program, presentation }: ReportPanelProps) {
  const createRun = useCreateRun();
  const generate = useGenerateReport();
  const [runId, setRunId] = useState<string | null>(null);
  const [validation, setValidation] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const status = useRunStatus(runId);
  const finished = DONE.has(status.data?.status ?? "");
  const runResult = useRunResult(runId, finished);

  /** Whichever path produced an answer. Same shape either way, so the rendering below is shared. */
  const result = runResult.data ?? generate.data ?? null;
  const busy =
    createRun.isPending ||
    generate.isPending ||
    (Boolean(runId) && !finished && !status.isError);

  const handleGenerate = () => {
    setDownloadError(null);
    setRunId(null);
    generate.reset();
    // Same two messages as the Dash screen, answered here rather than round-tripping to the server.
    if (!program) {
      setValidation(
        "Select a product above first, then click Generate PSA report."
      );
      return;
    }
    if (presentation === null || presentation === undefined) {
      setValidation(
        "Select a presentation above first, then click Generate PSA report."
      );
      return;
    }
    setValidation(null);

    createRun.mutate(
      { program, source_row: presentation },
      {
        onSuccess: (created) => setRunId(created.id),
        // No run routes here ⇒ standalone. Do the work synchronously instead of reporting a 404 the
        // person cannot act on.
        onError: () => generate.mutate({ program, presentation }),
      }
    );
  };

  const handleDownload = (item: ReportResult) => {
    const runFile = (runResult.data?.files ?? [])[0];
    const failed = (error: unknown) =>
      setDownloadError(describeError(error, "The download failed."));

    if (runId && runFile) {
      // Through the PLATFORM's file endpoint: a run's deliverable belongs to the run, where it stays
      // downloadable and auditable rather than behind a temporary id.
      downloadRunFile(runId, runFile.id, runFile.filename).catch(failed);
      return;
    }
    if (item.download_id) {
      downloadGeneratedFile(
        item.download_id,
        item.filename ?? "psa_report.docx"
      ).catch(failed);
    }
  };

  return (
    <>
      <Button disabled={busy} onClick={handleGenerate} variant="primary">
        Generate PSA report
      </Button>

      {busy ? (
        <PendingNote>
          {status.data?.progress_message ??
            "Generating the report from the live Smartsheet — this takes a few seconds…"}
        </PendingNote>
      ) : null}

      {/* Only shown for a real job: standalone there is nothing to record. */}
      {runId ? (
        <Caption className="block text-muted">
          Recorded as job {runId}
          {status.data?.stage ? ` · step: ${status.data.stage}` : ""}
          {typeof status.data?.progress_pct === "number"
            ? ` · ${status.data.progress_pct}%`
            : ""}
        </Caption>
      ) : null}

      {validation ? (
        <Alert className="my-2" icon="triangle-exclamation" status="info">
          {validation}
        </Alert>
      ) : null}

      {status.data?.status === "failed" ? (
        <Alert className="my-2" status="error">
          The job failed. Its checkpoints are kept, so a retry resumes from the
          last completed step rather than re-reading the Smartsheet.
        </Alert>
      ) : null}

      {generate.isError ? (
        <Alert className="my-2" status="error">
          {describeError(generate.error, "The report run failed.")}
        </Alert>
      ) : null}

      {result ? (
        <ReportResultBlock
          downloadError={downloadError}
          onDownload={handleDownload}
          result={result}
        />
      ) : null}
    </>
  );
}

function ReportResultBlock({
  result,
  onDownload,
  downloadError,
}: {
  result: ReportResult;
  onDownload: (result: ReportResult) => void;
  downloadError: string | null;
}) {
  const log = result.log ?? "";
  const status = result.status;

  const banner = log.includes("LIVE API") ? (
    <Caption className="mt-2 block text-muted">
      Data source: live Smartsheet ✓
    </Caption>
  ) : log.includes("STALE .xlsx") ? (
    <Alert className="my-2" icon="triangle-exclamation" status="info">
      Live Smartsheet is not configured — used the stale .xlsx export. Set the
      SMARTSHEET_ACCESS_TOKEN / SMARTSHEET_SHEET_ID variables to use live data.
    </Alert>
  ) : null;

  // 'message' is a validation answer; 'needs_override' means the product could not be identified.
  if (status === "message" || status === "needs_override") {
    return (
      <>
        {banner}
        <Alert
          className="my-2"
          icon={status === "message" ? "triangle-exclamation" : undefined}
          status={status === "message" ? "info" : "error"}
        >
          {result.message}
        </Alert>
        <RunLog log={log} />
      </>
    );
  }

  const presentationLabel = (result.presentations ?? []).find(
    (entry) => entry.source_row === result.presentation_used
  )?.label;
  const label =
    `${result.program} (${result.identified_name})` +
    (presentationLabel ? `  |  presentation: ${presentationLabel}` : "");

  const missingFile = !result.output_exists;
  const statusText = result.verify_ok
    ? `ALL CHECKS PASSED — generated PSA report for ${label}`
    : `Generated PSA report for ${label} — some checks did not pass (see the run log).`;

  return (
    <>
      {banner}
      <Alert className="my-2" status={result.verify_ok ? "success" : "info"}>
        {statusText}
        {missingFile
          ? "  (the report file was not produced — check the run log.)"
          : ""}
      </Alert>

      {result.reason && BASIS[result.reason] ? (
        <Caption className="block text-muted">
          Matched by: {BASIS[result.reason]}.
        </Caption>
      ) : null}

      {result.alternatives && result.alternatives.length > 0 ? (
        <Caption className="block text-muted">
          Other possible matches:{" "}
          {result.alternatives
            .map((entry) => `${String(entry[1])} (${String(entry[2])})`)
            .join(", ")}
        </Caption>
      ) : null}

      {result.output_exists ? (
        <Button
          className="my-2"
          onClick={() => onDownload(result)}
          startIcon="download"
          variant="secondary"
        >
          Download PSA report (.docx)
        </Button>
      ) : null}

      {downloadError ? (
        <Alert className="my-2" status="error">
          {downloadError}
        </Alert>
      ) : null}

      <RiskBlock risk={result.risk} />
      <RunLog log={log} />
    </>
  );
}

/** The provisional mix-up-risk section. Absent risk ⇒ nothing rendered, as on the Dash screen. */
function RiskBlock({ risk }: { risk?: RiskResult | null }) {
  if (!risk?.overall_risk) return null;

  const informational = risk.informational ?? [];

  return (
    <section className="mt-4 rounded-md bg-02 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">Provisional mix-up risk:</span>
        <Tag kind={riskKind(risk.overall_risk)} variant="filled">
          {risk.overall_risk}
        </Tag>
        <Caption className="text-muted">
          ({risk.n_comparators ?? 0} manufacturing co-located, same-family
          comparators — matches the report)
        </Caption>
      </div>

      {risk.rationale ? <P className="my-2">{risk.rationale}</P> : null}

      <ComparatorTable
        caption="Assessed comparators"
        items={risk.comparators ?? []}
      />

      {informational.length > 0 ? (
        <>
          <Caption className="mt-4 block font-semibold">
            Packaging-site / other-family co-location — informational only (not
            assessed, not in the report):
          </Caption>
          <ComparatorTable
            caption="Informational co-location"
            items={informational}
          />
        </>
      ) : null}

      {risk.note ? (
        <Caption className="mt-2 block text-muted">{risk.note}</Caption>
      ) : null}
    </section>
  );
}

/** Shared by the assessed and informational sets. Capped at 6 rows, as the Dash screen was. */
function ComparatorTable({
  items,
  caption,
}: {
  items: RiskComparator[];
  caption: string;
}) {
  if (items.length === 0) return null;

  return (
    <Table bodyBorders="row" className="text-sm" wrapperClassName="my-2">
      {/* `<caption>` must be the table's first child; hidden because the heading above already says it. */}
      <TableCaption className="sr-only">{caption}</TableCaption>
      <TableHeader>
        <TableRow>
          {COMPARATOR_COLUMNS.map((column) => (
            <TableHead key={column}>{column}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.slice(0, 6).map((item) => (
          <TableRow key={`${item.label}-${item.risk_level}`}>
            <TableCell>{item.label}</TableCell>
            <TableCell>
              <Tag kind={riskKind(item.risk_level)} variant="outlined">
                {item.risk_level}
              </Tag>
            </TableCell>
            <TableCell>{item.n_distinguishing}</TableCell>
            <TableCell>{item.shared_sites.join(", ") || "-"}</TableCell>
            <TableCell>
              {item.distinguishing.join(", ") || "none — looks alike"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function RunLog({ log }: { log: string }) {
  if (!log) return null;
  return (
    <Accordion className="my-4">
      <AccordionItem headerLevel={3} title="Run log">
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap rounded-md bg-02 p-4 text-xs">
          {log}
        </pre>
      </AccordionItem>
    </Accordion>
  );
}
