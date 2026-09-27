/**
 * Tests for `PdfOverlayEditor` — the reviewer-facing overlay that floats real form
 * controls on top of a PDF.js-rendered page.
 *
 * Why the component is faked at the `pdfjs-dist` seam rather than driven with a real PDF:
 *
 *  - jsdom has neither a 2D canvas implementation nor a Worker that can host
 *    `pdf.worker.min.mjs`, so PDF.js cannot rasterise anything here. A real fixture PDF
 *    would only ever exercise the error path.
 *  - Everything this component is actually responsible for lives *above* PDF.js: turning
 *    an AcroForm annotation list into positioned controls, flipping PDF user-space Y into
 *    viewport space, reclassifying reportlab's same-named "checkboxes" into single-select
 *    radio groups, seeding initial values, and reporting edits back through the imperative
 *    handle. All of that is deterministic given an annotation list, which is exactly what
 *    the fake document supplies.
 *
 * The fake is deliberately *loud*: `getDocument` throws unless it is handed
 * `{ data: ArrayBuffer }`, `getViewport` throws on any scale but the component's own, and
 * `getAnnotations` throws on any intent but "display". If the component's call shape drifts,
 * these tests fail with a pointed message instead of quietly asserting nothing. The
 * `?url` worker import is stubbed too — Vite's asset-URL suffix has no meaning in a test
 * environment, and the component assigns it to `GlobalWorkerOptions` at module load.
 *
 * `convertToViewportPoint` is implemented (not stubbed with a constant) so the geometry
 * assertions genuinely pin the Y-flip: a field near the top of the PDF page must end up
 * with a small `top`, and the component must not care which corner of `rect` is which.
 */

import { UnityProvider } from "@abbvie-unity/react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getDocument } = vi.hoisted(() => ({ getDocument: vi.fn() }));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: (...args: unknown[]) => getDocument(...args),
}));

// Vite rewrites `?url` to an emitted asset path at build time; in tests it is just a string.
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({
  default: "/assets/pdf.worker.min.mjs",
}));

import * as pdfjsLib from "pdfjs-dist";
import {
  PdfOverlayEditor,
  type PdfOverlayHandle,
} from "@/silos/mfg_atr/pdf-overlay-editor";

const SCALE = 1.4;
const PAGE_W = 612;
const PAGE_H = 792;
const VIEW_H = PAGE_H * SCALE;

/** Annotation fixtures mirror pdf.js's deliberately loose runtime shape. */
type Ann = Record<string, any>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type FakeViewport = {
  width: number;
  height: number;
  convertToViewportPoint: (x: number, y: number) => number[];
};

interface RenderArgs {
  canvasContext: unknown;
  viewport: FakeViewport;
}

interface RenderRecord extends RenderArgs {
  pageIndex: number;
  cancelled: boolean;
  finish: () => void;
}

/**
 * Build a fake document whose pages carry `specs[i]` annotations, and point the mocked
 * `getDocument` at it. `holdDoc` leaves `task.promise` pending so a test can unmount
 * mid-load and exercise the cancellation path.
 */
function installPdf(specs: Ann[][], { holdDoc = false } = {}) {
  const renders: RenderRecord[] = [];
  const destroyed: string[] = [];

  const pages = specs.map((annotations, i) => {
    const viewport = {
      width: PAGE_W * SCALE,
      height: VIEW_H,
      // PDF user space has Y growing upward from the bottom-left; the viewport does not.
      convertToViewportPoint: (x: number, y: number) => [
        x * SCALE,
        VIEW_H - y * SCALE,
      ],
    };
    return {
      getViewport: ({ scale }: { scale: number }) => {
        if (scale !== SCALE)
          throw new Error(`unexpected viewport scale ${scale}`);
        return viewport;
      },
      getAnnotations: ({ intent }: { intent: string }) => {
        if (intent !== "display")
          throw new Error(`unexpected annotation intent ${intent}`);
        return Promise.resolve(annotations);
      },
      render: ({ canvasContext, viewport: vp }: RenderArgs) => {
        const gate = deferred<void>();
        const record: RenderRecord = {
          pageIndex: i + 1,
          canvasContext,
          viewport: vp,
          cancelled: false,
          finish: () => gate.resolve(),
        };
        renders.push(record);
        return {
          promise: gate.promise,
          cancel: () => {
            record.cancelled = true;
            gate.reject(new Error("render cancelled"));
          },
        };
      },
    };
  });

  const doc = {
    numPages: pages.length,
    getPage: (p: number) => {
      const page = pages[p - 1];
      if (!page) throw new Error(`page ${p} was requested but does not exist`);
      return Promise.resolve(page);
    },
    destroy: () => destroyed.push("doc"),
  };

  const docGate = deferred<typeof doc>();
  if (!holdDoc) docGate.resolve(doc);

  const task = {
    promise: docGate.promise,
    destroy: () => destroyed.push("task"),
  };

  getDocument.mockImplementation((args: { data?: unknown }) => {
    if (!(args?.data instanceof ArrayBuffer)) {
      throw new Error("getDocument was not handed the fetched PDF bytes");
    }
    return task;
  });

  return { renders, destroyed, releaseDoc: () => docGate.resolve(doc) };
}

const fetchMock = vi.fn();

function wrap(node: ReactNode) {
  return (
    <UnityProvider defaultMode="light" density="balanced">
      {node}
    </UnityProvider>
  );
}

// ---------------------------------------------------------------------------
// Annotation fixtures. Rects are PDF user space: [x1, yBottom, x2, yTop].
// ---------------------------------------------------------------------------

/** A number, not a string, so the `String(fieldValue)` coercion is exercised. */
const SPEC_ID: Ann = {
  id: "a1",
  fieldName: "spec_id",
  fieldType: "Tx",
  rect: [72, 700, 272, 716],
  fieldValue: 10352,
};

const COMMENTS: Ann = {
  id: "a2",
  fieldName: "comments",
  fieldType: "Tx",
  multiLine: true,
  rect: [72, 600, 472, 660],
  fieldValue: null,
};

const ASSESSMENT: Ann = {
  id: "a3",
  fieldName: "hqc_assessment",
  fieldType: "Ch",
  rect: [72, 560, 272, 576],
  fieldValue: "meets",
  options: [
    { exportValue: "meets", displayValue: "Meets specification" },
    { exportValue: "deviates", displayValue: "Deviates from specification" },
  ],
};

/** No exportValue and no buttonValue, so the "Yes" fallback is the widget's on-value. */
const REGULATORY: Ann = {
  id: "a4",
  fieldName: "regulatory",
  checkBox: true,
  rect: [72, 520, 86, 534],
  fieldValue: "Off",
};

const GMP: Ann = {
  id: "a5",
  fieldName: "gmp",
  checkBox: true,
  rect: [100, 520, 114, 534],
  exportValue: "Yes",
  fieldValue: "Yes",
};

const DISPOSITION_ACCEPT: Ann = {
  id: "a6",
  fieldName: "disposition",
  radioButton: true,
  rect: [72, 480, 86, 494],
  buttonValue: "accept",
  fieldValue: "accept",
};

const DISPOSITION_REJECT: Ann = {
  id: "a7",
  fieldName: "disposition",
  radioButton: true,
  rect: [120, 480, 134, 494],
  buttonValue: "reject",
  fieldValue: "accept",
};

/** reportlab emits single-select groups as same-named checkboxes; these must reclassify. */
const PHASE_1: Ann = {
  id: "a8",
  fieldName: "phase",
  checkBox: true,
  rect: [72, 440, 86, 454],
  exportValue: "phase1",
  fieldValue: "Off",
};

const PHASE_3: Ann = {
  id: "a9",
  fieldName: "phase",
  checkBox: true,
  rect: [120, 440, 134, 454],
  exportValue: "phase3",
  fieldValue: "Off",
};

/** Three shapes that must never become controls: not a field, not a supported field, no rect. */
const IGNORED: Ann[] = [
  { id: "x1", subtype: "Link", rect: [0, 0, 10, 10] },
  {
    id: "x2",
    fieldName: "signature",
    fieldType: "Sig",
    rect: [72, 100, 272, 140],
  },
  { id: "x3", fieldName: "unplaced", fieldType: "Tx" },
];

const FULL_PAGE: Ann[] = [
  SPEC_ID,
  COMMENTS,
  ASSESSMENT,
  REGULATORY,
  GMP,
  DISPOSITION_ACCEPT,
  DISPOSITION_REJECT,
  PHASE_1,
  PHASE_3,
  ...IGNORED,
];

beforeEach(() => {
  getDocument.mockReset();
  fetchMock.mockReset().mockResolvedValue({
    ok: true,
    status: 200,
    arrayBuffer: async () => new ArrayBuffer(64),
  });
  vi.stubGlobal("fetch", fetchMock);
  // jsdom's canvas has no 2D context; the component only ever forwards it to pdf.js.
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
    ctx: "2d",
  })) as never;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PdfOverlayEditor", () => {
  it("points pdf.js at the worker bundled alongside the app", () => {
    // Assigned at module load; a wrong or empty workerSrc silently breaks every render.
    expect(pdfjsLib.GlobalWorkerOptions.workerSrc).toBe(
      "/assets/pdf.worker.min.mjs"
    );
  });

  it("sends the session cookie when fetching the PDF bytes", async () => {
    installPdf([[SPEC_ID]]);
    render(wrap(<PdfOverlayEditor src="/api/documents/d1/files/f1" />));

    await screen.findByLabelText("spec_id");
    expect(fetchMock).toHaveBeenCalledWith("/api/documents/d1/files/f1", {
      credentials: "include",
    });
  });

  it("shows a busy indicator until the pages are known", async () => {
    const { releaseDoc } = installPdf([[SPEC_ID]], { holdDoc: true });
    const { container } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await waitFor(() => expect(container.querySelector(".h-9")).toBeTruthy());
    expect(screen.queryByLabelText("spec_id")).not.toBeInTheDocument();

    releaseDoc();
    await screen.findByLabelText("spec_id");
    expect(container.querySelector(".h-9")).toBeNull();
  });

  it("renders one canvas per page of the document", async () => {
    installPdf([
      [SPEC_ID],
      [{ ...SPEC_ID, id: "b1", fieldName: "page_two_field" }],
    ]);
    const { container } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await screen.findByLabelText("page_two_field");
    expect(container.querySelectorAll("canvas")).toHaveLength(2);
    expect(screen.getByLabelText("spec_id")).toBeInTheDocument();
  });

  it("builds a control for every supported field kind and nothing else", async () => {
    installPdf([FULL_PAGE]);
    const { container } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await screen.findByLabelText("spec_id");

    expect(screen.getByLabelText("spec_id")).toHaveProperty("type", "text");
    expect(screen.getByLabelText("comments").tagName).toBe("TEXTAREA");
    expect(screen.getByLabelText("hqc_assessment").tagName).toBe("SELECT");
    expect(screen.getByLabelText("regulatory")).toHaveProperty(
      "type",
      "checkbox"
    );
    // Both same-named groups are single-select radios: reportlab's "phase" checkboxes included.
    expect(screen.getAllByRole("radio")).toHaveLength(4);

    // A link annotation, a signature field and a field with no rect must not appear.
    expect(container.querySelectorAll("[data-field]")).toHaveLength(9);
    expect(screen.queryByLabelText("signature")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("unplaced")).not.toBeInTheDocument();
  });

  it("seeds each control from the value already baked into the PDF", async () => {
    installPdf([FULL_PAGE]);
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    expect(await screen.findByLabelText("spec_id")).toHaveValue("10352");
    expect(screen.getByLabelText("comments")).toHaveValue("");
    expect(screen.getByLabelText("hqc_assessment")).toHaveValue("meets");
    expect(screen.getByLabelText("regulatory")).not.toBeChecked();
    expect(screen.getByLabelText("gmp")).toBeChecked();

    const disposition = screen.getAllByLabelText(
      "disposition"
    ) as HTMLInputElement[];
    expect(disposition.map((el) => el.checked)).toEqual([true, false]);
    const phase = screen.getAllByLabelText("phase") as HTMLInputElement[];
    expect(phase.map((el) => el.checked)).toEqual([false, false]);
  });

  it("offers the choice labels the PDF declares for a dropdown", async () => {
    installPdf([[ASSESSMENT]]);
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    await screen.findByLabelText("hqc_assessment");
    expect(
      screen.getByRole("option", { name: "Meets specification" })
    ).toHaveValue("meets");
    expect(
      screen.getByRole("option", { name: "Deviates from specification" })
    ).toHaveValue("deviates");
  });

  it("places a field using viewport coordinates, flipping the PDF's bottom-up Y axis", async () => {
    installPdf([[SPEC_ID]]);
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    // rect [72, 700, 272, 716] on a 792pt page at scale 1.4: the top-left corner is the
    // *upper* Y (716), which lands 106.4px from the top of the viewport.
    const input = await screen.findByLabelText("spec_id");
    // Compared numerically: the browser stringifies these straight from float arithmetic.
    expect(parseFloat(input.style.left)).toBeCloseTo(100.8, 5);
    expect(parseFloat(input.style.top)).toBeCloseTo(106.4, 5);
    expect(parseFloat(input.style.width)).toBeCloseTo(280, 5);
    expect(parseFloat(input.style.height)).toBeCloseTo(22.4, 5);
    // Text is scaled to the box so a short field does not overflow its own outline.
    expect(input.style.fontSize).toBe("11px");
  });

  it("centres a checkbox inside the box the PDF reserved for it", async () => {
    installPdf([[REGULATORY]]);
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    // A 19.6px box holding a 16px control: (19.6 - 16) / 2 = 1.8px of inset on each side.
    const box = await screen.findByLabelText("regulatory");
    expect(box.style.width).toBe("16px");
    expect(parseFloat(box.style.left)).toBeCloseTo(102.6, 5);
  });

  it("reports edited text through the imperative handle", async () => {
    installPdf([[SPEC_ID, COMMENTS]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    const input = await screen.findByLabelText("spec_id");
    await user.clear(input);
    await user.type(input, "CMC-10353");
    await user.type(screen.getByLabelText("comments"), "pH re-tested");

    expect(input).toHaveValue("CMC-10353");
    expect(ref.current?.getFieldValues()).toMatchObject({
      spec_id: "CMC-10353",
      comments: "pH re-tested",
    });
  });

  it("reports the untouched values when nothing is edited", async () => {
    installPdf([[SPEC_ID, REGULATORY, DISPOSITION_ACCEPT, DISPOSITION_REJECT]]);
    const ref = createRef<PdfOverlayHandle>();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    await screen.findByLabelText("spec_id");
    expect(ref.current?.getFieldValues()).toEqual({
      spec_id: "10352",
      regulatory: "Off",
      disposition: "accept",
    });
  });

  it("reports a dropdown choice by its export value, not its label", async () => {
    installPdf([[ASSESSMENT]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    await user.selectOptions(
      await screen.findByLabelText("hqc_assessment"),
      "Deviates from specification"
    );
    expect(ref.current?.getFieldValues()).toEqual({
      hqc_assessment: "deviates",
    });
  });

  it("toggles a lone checkbox between its export value and Off", async () => {
    installPdf([[REGULATORY]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    const box = await screen.findByLabelText("regulatory");
    await user.click(box);
    expect(box).toBeChecked();
    expect(ref.current?.getFieldValues()).toEqual({ regulatory: "Yes" });

    await user.click(box);
    expect(box).not.toBeChecked();
    expect(ref.current?.getFieldValues()).toEqual({ regulatory: "Off" });
  });

  it("lets exactly one widget of a radio group be chosen", async () => {
    installPdf([[DISPOSITION_ACCEPT, DISPOSITION_REJECT]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    const [accept, reject] = (await screen.findAllByLabelText(
      "disposition"
    )) as HTMLInputElement[];
    await user.click(reject);

    expect(reject).toBeChecked();
    expect(accept).not.toBeChecked();
    expect(ref.current?.getFieldValues()).toEqual({ disposition: "reject" });
  });

  it("treats same-named checkboxes as a single-select group, as reportlab intends", async () => {
    installPdf([[PHASE_1, PHASE_3]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    const [phase1, phase3] = (await screen.findAllByLabelText(
      "phase"
    )) as HTMLInputElement[];
    await user.click(phase1);
    expect(ref.current?.getFieldValues()).toEqual({ phase: "phase1" });

    // Choosing the second must move the choice, not add a second "on" widget.
    await user.click(phase3);
    expect(phase1).not.toBeChecked();
    expect(phase3).toBeChecked();
    expect(ref.current?.getFieldValues()).toEqual({ phase: "phase3" });
  });

  it("keeps the value of a field that appears on more than one page in step", async () => {
    // Continuation pages repeat header fields; a reviewer correcting one expects both.
    installPdf([[SPEC_ID], [{ ...SPEC_ID, id: "a1b" }]]);
    const ref = createRef<PdfOverlayHandle>();
    const user = userEvent.setup();
    render(wrap(<PdfOverlayEditor ref={ref} src="/pdf" />));

    const both = await screen.findAllByLabelText("spec_id");
    expect(both).toHaveLength(2);
    await user.clear(both[0]);
    await user.type(both[0], "9");

    expect(both[1]).toHaveValue("9");
    expect(ref.current?.getFieldValues()).toEqual({ spec_id: "9" });
  });

  it("hands each page's canvas and viewport to pdf.js and marks it rendered", async () => {
    const { renders } = installPdf([[SPEC_ID], [{ ...SPEC_ID, id: "b1" }]]);
    const { container } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await screen.findAllByLabelText("spec_id");
    await waitFor(() => expect(renders).toHaveLength(2));
    expect(renders[0].canvasContext).toEqual({ ctx: "2d" });
    expect(renders[0].viewport.height).toBe(VIEW_H);

    // The backing store is sized from the viewport, not the CSS box, or the page is blurry.
    const canvas = container.querySelector("canvas") as HTMLCanvasElement;
    expect(canvas.width).toBe(Math.trunc(PAGE_W * SCALE));
    expect(canvas.height).toBe(Math.trunc(VIEW_H));

    for (const r of renders) r.finish();
    // The rendered marker is what stops StrictMode's second effect re-rasterising a page.
    await waitFor(() => expect(canvas.dataset.rendered).toBe("1"));
  });

  it("skips rasterising when the browser gives no 2D context", async () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never;
    const { renders } = installPdf([[SPEC_ID]]);
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    // The fields still have to be editable — losing the backdrop must not lose the form.
    expect(await screen.findByLabelText("spec_id")).toBeInTheDocument();
    expect(renders).toHaveLength(0);
  });

  it("cancels in-flight rasterisation and releases the document when unmounted", async () => {
    const { renders, destroyed } = installPdf([[SPEC_ID]]);
    const { unmount } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await screen.findByLabelText("spec_id");
    await waitFor(() => expect(renders).toHaveLength(1));

    unmount();
    expect(renders[0].cancelled).toBe(true);
    expect(destroyed).toContain("doc");
    expect(destroyed).toContain("task");
  });

  it("releases a document that arrives after the editor has gone away", async () => {
    const { releaseDoc, destroyed } = installPdf([[SPEC_ID]], {
      holdDoc: true,
    });
    const { unmount } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    await waitFor(() => expect(getDocument).toHaveBeenCalled());
    unmount();
    releaseDoc();

    // Otherwise the abandoned worker leaks for as long as the tab lives.
    await waitFor(() => expect(destroyed).toContain("doc"));
  });

  it("reloads from scratch when pointed at a different file", async () => {
    installPdf([[SPEC_ID]]);
    const { rerender } = render(wrap(<PdfOverlayEditor src="/pdf/one" />));
    await screen.findByLabelText("spec_id");

    installPdf([[{ ...ASSESSMENT, id: "c1" }]]);
    rerender(wrap(<PdfOverlayEditor src="/pdf/two" />));

    expect(await screen.findByLabelText("hqc_assessment")).toBeInTheDocument();
    expect(screen.queryByLabelText("spec_id")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith("/pdf/two", {
      credentials: "include",
    });
  });

  it("explains an HTTP failure instead of showing an empty page", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      arrayBuffer: async () => null,
    });
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    expect(
      await screen.findByText(/could not load the preview/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/HTTP 403/)).toBeInTheDocument();
    expect(getDocument).not.toHaveBeenCalled();
  });

  it("explains a PDF that pdf.js refuses to parse", async () => {
    getDocument.mockImplementation(() => ({
      promise: Promise.reject(new Error("Invalid PDF structure")),
      destroy: () => {},
    }));
    const { container } = render(wrap(<PdfOverlayEditor src="/pdf" />));

    expect(
      await screen.findByText(/Invalid PDF structure/)
    ).toBeInTheDocument();
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("still reports a failure that was not thrown as an Error", async () => {
    getDocument.mockImplementation(() => ({
      // A bare string, because pdf.js worker teardown really does reject with one.
      promise: Promise.reject("worker terminated"),
      destroy: () => {},
    }));
    render(wrap(<PdfOverlayEditor src="/pdf" />));

    expect(await screen.findByText(/worker terminated/)).toBeInTheDocument();
  });

  it("sizes itself to the height the host panel asks for", async () => {
    installPdf([[SPEC_ID]]);
    const { container, unmount } = render(
      wrap(<PdfOverlayEditor src="/pdf" height={420} />)
    );
    await screen.findByLabelText("spec_id");
    expect((container.firstElementChild as HTMLElement).style.height).toBe(
      "420px"
    );
    unmount();

    installPdf([[SPEC_ID]]);
    const second = render(wrap(<PdfOverlayEditor src="/pdf" />));
    await second.findByLabelText("spec_id");
    expect(
      (second.container.firstElementChild as HTMLElement).style.height
    ).toBe("820px");
  });
});
