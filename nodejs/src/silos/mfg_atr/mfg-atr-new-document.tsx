import {
  Alert,
  Button,
  Card,
  CardBody,
  Combobox,
  ComboboxItem,
  Field,
  H1,
  H2,
  Icon,
  P,
  Spinner,
  TextInput,
} from "@abbvie-unity/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError, api } from "@/api/http";

/**
 * This silo takes an identifier, not a file, so it replaces the shared upload screen.
 *
 * Ported from the two entry pages of the app being replaced. The difference is what the
 * button does: there it ran the whole pipeline inside the request and rendered the result
 * on the same page, so a slow warehouse held the browser open. Here it queues a run and
 * navigates to the document, which is why both report types can be in flight at once.
 */
type ReportType = "atr" | "mfgr";
type Source = "live" | "fixture";

const DEFAULT_FIXTURE = "tests/fixtures/cmc10352.json";

const COPY: Record<
  ReportType,
  {
    label: string;
    idLabel: string;
    placeholder: string;
    hint: React.ReactNode;
    pickerTitle: string;
    pickerHint: string;
  }
> = {
  atr: {
    label: "Analytical Report (ATR)",
    idLabel: "CMC Request ID",
    placeholder: "e.g. CMC-10352",
    hint: (
      <>
        Type a request id in any form — <code>10352</code>,{" "}
        <code>CMC-10352</code>, or <code>PEGA-PROD-CMC-10352</code>.
      </>
    ),
    pickerTitle: "Select from existing request",
    pickerHint:
      "Populated live from the CMC DWH (distinct request ids with results).",
  },
  mfgr: {
    label: "Manufacturing Report (MFGR)",
    idLabel: "Batch ID",
    placeholder: "e.g. BAX000584",
    hint: (
      <>
        Type a batch id in any form — <code>BAX000584</code> or{" "}
        <code>nest-br-prod-BAX000584</code>.
      </>
    ),
    pickerTitle: "Select from existing batch",
    pickerHint: "Populated live from the warehouse (batches with results).",
  },
};

type IdList = { request_ids?: string[]; batch_ids?: string[] };

function fetchIds(reportType: ReportType) {
  const path = reportType === "atr" ? "/request-ids" : "/batch-ids";
  return api.get<IdList>(`/silos/mfg_atr${path}`);
}

function createRun(body: {
  report_type: ReportType;
  identifier: string;
  source: Source;
  fixture_path?: string;
}) {
  return api.post<{ id: string }>("/silos/mfg_atr/runs", body);
}

function createErrorMessage(error: unknown): string {
  // The identifier is validated server-side and answered with 400 plus the source's own
  // wording; showing it beats a generic message because the user can fix the input.
  if (error instanceof ApiError && error.status === 400) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === "string") return body.detail;
  }
  // Anything else is a fault rather than bad input, so name the status. A bare "could not
  // start" once hid a 422 from a shadowed route, which cost real debugging time.
  if (error instanceof ApiError) {
    return `Could not start the report (HTTP ${error.status}).`;
  }
  return "Could not start the report.";
}

export function MfgAtrNewDocument() {
  const navigate = useNavigate();
  const [reportType, setReportType] = useState<ReportType>("atr");
  const [source, setSource] = useState<Source>("live");
  const [fixturePath, setFixturePath] = useState(DEFAULT_FIXTURE);
  const [typed, setTyped] = useState("");
  const [picked, setPicked] = useState("");
  const [browseOpen, setBrowseOpen] = useState(false);
  const [validation, setValidation] = useState<string | null>(null);

  const copy = COPY[reportType];

  const ids = useQuery({
    queryKey: ["silos", "mfg_atr", "ids", reportType],
    queryFn: () => fetchIds(reportType),
    enabled: browseOpen,
    retry: false,
    // The backend already caches these for an hour; no need to re-ask on every mount.
    staleTime: 5 * 60 * 1000,
  });

  const create = useMutation({
    mutationFn: (identifier: string) =>
      createRun({
        report_type: reportType,
        identifier,
        source,
        fixture_path: source === "fixture" ? fixturePath : undefined,
      }),
    onSuccess: ({ id }) =>
      navigate({ to: "/documents/$documentId", params: { documentId: id } }),
  });

  function start(identifier: string) {
    const trimmed = identifier.trim();
    if (!trimmed) {
      setValidation(`Please enter a ${copy.idLabel} to continue.`);
      return;
    }
    setValidation(null);
    create.mutate(trimmed);
  }

  function switchReportType(next: ReportType) {
    setReportType(next);
    // The id forms differ, and so do the pickers — carrying either across would offer an
    // id that cannot validate.
    setTyped("");
    setPicked("");
    setBrowseOpen(false);
    setValidation(null);
    create.reset();
    if (next === "mfgr") setSource("live");
  }

  const available = ids.data?.request_ids ?? ids.data?.batch_ids ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <H1 styledAs="h2">New {copy.label}</H1>
        <P className="text-muted">
          Generation runs in the background, so you can leave this page and come
          back to it. You will be asked to complete the judgement fields once
          the report is ready.
        </P>
      </div>

      <div className="flex items-center gap-3">
        <span className="text-body text-muted">Report:</span>
        <Button
          size="small"
          active={reportType === "atr"}
          activeVariant="primary"
          variant="secondary"
          onClick={() => switchReportType("atr")}
        >
          Analytical (ATR)
        </Button>
        <Button
          size="small"
          active={reportType === "mfgr"}
          activeVariant="primary"
          variant="secondary"
          onClick={() => switchReportType("mfgr")}
        >
          Manufacturing (MFGR)
        </Button>
      </div>

      {reportType === "atr" ? (
        <div className="flex items-center gap-3">
          <span className="text-body text-muted">Data source:</span>
          <Button
            size="small"
            active={source === "live"}
            activeVariant="primary"
            variant="secondary"
            onClick={() => setSource("live")}
          >
            Live CMC DWH
          </Button>
          <Button
            size="small"
            active={source === "fixture"}
            activeVariant="primary"
            variant="secondary"
            onClick={() => setSource("fixture")}
          >
            Fixture (offline)
          </Button>
        </div>
      ) : null}

      {source === "fixture" && reportType === "atr" ? (
        <Field label="Fixture JSON" floatingLabel block>
          <TextInput
            value={fixturePath}
            onChange={(e) => setFixturePath(e.target.value)}
            className="min-w-0"
          />
        </Field>
      ) : null}

      {validation ? <Alert status="error">{validation}</Alert> : null}
      {create.isError ? (
        <Alert status="error">{createErrorMessage(create.error)}</Alert>
      ) : null}

      <Card>
        <CardBody className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Icon icon={["fas", "plus"]} className="text-primary" />
            <H2 styledAs="h4">Enter {copy.idLabel}</H2>
          </div>
          <P className="text-muted">{copy.hint}</P>
          <div className="flex items-end gap-3">
            <Field label={copy.idLabel} floatingLabel block className="flex-1">
              <TextInput
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={copy.placeholder}
                className="min-w-0"
                onKeyDown={(e) => e.key === "Enter" && start(typed)}
              />
            </Field>
            <Button
              variant="primary"
              disabled={create.isPending}
              endIcon={["fas", "arrow-right"]}
              onClick={() => start(typed)}
            >
              {create.isPending
                ? "Starting…"
                : `Generate ${reportType.toUpperCase()}`}
            </Button>
          </div>
        </CardBody>
      </Card>

      {source === "live" ? (
        <>
          <div className="text-center text-body text-muted">OR</div>
          <Card>
            <CardBody className="flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <Icon icon={["fas", "list"]} className="text-primary" />
                <H2 styledAs="h4">{copy.pickerTitle}</H2>
              </div>
              <P className="text-muted">{copy.pickerHint}</P>
              {!browseOpen ? (
                <Button variant="secondary" onClick={() => setBrowseOpen(true)}>
                  Browse existing IDs
                </Button>
              ) : ids.isPending ? (
                <div className="flex items-center gap-2 text-muted">
                  <Spinner className="h-5" /> Loading ids…
                </div>
              ) : ids.isError || available.length === 0 ? (
                <Alert
                  status="info"
                  subtitle="Type an id in the tile above instead."
                >
                  The picker is unavailable — the warehouse is not reachable
                </Alert>
              ) : (
                <div className="flex items-end gap-3">
                  <Field
                    label={`Existing ${reportType === "atr" ? "request" : "batch"}`}
                    floatingLabel
                    block
                    className="flex-1"
                  >
                    <Combobox
                      placeholder="Select one"
                      value={picked}
                      onChange={(v) => setPicked(String(v))}
                    >
                      {available.map((id) => (
                        <ComboboxItem key={id} value={id} />
                      ))}
                    </Combobox>
                  </Field>
                  <Button
                    variant="primary"
                    disabled={create.isPending}
                    endIcon={["fas", "arrow-right"]}
                    onClick={() =>
                      picked
                        ? start(picked)
                        : setValidation("Pick one from the dropdown first.")
                    }
                  >
                    Load
                  </Button>
                </div>
              )}
            </CardBody>
          </Card>
        </>
      ) : null}
    </div>
  );
}
