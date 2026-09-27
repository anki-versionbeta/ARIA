import {
  Accordion,
  AccordionItem,
  Alert,
  Button,
  Caption,
  Field,
  Select,
  TextInput,
} from "@abbvie-unity/react";
import { useState } from "react";
import {
  describeError,
  useAddPaletteColor,
  usePalette,
  useRemovePaletteColor,
} from "../api/queries";
import type { PaletteAddBody } from "../api/types";

/**
 * Add or remove colours in the supplier palette.
 *
 * The palette is a shipped catalogue (`psa/assets/cap_palette.csv`, transcribed from the two supplier
 * PDFs) plus an override object holding the edits. Nothing here writes the shipped file, which is why the
 * banner can always say whether what you are looking at has been edited.
 *
 * There is no "restore the catalogue" action, by request. An override is still fully reversible through
 * ordinary editing — a removed shipped colour can be re-added, an added colour removed — and an
 * unreadable override is replaced by the next successful edit, which is what the warning above says.
 *
 * The override is GLOBAL: an edit changes what every assessor is recommended. The banner says so.
 *
 * The field labels are deliberately the raw CSV column names — the people who maintain the palette work
 * from that file, and renaming the columns in the UI would make the two disagree.
 *
 * The server answers a bad edit with `{ok: false, message}` at HTTP 200 (`cap_colors.add_color`
 * returns a tuple), so a validation failure is a message to render, not an error to throw.
 */

/** The eight free-text columns, in CSV order. `off_the_shelf` is a Select and handled separately. */
const TEXT_COLUMNS = [
  { key: "vendor_color_name", width: "w-44", required: true },
  { key: "vendor_code", width: "w-32", required: false },
  { key: "canonical_color", width: "w-36", required: true },
  { key: "hue_group", width: "w-28", required: false },
  { key: "hex", width: "w-28", required: false },
  { key: "sizes_mm", width: "w-28", required: false },
  { key: "finish", width: "w-28", required: false },
  { key: "component", width: "w-32", required: false },
] as const;

type TextColumnKey = (typeof TEXT_COLUMNS)[number]["key"];

const EMPTY: Record<TextColumnKey, string> = {
  vendor_color_name: "",
  vendor_code: "",
  canonical_color: "",
  hue_group: "",
  hex: "",
  sizes_mm: "",
  finish: "",
  component: "",
};

const OFF_THE_SHELF_OPTIONS = [
  { value: "1", label: "off-the-shelf" },
  { value: "0", label: "custom" },
];

export type PaletteEditorProps = {
  vendor: string;
  /** Called after a successful add/remove so the page can drop a now-stale colour selection. */
  onPaletteChanged: () => void;
};

export function PaletteEditor({
  vendor,
  onPaletteChanged,
}: PaletteEditorProps) {
  const [values, setValues] = useState<Record<TextColumnKey, string>>(EMPTY);
  const [offTheShelf, setOffTheShelf] = useState("1");
  const [removeTarget, setRemoveTarget] = useState("");

  const palette = usePalette(vendor);
  const addColor = useAddPaletteColor();
  const removeColor = useRemovePaletteColor(vendor);

  const setField = (key: TextColumnKey, value: string) =>
    setValues((previous) => ({ ...previous, [key]: value }));

  const handleAdd = () => {
    const body: PaletteAddBody = {
      vendor,
      vendor_color_name: values.vendor_color_name,
      canonical_color: values.canonical_color,
      vendor_code: values.vendor_code,
      hue_group: values.hue_group,
      hex: values.hex,
      // Same defaults the Dash screen applied on submit, kept here so the request is explicit.
      sizes_mm: values.sizes_mm || "13;20",
      finish: values.finish,
      off_the_shelf: offTheShelf === "1",
      component: values.component || "pp_disc",
    };
    addColor.mutate(body, {
      onSuccess: (result) => {
        if (!result.ok) return;
        // Clear on success. Leaving the values in place invited a second click, and because an add is
        // keyed on (vendor, vendor_color_name) that silently rewrote the row the user had just added —
        // or, after switching manufacturer, added the same colour to the other vendor.
        setValues(EMPTY);
        setOffTheShelf("1");
        onPaletteChanged();
      },
    });
  };

  const handleRemove = () => {
    if (!removeTarget) return;
    removeColor.mutate(removeTarget, {
      onSuccess: (result) => {
        if (result.ok) {
          setRemoveTarget("");
          onPaletteChanged();
        }
      },
    });
  };

  // One status line for both mutations — whichever ran last.
  const statusOf = (mutation: {
    isError: boolean;
    error: unknown;
    data?: { ok: boolean; message: string };
  }) =>
    mutation.isError
      ? {
          ok: false,
          message: describeError(mutation.error, "The palette edit failed."),
        }
      : (mutation.data ?? null);

  const status = [
    { at: addColor.submittedAt, value: statusOf(addColor) },
    { at: removeColor.submittedAt, value: statusOf(removeColor) },
  ].reduce((latest, candidate) =>
    candidate.at > latest.at ? candidate : latest
  ).value;

  const overridden = palette.data?.is_override ?? false;
  const overrideInvalid = palette.data?.override_invalid ?? false;

  return (
    <Accordion className="my-4">
      <AccordionItem
        headerLevel={3}
        title="Add / remove a cap colour (palette)"
      >
        {/*
          `info` + the triangle rather than a warning status: Unity's Alert has none, and this is not an
          error anyway — nothing is blocked, the shipped catalogue simply took over.
        */}
        {overrideInvalid ? (
          <Alert className="mb-2" icon="triangle-exclamation" status="info">
            A stored palette edit could not be read, so the shipped catalogue is
            in force. Recommendations are unaffected — the next add or remove
            below replaces the unreadable edit.
          </Alert>
        ) : null}
        {overridden ? (
          <Alert className="mb-2" icon="pen-to-square" status="info">
            This palette has local edits: {palette.data?.added ?? 0} colour(s)
            added or changed, {palette.data?.removed ?? 0} hidden. The edits are
            shared — everyone sees this palette, not just you.
          </Alert>
        ) : null}

        <Caption className="block text-muted">
          Edits the supplier palette for the seal manufacturer selected above.
          This only changes the list of colours the tool recommends from — it
          never changes any product&apos;s assigned cap, and never changes the
          shipped catalogue.
        </Caption>
        <Caption className="mt-2 block text-muted">
          Vendor = <strong>{vendor}</strong>, the seal manufacturer selected
          above. Fill the columns (labels match <code>cap_palette.csv</code>),
          then Add colour — vendor_color_name + canonical_color are required.
        </Caption>
        {/*
          Both facts below caused "I added a colour and nothing happened" reports. The grid above shows
          only off-the-shelf pp_disc rows, and the recommendation engine ranks by ΔE, which needs a hex.
        */}
        <Caption className="mt-2 block text-muted">
          The grid above lists off-the-shelf <code>pp_disc</code> colours only,
          so a colour added as <em>custom</em> — or with another{" "}
          <code>component</code> — will not appear there. Leave <code>hex</code>{" "}
          blank and the colour shows as a hatched tile and cannot be ranked by
          ΔE.
        </Caption>

        <div className="mt-4 flex flex-wrap items-start gap-2">
          {TEXT_COLUMNS.map((column) => (
            <Field
              floatingLabel
              key={column.key}
              label={column.key}
              required={column.required}
            >
              <TextInput
                className={column.width}
                onChange={(event) => setField(column.key, event.target.value)}
                type="text"
                value={values[column.key]}
              />
            </Field>
          ))}

          <Field floatingLabel label="off_the_shelf">
            <Select
              className="w-36"
              onChange={(value: string) => setOffTheShelf(value)}
              options={OFF_THE_SHELF_OPTIONS}
              value={offTheShelf}
            />
          </Field>

          <Button
            disabled={addColor.isPending}
            onClick={handleAdd}
            variant="secondary"
          >
            {addColor.isPending ? "Adding…" : "Add colour"}
          </Button>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Field floatingLabel label="Remove a colour">
            <Select
              className="w-60"
              clearable
              onChange={(value: string) => setRemoveTarget(value)}
              options={(palette.data?.colors ?? []).map(
                (color) => color.vendor_color_name
              )}
              placeholder="— pick a colour —"
              searchable
              value={removeTarget}
            />
          </Field>
          <Button
            disabled={!removeTarget || removeColor.isPending}
            onClick={handleRemove}
            variant="secondary"
          >
            {removeColor.isPending ? "Removing…" : "Remove selected"}
          </Button>
        </div>

        {status ? (
          <Alert className="mt-4" status={status.ok ? "success" : "error"}>
            {status.message}
          </Alert>
        ) : null}
      </AccordionItem>
    </Accordion>
  );
}
