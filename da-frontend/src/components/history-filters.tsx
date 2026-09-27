import { Checkbox, Field, Icon, Select, TextInput } from "@abbvie-unity/react";
import { useEffect, useState } from "react";
import { useSilos } from "@/api/queries";
import { RUN_STATUSES, type RunStatus } from "@/api/types";

const STATUS_LABELS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  awaiting_user: "Needs review",
  complete: "Complete",
  failed: "Failed",
};

export type FilterValues = {
  q: string;
  silo: string;
  status: string;
  mine: boolean;
};

type Props = {
  values: FilterValues;
  onChange: (patch: Partial<FilterValues>) => void;
};

export function HistoryFilters({ values, onChange }: Props) {
  const { data: silos } = useSilos();
  const siloOptions = (silos ?? []).map((silo) => ({
    value: silo.id,
    label: silo.label,
  }));

  // Local mirror so typing stays responsive; the URL and query update on a pause
  // rather than on every keystroke.
  const [searchText, setSearchText] = useState(values.q);

  useEffect(() => {
    setSearchText(values.q);
  }, [values.q]);

  useEffect(() => {
    if (searchText === values.q) return;
    const timer = setTimeout(() => onChange({ q: searchText }), 300);
    return () => clearTimeout(timer);
  }, [searchText, values.q, onChange]);

  return (
    <div className="flex medium:flex-row flex-col medium:items-end gap-4">
      <Field aria-label="Search documents by title" block>
        <TextInput
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          placeholder="Search by title"
          start={<Icon icon="magnifying-glass" />}
          className="medium:min-w-64 min-w-0"
        />
      </Field>

      <Field label="Status" floatingLabel block>
        <Select
          options={RUN_STATUSES.map((status) => ({
            value: status,
            label: STATUS_LABELS[status],
          }))}
          value={values.status}
          onChange={(value) => onChange({ status: String(value) })}
          placeholder="Any status"
          clearable
          className="medium:min-w-48 min-w-0"
        />
      </Field>

      {siloOptions.length > 0 ? (
        <Field label="Type" floatingLabel block>
          <Select
            options={siloOptions}
            value={values.silo}
            onChange={(value) => onChange({ silo: String(value) })}
            placeholder="Any type"
            clearable
            className="medium:min-w-48 min-w-0"
          />
        </Field>
      ) : null}

      <Checkbox
        label="Only mine"
        checked={values.mine}
        onChange={(event) => onChange({ mine: event.target.checked })}
      />
    </div>
  );
}
