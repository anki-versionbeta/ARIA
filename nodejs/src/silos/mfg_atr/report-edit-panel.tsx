import {
  Alert,
  Button,
  Card,
  CardBody,
  H2,
  P,
  Spinner,
} from "@abbvie-unity/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useRef } from "react";
import { ApiError, api } from "@/api/http";
import { isForbidden, queryKeys } from "@/api/queries";
import { getRuntimeConfig } from "@/config/runtime";
import type { PdfOverlayHandle } from "./pdf-overlay-editor";
import { type ReviewFlag, ReviewFlags } from "./review-flags";

/**
 * Loaded on demand. The silo registry is globbed eagerly, so a static import would put
 * pdfjs-dist — around 450 kB — in the chunk every page of the app waits for, to serve one
 * screen of one silo.
 */
const PdfOverlayEditor = lazy(() =>
  import("./pdf-overlay-editor").then((module) => ({
    default: module.PdfOverlayEditor,
  }))
);

/**
 * The run parks after generating, so the author can fill in the judgement fields that no
 * query can answer — regulatory report, HQC specification id, the assessment and remarks.
 * Submitting them completes `await_edits`, and `finalize` re-bakes the report with those
 * values and locks the archival copy.
 *
 * The app being replaced offered "download with your changes", which re-baked on every
 * download and kept the report in a TTL cache in the meantime. Here the re-bake is the
 * finalize stage: the edited PDF and DOCX arrive as run outputs, so they are downloadable
 * from the Files table for as long as the document exists.
 */
type ReviewPayload = {
  report_type: string | null;
  display: string | null;
  generated: boolean;
  flags: ReviewFlag[];
  preview_file_id: string | null;
};

function fetchReview(documentId: string) {
  return api.get<ReviewPayload>(
    `/silos/mfg_atr/documents/${documentId}/review`
  );
}

function submitFieldValues(
  documentId: string,
  fieldValues: Record<string, string>
) {
  return api.post<{ id: string; status: string }>(
    `/silos/mfg_atr/documents/${documentId}/field-values`,
    { field_values: fieldValues }
  );
}

function submitErrorMessage(error: unknown): string {
  if (isForbidden(error)) {
    return "You do not own this document, so your values cannot be applied. Make your own copy first.";
  }
  // A 409 means the run moved on — another tab may have finalized it already. The detail
  // says which stage it is at, which is what the user needs to know.
  if (error instanceof ApiError && error.status === 409) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === "string") return body.detail;
  }
  return "Could not apply your values.";
}

/**
 * Which fields the author is being asked for. Both report types park at the same stage and
 * one overlay serves both, but they are different documents with different manual fields,
 * so naming ATR's on an MFGR run would simply be wrong.
 */
const FIELDS: Record<string, string> = {
  atr: "Regulatory report, HQC Specification ID, the assessment and remarks",
  mfgr: "clinical phase, DS quality and site, storage temperature, fill volume and the closing conclusion",
};

type Props = { documentId: string; canEdit: boolean };

export function ReportEditPanel({ documentId, canEdit }: Props) {
  const queryClient = useQueryClient();
  const overlayRef = useRef<PdfOverlayHandle>(null);

  const { data, isPending, isError } = useQuery({
    queryKey: ["silos", "mfg_atr", "review", documentId],
    queryFn: () => fetchReview(documentId),
    retry: false,
    // A completed checkpoint plus an attached file; neither changes while the run is parked.
    staleTime: Number.POSITIVE_INFINITY,
  });

  const submit = useMutation({
    mutationFn: () =>
      submitFieldValues(documentId, overlayRef.current?.getFieldValues() ?? {}),
    // The run goes back to queued, so the document query must resume polling.
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.document(documentId),
      }),
  });

  if (isPending) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-9" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <Alert
        status="error"
        subtitle="Retrying the document will regenerate the report."
      >
        Could not load the report for review
      </Alert>
    );
  }

  const previewUrl = data.preview_file_id
    ? `${getRuntimeConfig().apiBaseUrl}/documents/${documentId}/files/${data.preview_file_id}`
    : null;

  return (
    <Card>
      <CardBody className="flex flex-col gap-4">
        <H2 styledAs="h4">
          Review and complete{data.display ? ` — ${data.display}` : ""}
        </H2>
        <P className="text-muted">
          Click any field in the report below
          {data.report_type && FIELDS[data.report_type]
            ? ` — ${FIELDS[data.report_type]} — `
            : " "}
          and edit it directly. Applying your values rebuilds the report and
          locks the archival copy, so review the flags first.
        </P>

        <ReviewFlags flags={data.flags} />

        {submit.isError ? (
          <Alert status="error">{submitErrorMessage(submit.error)}</Alert>
        ) : null}

        {previewUrl ? (
          <Suspense
            fallback={
              <div className="flex justify-center py-12">
                <Spinner className="h-9" />
              </div>
            }
          >
            <PdfOverlayEditor ref={overlayRef} src={previewUrl} />
          </Suspense>
        ) : (
          <Alert
            status="info"
            subtitle="Applying empty values will still finalize the report."
          >
            The generated PDF is not attached to this document, so there is
            nothing to edit
          </Alert>
        )}

        <div>
          <Button
            disabled={!canEdit || submit.isPending}
            onClick={() => submit.mutate()}
          >
            {submit.isPending ? "Applying…" : "Apply values and finalize"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
