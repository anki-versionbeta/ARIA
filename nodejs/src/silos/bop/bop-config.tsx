import {
  Alert,
  Button,
  Caption,
  Card,
  CardBody,
  Field,
  H1,
  H2,
  P,
  Spinner,
  Tag,
  TextArea,
} from "@abbvie-unity/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api/http";

type Prompts = {
  petra: string;
  reviewer: string;
  petra_is_override: boolean;
  reviewer_is_override: boolean;
};

const KEY = ["silos", "bop", "prompts"];

function fetchPrompts() {
  return api.get<Prompts>("/silos/bop/config/prompts");
}

export function BopConfig() {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: KEY,
    queryFn: fetchPrompts,
  });

  const save = useMutation({
    mutationFn: ({
      which,
      prompt,
    }: {
      which: "petra" | "reviewer";
      prompt: string;
    }) => api.post(`/silos/bop/config/prompts/${which}`, { prompt }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });

  const reset = useMutation({
    mutationFn: (which: "petra" | "reviewer") =>
      api.post(`/silos/bop/config/prompts/${which}/reset`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
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
        subtitle="The backend may need restarting to mount the BOP routes."
      >
        Could not load the BOP prompts
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <H1 styledAs="h2">BOP prompt configuration</H1>
        <P className="text-muted">
          These prompts drive every generated section.
        </P>
        <Alert status="info">
          Saving applies to everyone, not just you — the next document any user
          generates will use it.
        </Alert>
      </div>

      <PromptCard
        title="PETRA generator"
        description="Included in all 14 section and phase generation calls."
        which="petra"
        value={data.petra}
        isOverride={data.petra_is_override}
        saving={save.isPending}
        resetting={reset.isPending}
        onSave={(prompt) => save.mutate({ which: "petra", prompt })}
        onReset={() => reset.mutate("petra")}
      />

      <PromptCard
        title="Reviewer"
        description="Critiques the generated draft and flags points for review."
        which="reviewer"
        value={data.reviewer}
        isOverride={data.reviewer_is_override}
        saving={save.isPending}
        resetting={reset.isPending}
        onSave={(prompt) => save.mutate({ which: "reviewer", prompt })}
        onReset={() => reset.mutate("reviewer")}
      />
    </div>
  );
}

type CardProps = {
  title: string;
  description: string;
  which: "petra" | "reviewer";
  value: string;
  isOverride: boolean;
  saving: boolean;
  resetting: boolean;
  onSave: (prompt: string) => void;
  onReset: () => void;
};

function PromptCard({
  title,
  description,
  value,
  isOverride,
  saving,
  resetting,
  onSave,
  onReset,
}: CardProps) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const dirty = draft !== value;

  return (
    <Card>
      <CardBody className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-3">
          <H2 styledAs="h4">{title}</H2>
          <Tag kind={isOverride ? "warning" : "neutral"}>
            {isOverride ? "Customised" : "Default"}
          </Tag>
          <Caption className="ml-auto text-muted">
            {draft.length} characters
          </Caption>
        </div>
        <P className="text-muted">{description}</P>

        <Field aria-label={`${title} prompt`} block>
          <TextArea
            value={draft}
            rows={14}
            onChange={(event) => setDraft(event.target.value)}
            className="min-w-0 font-mono"
          />
        </Field>

        <div className="flex flex-wrap gap-3">
          <Button disabled={!dirty || saving} onClick={() => onSave(draft)}>
            {saving ? "Saving…" : "Save"}
          </Button>
          <Button
            variant="secondary"
            disabled={!isOverride || resetting}
            onClick={onReset}
          >
            {resetting ? "Resetting…" : "Reset to default"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
