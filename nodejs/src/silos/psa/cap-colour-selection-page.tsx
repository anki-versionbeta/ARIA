import {
  Alert,
  Button,
  Caption,
  Field,
  H1,
  H2,
  P,
  Select,
  Spinner,
} from "@abbvie-unity/react";
import { useState } from "react";
import { downloadGeneratedFile } from "./api/client";
import {
  describeError,
  useCapExport,
  usePresentations,
  usePrograms,
  useRecommend,
  useRefresh,
} from "./api/queries";
import { VENDORS, type Vendor } from "./api/types";
import { PaletteEditor } from "./components/palette-editor";
import { PaletteGrid } from "./components/palette-grid";
import { PendingNote } from "./components/pending-note";
import { ProductsByColourTable } from "./components/products-by-colour-table";
import { RecommendationPanel } from "./components/recommendation-panel";
import { ReportPanel } from "./components/report-panel";
import { SiteBarChart } from "./components/site-bar-chart";

/**
 * Cap Colour Selection — the whole PSA screen, in the order `app_dash_dataiku.py` renders it.
 *
 * Three rules from the Dash callbacks are behavioural, not cosmetic, and are reproduced here:
 *
 *  1. The recommendation renders ONLY on the Recommend click. Changing the product, presentation or
 *     seal manufacturer clears it (`_cap_output` returns "" unless `ctx.triggered_id == "cap-btn"`) —
 *     a preview that silently belonged to a different selection would be worse than no preview.
 *  2. Changing the product resets the presentation to nothing (`_cap_pres_opts` returns `[], None`),
 *     because a `source_row` only means anything within one program.
 *  3. Refreshing drops the product selection (`_cap_refresh` returns `..., None, ...`): the row keys
 *     are re-read from the sheet, so the previous choice may no longer exist.
 *
 * The Product/Presentation pair is the page's single source of truth — the recommendation, the .docx
 * export and the PSA report all read it, exactly as the Dash `State(...)`s did.
 */

/** The public Smartsheet intake form (`app_dash_dataiku.py:88`) for products missing from the sheet. */
const SMARTSHEET_FORM_URL =
  "https://app.smartsheet.com/b/form/019d8d54f6047f0587f79aff20219714";

/**
 * Purely decorative section break — no `role="separator"`, because the `h2` under each break already
 * announces the new section and a second announcement would only add noise.
 *
 * Preflight is not loaded (`styles.css` imports Tailwind's utilities only), so an `<hr>` would keep the
 * browser's inset border. An explicit top border on a div is predictable in both themes.
 */
function Divider() {
  return <div className="my-6 border-separator border-t border-solid" />;
}

export function CapColourSelectionPage() {
  const [vendor, setVendor] = useState<Vendor>("Datwyler");
  const [selectedColour, setSelectedColour] = useState<string | null>(null);
  // Select values are strings; `source_row` is compared as a string everywhere downstream.
  const [program, setProgram] = useState("");
  const [presentation, setPresentation] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const programs = usePrograms();
  const presentations = usePresentations(program || null);
  const refresh = useRefresh();
  const recommend = useRecommend();
  const capExport = useCapExport();

  /** Rule 1: drop the preview and any message, so nothing on screen outlives its selection. */
  const clearPreview = () => {
    setValidation(null);
    setExportError(null);
    recommend.reset();
    capExport.reset();
  };

  const changeVendor = (next: Vendor) => {
    if (next === vendor) return;
    setVendor(next);
    setSelectedColour(null); // the other vendor's palette does not contain this colour
    // Deliberately NOT clearPreview(). This toggle used to drive the recommendation, so switching it
    // invalidated the result; since the recommendation spans BOTH catalogues it cannot. Rule 1 is "nothing
    // outlives its selection", and the selection here — product, presentation — has not changed. Clearing
    // would throw away a still-correct answer because the user looked at the other catalogue.
  };

  const changeProgram = (next: string) => {
    setProgram(next);
    setPresentation(""); // rule 2
    clearPreview();
  };

  const changePresentation = (next: string) => {
    setPresentation(next);
    clearPreview();
  };

  const handleRefresh = () => {
    setProgram(""); // rule 3
    setPresentation("");
    clearPreview();
    refresh.mutate();
  };

  const handleRecommend = () => {
    // The Dash screen's two validation messages, answered here instead of round-tripping.
    if (!program) {
      setValidation("Select a product first, then click Recommend.");
      return;
    }
    if (!presentation) {
      setValidation("Select a presentation, then click Recommend.");
      return;
    }
    setValidation(null);
    // No `vendor`: the recommendation spans both catalogues and names the supplier of each
    // colour. The toggle above governs the swatch grid only.
    recommend.mutate({ program, source_row: presentation });
  };

  /**
   * Export recomputes the recommendation server-side through the same path the screen used, so the
   * .docx always matches what was rendered. Dash silently ignored a click with nothing selected; a
   * disabled button says the same thing honestly.
   */
  const handleExport = () => {
    setExportError(null);
    capExport.mutate(
      { program, source_row: presentation },
      {
        onSuccess: (result) => {
          if (!(result.ok && result.download_id)) {
            setExportError(result.message);
            return;
          }
          downloadGeneratedFile(
            result.download_id,
            result.filename ?? "cap_recommendation.docx"
          ).catch((error: unknown) =>
            setExportError(describeError(error, "The download failed."))
          );
        },
      }
    );
  };

  const programOptions = (programs.data ?? []).map((entry) => ({
    value: entry.program_no,
    label: entry.label,
  }));
  const presentationOptions = (presentations.data ?? []).map((entry) => ({
    value: String(entry.source_row),
    label: entry.label,
  }));

  return (
    // `relative` is load-bearing, not cosmetic. Unity's `VisuallyHidden` renders
    // `position: absolute`, and with no positioned ancestor anywhere above it the containing
    // block became the document itself, so each sr-only span resolved its static position
    // against the initial containing block instead of against this page. Measured in Edge:
    // the nine spans in `SiteBarChart` (one per manufacturing site) landed at document
    // y=1158 — 201px BELOW the footer — stretching documentElement.scrollHeight to 1158 while
    // body was 957. That produced a second scrollbar and a band of dead white space under the
    // footer. One positioned ancestor fixes the whole class: overflow 258px -> 57px, and the
    // residual 57 is the shell's own footer, which sits outside its `h-dvh` box on every route.
    <div className="relative mx-auto max-w-4xl">
      <H1 styledAs="h2">Cap Colour Selection</H1>
      <P className="text-muted">
        Browse the available cap colours by manufacturer, see which products use
        a colour, pick a product + presentation for a colour recommendation,
        then generate its PSA similarity report — all in one place.
      </P>

      <div className="my-4 flex flex-wrap items-center gap-2">
        <Button
          disabled={refresh.isPending}
          onClick={handleRefresh}
          startIcon="arrows-rotate"
          variant="secondary"
        >
          Refresh from Smartsheet
        </Button>
        {refresh.isPending ? (
          <Caption className="flex items-center gap-2 text-muted">
            <Spinner className="h-5" /> Re-ingesting the live Smartsheet…
          </Caption>
        ) : null}
        {refresh.isSuccess ? (
          <Caption className="text-muted">{refresh.data.message}</Caption>
        ) : null}
      </div>
      {refresh.isError ? (
        <Alert className="my-2" status="error">
          {describeError(refresh.error, "The Smartsheet refresh failed.")}
        </Alert>
      ) : null}

      <div className="my-4 flex flex-wrap items-center gap-2">
        <span className="font-semibold text-sm" id="cap-vendor-label">
          Seal manufacturer:
        </span>
        {/*
          A fieldset (implicit role=group) labelled by the visible text, rather than a div with
          role="group" — same semantics, no redundant ARIA. No <legend>: its rendered position is not
          reliably inline across browsers, and the adjacent span already labels the group.
        */}
        <fieldset
          aria-labelledby="cap-vendor-label"
          className="m-0 flex min-w-0 gap-1 border-0 p-0"
        >
          {VENDORS.map((name) => (
            <Button
              active={vendor === name}
              activeVariant="primary"
              aria-pressed={vendor === name}
              key={name}
              onClick={() => changeVendor(name)}
              size="small"
              variant="secondary"
            >
              {name}
            </Button>
          ))}
        </fieldset>
      </div>

      <H2 styledAs="h4">Available cap colours (off-the-shelf)</H2>
      <Caption className="block text-muted">
        Click a colour to see which products already use it.
      </Caption>
      <PaletteGrid
        onSelectColor={setSelectedColour}
        selectedColor={selectedColour}
        vendor={vendor}
      />
      <ProductsByColourTable colorName={selectedColour} vendor={vendor} />

      {/* A removed colour must not stay selected below, so an edit clears the selection. */}
      <PaletteEditor
        onPaletteChanged={() => setSelectedColour(null)}
        vendor={vendor}
      />

      <H2 styledAs="h4">Products per manufacturing site</H2>
      <SiteBarChart />

      <Divider />

      <H2 styledAs="h4">Recommend a cap colour</H2>
      <Caption className="block text-muted">
        Select a product and one of its presentations, then click Recommend.
      </Caption>

      <div className="my-4 flex flex-wrap items-end gap-4">
        <Field className="min-w-60 flex-1" floatingLabel label="Product">
          <Select
            clearable
            onChange={changeProgram}
            options={programOptions}
            placeholder="— select a product —"
            searchable
            value={program}
          />
        </Field>
        <Field className="min-w-60 flex-1" floatingLabel label="Presentation">
          <Select
            clearable
            disabled={!program}
            onChange={changePresentation}
            options={presentationOptions}
            placeholder="— select a presentation —"
            searchable
            value={presentation}
          />
        </Field>
      </div>

      {programs.isError ? (
        <Alert className="my-2" status="error">
          {describeError(programs.error, "Could not load the product list.")}
        </Alert>
      ) : null}
      {programs.isSuccess && programOptions.length === 0 ? (
        <Alert className="my-2" icon="triangle-exclamation" status="info">
          No in-scope products are available — click Refresh from Smartsheet.
        </Alert>
      ) : null}
      {presentations.isError ? (
        <Alert className="my-2" status="error">
          {describeError(
            presentations.error,
            "Could not load this product's presentations."
          )}
        </Alert>
      ) : null}

      {/*
        Body text, not a `Caption`: this is the way out of a dead end (the product is not on the
        sheet), so it has to be readable at a glance rather than sized like a footnote.
      */}
      <P className="my-2">
        Product or presentation not listed?{" "}
        <a
          className="font-semibold text-primary underline"
          href={SMARTSHEET_FORM_URL}
          rel="noopener noreferrer"
          target="_blank"
        >
          Add a new entry to the Smartsheet →
        </a>
      </P>

      {/*
        The same action-row pattern as "Refresh from Smartsheet" above: one flex row owning the vertical
        rhythm (`my-4`) with the in-flight note beside the button instead of under it. This button
        previously carried no spacing class at all, so it sat flush against the paragraph above while the
        export button below had `my-2` — the inconsistency between the three action rows is what read as
        badly spaced.
      */}
      <div className="my-4 flex flex-wrap items-center gap-2">
        <Button
          disabled={recommend.isPending}
          onClick={handleRecommend}
          variant="primary"
        >
          Recommend cap colours
        </Button>
        {recommend.isPending ? (
          <PendingNote>
            Re-ingesting the live Smartsheet and ranking the palette…
          </PendingNote>
        ) : null}
      </div>
      {validation ? (
        <Alert className="my-2" icon="triangle-exclamation" status="info">
          {validation}
        </Alert>
      ) : null}
      {recommend.isError ? (
        <Alert className="my-2" status="error">
          {describeError(recommend.error, "The recommendation failed.")}
        </Alert>
      ) : null}
      {recommend.data ? <RecommendationPanel result={recommend.data} /> : null}

      <div className="my-4 flex flex-wrap items-center gap-2">
        <Button
          disabled={!program || !presentation || capExport.isPending}
          onClick={handleExport}
          startIcon="download"
          variant="secondary"
        >
          {capExport.isPending
            ? "Preparing the .docx…"
            : "Export recommendation (.docx)"}
        </Button>
      </div>
      {capExport.isError ? (
        <Alert className="my-2" status="error">
          {describeError(capExport.error, "The export failed.")}
        </Alert>
      ) : null}
      {exportError ? (
        <Alert className="my-2" status="error">
          {exportError}
        </Alert>
      ) : null}

      <Divider />

      <H2 styledAs="h4">PSA similarity report</H2>
      <Caption className="mb-2 block text-muted">
        Generates the similarity report for the product + presentation selected
        above — automatically, from the live Smartsheet (no file upload needed).
      </Caption>
      <ReportPanel
        presentation={presentation || null}
        program={program || null}
      />
    </div>
  );
}
