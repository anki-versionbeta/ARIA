/**
 * PdfOverlayEditor — PDF.js renders each page to a <canvas>; we overlay our OWN
 * inputs at each AcroForm field's position so the user edits "inside" the document.
 * Values live in React state (reportlab forms don't round-trip through PDF.js's own
 * storage), and are read via getFieldValues() on submit → baked server-side.
 *
 * Ported from the app being replaced. The only change is where the bytes come from: the
 * source fetched its own `/api/atr/file?...` URL, and here the PDF is a run output file
 * streamed by the platform.
 *
 * Robustness vs. the naive version:
 *  - Each page is a React-rendered wrapper; inputs are positioned PAGE-LOCAL (no
 *    cumulative-top / container-padding / centering math), so they line up exactly.
 *  - Coordinates come from PDF.js `viewport.convertToViewportPoint` (handles the
 *    Y-flip and rotation correctly).
 *  - Canvases render in a separate effect keyed on the page model; render tasks are
 *    cancelled on cleanup and guarded so React StrictMode's double-mount can't
 *    double-render the same canvas.
 */
import { Alert, Spinner } from "@abbvie-unity/react";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

const SCALE = 1.4;

export interface PdfOverlayHandle {
  getFieldValues: () => Record<string, string>;
}

type FieldKind = "text" | "textarea" | "radio" | "checkbox" | "select";

interface FieldModel {
  id: string;
  name: string;
  kind: FieldKind;
  left: number;
  top: number;
  width: number;
  height: number;
  onValue: string; // this widget's "on"/export value (radio & checkbox)
  options?: { value: string; label: string }[];
}

interface PageModel {
  index: number; // 1-based
  width: number;
  height: number;
  fields: FieldModel[];
}

// biome-ignore lint/suspicious/noExplicitAny: pdf.js annotation shape
function annKind(a: any): FieldKind | null {
  if (a.radioButton) return "radio";
  if (a.checkBox) return "checkbox";
  if (a.fieldType === "Ch") return "select";
  if (a.fieldType === "Tx") return a.multiLine ? "textarea" : "text";
  return null;
}

/** The session is an httpOnly cookie, so the PDF fetch must send credentials. */
async function fetchArrayBuffer(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url, { credentials: "include" });
  if (!response.ok) {
    throw new Error(`Could not load the PDF (HTTP ${response.status})`);
  }
  return response.arrayBuffer();
}

export const PdfOverlayEditor = forwardRef<
  PdfOverlayHandle,
  { src: string; height?: number }
>(function PdfOverlayEditor({ src, height = 820 }, ref) {
  const [pages, setPages] = useState<PageModel[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const valuesRef = useRef<Record<string, string>>({});
  // page index → { page proxy, viewport } for the canvas-render effect
  // biome-ignore lint/suspicious/noExplicitAny: pdf.js page/viewport have no exported runtime types
  const proxiesRef = useRef<Map<number, { page: any; viewport: any }>>(
    new Map()
  );
  const canvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map());

  useImperativeHandle(
    ref,
    () => ({ getFieldValues: () => valuesRef.current }),
    []
  );

  const setValue = useCallback((name: string, val: string) => {
    setValues((prev) => {
      const next = { ...prev, [name]: val };
      valuesRef.current = next;
      return next;
    });
  }, []);

  // --- load document + compute page/field models -----------------------------
  useEffect(() => {
    let cancelled = false;
    // biome-ignore lint/suspicious/noExplicitAny: pdf.js loading task / doc
    let task: any = null;
    // biome-ignore lint/suspicious/noExplicitAny: pdf.js doc
    let doc: any = null;
    setLoading(true);
    setError(null);
    setPages([]);
    proxiesRef.current = new Map();
    canvasRefs.current = new Map();

    (async () => {
      try {
        const buf = await fetchArrayBuffer(src);
        if (cancelled) return;
        task = pdfjsLib.getDocument({ data: buf });
        doc = await task.promise;
        if (cancelled) {
          doc.destroy();
          return;
        }
        const models: PageModel[] = [];
        const initVals: Record<string, string> = {};
        const nameCounts: Record<string, number> = {};
        for (let p = 1; p <= doc.numPages; p++) {
          const page = await doc.getPage(p);
          if (cancelled) return;
          const viewport = page.getViewport({ scale: SCALE });
          proxiesRef.current.set(p, { page, viewport });
          // biome-ignore lint/suspicious/noExplicitAny: pdf.js annotations
          const anns: any[] = await page.getAnnotations({ intent: "display" });
          const fields: FieldModel[] = [];
          for (const a of anns) {
            const kind = a.fieldName && a.rect ? annKind(a) : null;
            if (!kind) continue;
            nameCounts[a.fieldName] = (nameCounts[a.fieldName] ?? 0) + 1;
            const [x1, y1, x2, y2] = a.rect as number[];
            const [vx1, vy1] = viewport.convertToViewportPoint(x1, y2); // top-left
            const [vx2, vy2] = viewport.convertToViewportPoint(x2, y1); // bottom-right
            fields.push({
              id: a.id,
              name: a.fieldName,
              kind,
              left: Math.min(vx1, vx2),
              top: Math.min(vy1, vy2),
              width: Math.abs(vx2 - vx1),
              height: Math.abs(vy2 - vy1),
              onValue: a.buttonValue ?? a.exportValue ?? "Yes",
              options: a.options?.map(
                (o: { exportValue: string; displayValue: string }) => ({
                  value: o.exportValue,
                  label: o.displayValue,
                })
              ),
            });
            if (!(a.fieldName in initVals)) {
              const on =
                a.fieldValue != null &&
                a.fieldValue !== "" &&
                a.fieldValue !== "Off";
              if (kind === "text" || kind === "textarea" || kind === "select")
                initVals[a.fieldName] =
                  a.fieldValue != null ? String(a.fieldValue) : "";
              else
                initVals[a.fieldName] = on
                  ? (a.buttonValue ?? a.exportValue ?? "Yes")
                  : "Off";
            }
          }
          models.push({
            index: p,
            width: viewport.width,
            height: viewport.height,
            fields,
          });
        }
        if (cancelled) return;
        // A Btn field with >1 widget sharing the same name is a single-select
        // radio group (reportlab emits these as "checkboxes" — reclassify them).
        for (const pm of models)
          for (const f of pm.fields)
            if (f.kind === "checkbox" && nameCounts[f.name] > 1)
              f.kind = "radio";
        valuesRef.current = initVals;
        setValues(initVals);
        setPages(models);
        setLoading(false);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      cancelled = true;
      try {
        task?.destroy?.();
        doc?.destroy?.();
      } catch {
        /* ignore teardown races */
      }
    };
  }, [src]);

  // --- render each page's canvas ---------------------------------------------
  useEffect(() => {
    if (pages.length === 0) return;
    let cancelled = false;
    // biome-ignore lint/suspicious/noExplicitAny: render tasks
    const tasks: any[] = [];
    for (const pm of pages) {
      const canvas = canvasRefs.current.get(pm.index);
      const entry = proxiesRef.current.get(pm.index);
      if (!canvas || !entry || canvas.dataset.rendered === "1") continue;
      canvas.width = entry.viewport.width;
      canvas.height = entry.viewport.height;
      const ctx = canvas.getContext("2d");
      if (!ctx) continue;
      const t = entry.page.render({
        canvasContext: ctx,
        viewport: entry.viewport,
      });
      tasks.push(t);
      t.promise
        .then(() => {
          if (!cancelled) canvas.dataset.rendered = "1";
        })
        .catch(() => {
          /* cancelled render */
        });
    }
    return () => {
      cancelled = true;
      for (const t of tasks) {
        try {
          t.cancel();
        } catch {
          /* ignore */
        }
      }
    };
  }, [pages]);

  if (error)
    return <Alert status="error">Could not load the preview: {error}</Alert>;

  return (
    <div
      className="relative overflow-hidden rounded-medium border border-separator bg-02"
      style={{ height }}
    >
      {loading && (
        <div className="absolute inset-0 z-20 flex items-center justify-center">
          <Spinner className="h-9" />
        </div>
      )}
      <div className="absolute inset-0 overflow-auto py-4">
        {pages.map((pm) => (
          <div
            key={pm.index}
            className="relative mx-auto mb-2 shadow"
            style={{ width: pm.width, height: pm.height }}
          >
            <canvas
              ref={(el) => {
                if (el) canvasRefs.current.set(pm.index, el);
              }}
              width={pm.width}
              height={pm.height}
              className="block"
            />
            {pm.fields.map((f) => (
              <OverlayInput
                key={`${f.name}-${f.id}`}
                field={f}
                values={values}
                onChange={setValue}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
});

function OverlayInput({
  field,
  values,
  onChange,
}: {
  field: FieldModel;
  values: Record<string, string>;
  onChange: (name: string, val: string) => void;
}) {
  const { name, kind, left, top, width, height } = field;
  const box: React.CSSProperties = {
    position: "absolute",
    left,
    top,
    width,
    height,
    boxSizing: "border-box",
    zIndex: 10,
  };
  const textStyle: React.CSSProperties = {
    ...box,
    border: "1px solid rgba(0,102,204,0.7)",
    borderRadius: 2,
    background: "rgba(255,255,255,0.9)",
    fontSize: Math.max(9, Math.round(height * 0.5)),
    lineHeight: 1.1,
    padding: "0 3px",
    outline: "none",
  };

  if (kind === "radio" || kind === "checkbox") {
    const size = Math.min(width, height, 16);
    // Each widget is "on" only when the field's value equals THIS widget's value
    // (so single-select groups sharing one name light up exactly one option).
    const checked = values[name] === field.onValue;
    return (
      <input
        type={kind}
        name={name}
        data-field={name}
        aria-label={name}
        value={field.onValue}
        checked={checked}
        onChange={() =>
          kind === "radio"
            ? onChange(name, field.onValue)
            : onChange(name, checked ? "Off" : field.onValue)
        }
        style={{
          ...box,
          left: left + (width - size) / 2,
          top: top + (height - size) / 2,
          width: size,
          height: size,
          margin: 0,
          cursor: "pointer",
          accentColor: "#0066cc",
        }}
      />
    );
  }

  if (kind === "select") {
    return (
      <select
        value={values[name] ?? ""}
        data-field={name}
        aria-label={name}
        onChange={(e) => onChange(name, e.target.value)}
        style={textStyle}
      >
        <option value="" />
        {field.options?.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }

  if (kind === "textarea") {
    return (
      <textarea
        value={values[name] ?? ""}
        data-field={name}
        aria-label={name}
        onChange={(e) => onChange(name, e.target.value)}
        style={{ ...textStyle, resize: "none", overflow: "auto" }}
      />
    );
  }

  return (
    <input
      type="text"
      value={values[name] ?? ""}
      data-field={name}
      aria-label={name}
      onChange={(e) => onChange(name, e.target.value)}
      style={textStyle}
    />
  );
}
