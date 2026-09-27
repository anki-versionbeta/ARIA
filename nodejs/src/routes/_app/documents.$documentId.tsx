import {
  Alert,
  Button,
  Caption,
  Card,
  CardBody,
  H1,
  H2,
  Link,
  P,
  Progress,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import {
  isForbidden,
  isNotFound,
  useBuildDocument,
  useDocument,
  useForkDocument,
  useRetryDocument,
} from "@/api/queries";
import type { DocumentDetail } from "@/api/types";
import { RouterLink } from "@/components/router-link/router-link";
import { SectionEditor } from "@/components/section-editor";
import { StatusBadge } from "@/components/status-badge";
import { getRuntimeConfig } from "@/config/runtime";
import { getSiloModule } from "@/silos/registry";
import { formatBytes } from "@/utils/format";

export const Route = createFileRoute("/_app/documents/$documentId")({
  component: DocumentPage,
});

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function DocumentPage() {
  const { documentId } = Route.useParams();
  // Everything renders from the URL, so a reload rebuilds the page. The apps this
  // replaces held the job id in a JavaScript variable and lost it on refresh.
  const { data, isPending, isError, error } = useDocument(documentId);

  if (isPending) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-9" />
      </div>
    );
  }

  if (isError) {
    return (
      <Alert status="error">
        {isNotFound(error)
          ? "This document does not exist."
          : "Could not load this document."}
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <RouterLink to="/history">← All documents</RouterLink>

      {data.can_edit ? null : <ReadOnlyBanner document={data} />}

      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-4">
          <H1 styledAs="h2">{data.title}</H1>
          <StatusBadge status={data.status} />
        </div>
        <Caption className="text-muted">
          {data.silo_id.toUpperCase()} · {data.owner.display_name} ·{" "}
          {dateFormatter.format(new Date(data.started_at))}
        </Caption>
      </div>

      {data.status === "queued" ||
      data.status === "running" ||
      data.status === "awaiting_user" ? (
        <ProgressPanel document={data} />
      ) : null}

      {data.status === "failed" && data.error_message ? (
        <Alert status="error" subtitle={data.error_message}>
          This document failed to generate
        </Alert>
      ) : null}

      {data.forked_from_run_id ? (
        <P className="text-muted">
          Copied from{" "}
          <RouterLink
            to="/documents/$documentId"
            params={{ documentId: data.forked_from_run_id }}
          >
            the original document
          </RouterLink>
          .
        </P>
      ) : null}

      {data.status === "awaiting_user" ? (
        <AwaitingUserPanel document={data} />
      ) : null}

      {data.status === "failed" ? <RetryPanel documentId={data.id} /> : null}

      {data.status === "complete" ||
      (data.status === "awaiting_user" && !siloOwnsReview(data.silo_id)) ? (
        <SectionEditor documentId={data.id} canEdit={data.can_edit} />
      ) : null}

      <Card>
        <CardBody className="flex flex-col gap-4">
          <H2 styledAs="h4">Files</H2>
          {data.files.length === 0 ? (
            <P className="text-muted">
              No files are attached to this document yet.
            </P>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Size</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.files.map((file) => (
                  <TableRow key={file.id}>
                    <TableCell>
                      {/* A real browser download, so it must be an anchor rather
                          than a click handler. */}
                      <Link
                        render={
                          <a
                            href={`${getRuntimeConfig().apiBaseUrl}/documents/${data.id}/files/${file.id}`}
                            download={file.filename}
                          />
                        }
                      >
                        {file.filename}
                      </Link>
                    </TableCell>
                    <TableCell>
                      {file.kind === "input" ? "Input" : "Output"}
                    </TableCell>
                    <TableCell>{formatBytes(file.size_bytes)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function siloOwnsReview(siloId: string): boolean {
  return Boolean(getSiloModule(siloId)?.screens?.review);
}

/**
 * A silo may own the awaiting-user screen, and then it *replaces* the generic panel
 * rather than sitting beside it. ISO parks to ask which clauses to assess: pressing the
 * generic "Build document" would complete that stage with an empty checkpoint and the run
 * would resume with no range at all.
 *
 * The shared section editor is suppressed in that case too — a silo parked before it has
 * generated anything has nothing to edit, so the empty state would just be noise.
 */
function AwaitingUserPanel({ document }: { document: DocumentDetail }) {
  const Review = getSiloModule(document.silo_id)?.screens?.review;
  if (Review) {
    return <Review documentId={document.id} canEdit={document.can_edit} />;
  }
  return <ReviewPanel document={document} />;
}

/**
 * The run is parked waiting for a person. Building resumes it: the worker picks it up
 * again and continues at the next stage.
 */
function ReviewPanel({ document }: { document: DocumentDetail }) {
  const build = useBuildDocument(document.id);

  return (
    <Card>
      <CardBody className="flex flex-col gap-3">
        <H2 styledAs="h4">Ready for your review</H2>
        <P className="text-muted">
          Edit any section below, then build the final document. Your saved
          edits are used; untouched sections keep what was generated.
        </P>
        {build.isError ? (
          <Alert status="error">
            {isForbidden(build.error)
              ? "You do not own this document, so it cannot be built. Make your own copy first."
              : "Could not start the build."}
          </Alert>
        ) : null}
        <div>
          <Button
            disabled={!document.can_edit || build.isPending}
            onClick={() => build.mutate(undefined)}
          >
            {build.isPending ? "Starting…" : "Build document"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

function RetryPanel({ documentId }: { documentId: string }) {
  const retry = useRetryDocument(documentId);
  return (
    <Card>
      <CardBody className="flex flex-col gap-3">
        <P className="text-muted">
          Retrying resumes from the last completed stage rather than starting
          over.
        </P>
        <div>
          <Button disabled={retry.isPending} onClick={() => retry.mutate()}>
            {retry.isPending ? "Retrying…" : "Retry"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

/**
 * Live run state. The query polls every 2s while work is outstanding and stops on a
 * terminal status, so a finished document costs nothing to display.
 */
function ProgressPanel({ document }: { document: DocumentDetail }) {
  const stage = document.stage ? ` · ${document.stage}` : "";
  return (
    <Card>
      <CardBody className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <H2 styledAs="h5">
            {document.status === "awaiting_user"
              ? "Waiting for you"
              : "In progress"}
          </H2>
          <Caption className="text-muted">
            {document.status}
            {stage}
          </Caption>
        </div>
        <Progress value={document.progress_pct} />
        {document.progress_message ? (
          <P className="text-muted">{document.progress_message}</P>
        ) : null}
      </CardBody>
    </Card>
  );
}

/**
 * D14/D15: a non-owner sees the document read-only and forks their own copy
 * rather than editing someone else's. The original is left untouched.
 */
function ReadOnlyBanner({ document }: { document: DocumentDetail }) {
  const navigate = useNavigate();
  const fork = useForkDocument();

  return (
    <Alert
      status="info"
      subtitle={`${document.owner.display_name} owns this document. Make your own copy to change it.`}
      action={
        <Button
          disabled={fork.isPending}
          onClick={() =>
            fork.mutate(document.id, {
              onSuccess: ({ id }) =>
                navigate({
                  to: "/documents/$documentId",
                  params: { documentId: id },
                }),
            })
          }
        >
          {fork.isPending ? "Copying…" : "Create my own copy"}
        </Button>
      }
    >
      Read-only
    </Alert>
  );
}
