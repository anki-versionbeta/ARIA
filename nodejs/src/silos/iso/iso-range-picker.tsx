import {
  Alert,
  Button,
  Caption,
  Card,
  CardBody,
  Field,
  H2,
  P,
  Select,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@abbvie-unity/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "@/api/http";
import { isForbidden, queryKeys } from "@/api/queries";

/**
 * ISO parks in the middle of its run, not at the end: the outline is known but which
 * clauses to assess is a human decision. So `await_range` is a real pause, and this
 * screen is what completes it.
 *
 * Only the array index is sent, so this depends on nothing in a TOC entry except its
 * title.
 */
type TocEntry = { level?: number; title: string; page?: number };
type Outline = { doc_title: string; total_pages: number; toc: TocEntry[] };

function fetchToc(documentId: string) {
  return api.get<Outline>(`/silos/iso/documents/${documentId}/toc`);
}

function submitRange(documentId: string, start_idx: number, end_idx: number) {
  return api.post<{ id: string; status: string }>(
    `/silos/iso/documents/${documentId}/toc-range`,
    { start_idx, end_idx }
  );
}

function rangeErrorMessage(error: unknown): string {
  if (isForbidden(error)) {
    return "You do not own this document, so the range cannot be set. Make your own copy first.";
  }
  // The endpoint answers a bad range with 400 and a readable detail; showing it beats a
  // generic message, because the user can act on it.
  if (error instanceof ApiError && error.status === 400) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === "string") return body.detail;
  }
  return "Could not start the build.";
}

type Props = { documentId: string; canEdit: boolean };

export function IsoRangePicker({ documentId, canEdit }: Props) {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["silos", "iso", "toc", documentId],
    queryFn: () => fetchToc(documentId),
    retry: false,
    // The outline is a completed checkpoint; it cannot change while the run is parked.
    staleTime: Number.POSITIVE_INFINITY,
  });

  const [start, setStart] = useState(0);
  const [end, setEnd] = useState<number | null>(null);

  const entries = data?.toc ?? [];
  // Defaults to the whole document. Computed here rather than after the early returns
  // below so the mutation and the preview table cannot disagree about what "last
  // section" means — sending a different range from the one on screen is the worst
  // possible failure for this screen.
  const effectiveEnd = end ?? Math.max(entries.length - 1, 0);

  const submit = useMutation({
    mutationFn: () => submitRange(documentId, start, effectiveEnd),
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
        subtitle="Retrying the document may rebuild the outline."
      >
        Could not load the table of contents
      </Alert>
    );
  }

  // Object options rather than plain strings: ISO titles repeat ("General" appears under
  // several annexes) and a string list would collide on value.
  const options = entries.map((entry, index) => ({
    value: String(index),
    label: `${index + 1}. ${entry.title}`,
  }));

  const valid = entries.length > 0 && effectiveEnd >= start;
  // The TOC index is the entry's real identity — it is what gets submitted, and titles
  // repeat across annexes — so carry it as data rather than keying on list position.
  const selected = valid
    ? entries
        .slice(start, effectiveEnd + 1)
        .map((entry, offset) => ({ index: start + offset, title: entry.title }))
    : [];

  return (
    <Card>
      <CardBody className="flex flex-col gap-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <H2 styledAs="h4">Choose the clauses to assess</H2>
          <Caption className="text-muted">
            {entries.length} sections · {data.total_pages} pages
          </Caption>
        </div>
        <P className="text-muted">
          Every clause from the first to the last selected section becomes a row
          in the assessment table. A wider range takes longer and costs more.
        </P>

        {submit.isError ? (
          <Alert status="error">{rangeErrorMessage(submit.error)}</Alert>
        ) : null}

        <div className="flex flex-wrap gap-4">
          <Field label="First section" block>
            <Select
              aria-label="First section"
              options={options}
              value={String(start)}
              readOnly={!canEdit}
              searchable
              menuWidthGrow
              onChange={(value) => setStart(Number(value))}
            />
          </Field>
          <Field
            label="Last section"
            block
            validateStatus={valid ? "normal" : "error"}
            feedback={
              valid
                ? undefined
                : "The last section must not come before the first."
            }
          >
            <Select
              aria-label="Last section"
              options={options}
              value={String(effectiveEnd)}
              readOnly={!canEdit}
              searchable
              menuWidthGrow
              onChange={(value) => setEnd(Number(value))}
            />
          </Field>
        </div>

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Section</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {selected.map((entry) => (
              <TableRow key={entry.index}>
                <TableCell>{entry.title}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <div>
          <Button
            disabled={!canEdit || !valid || submit.isPending}
            onClick={() => submit.mutate()}
          >
            {submit.isPending ? "Starting…" : "Generate the assessment"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
