import Quill from "quill";
import "quill/dist/quill.snow.css";
import { useEffect, useRef } from "react";

/**
 * The same editor and toolbar the app being replaced used, so authors see no change:
 * headings 1-3, bold/italic/underline, ordered and bullet lists, and clear-formatting.
 *
 * Content is exchanged as HTML because that is what the docx emitter parses. The
 * server sanitises it on save — Quill's HTML export carries a known XSS advisory, so
 * the boundary is defended there rather than trusted here.
 */
const TOOLBAR = [
  [{ header: [1, 2, 3, false] }],
  ["bold", "italic", "underline"],
  [{ list: "ordered" }, { list: "bullet" }],
  ["clean"],
];

type Props = {
  initialHtml: string;
  readOnly?: boolean;
  onChange: (html: string) => void;
};

/**
 * Quill owns its own DOM, so swapping content in place is error-prone. Callers give
 * this a React `key` that changes per section and revision, which remounts it cleanly
 * — including when two sections happen to hold identical content.
 */
export function RichTextEditor({
  initialHtml,
  readOnly = false,
  onChange,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const container = document.createElement("div");
    host.appendChild(container);

    const quill = new Quill(container, {
      theme: "snow",
      readOnly,
      modules: { toolbar: readOnly ? false : TOOLBAR },
    });

    // dangerouslyPasteHTML rather than assigning innerHTML, so Quill normalises the
    // markup into its own model instead of holding arbitrary DOM.
    if (initialHtml) {
      quill.clipboard.dangerouslyPasteHTML(initialHtml, "silent");
    }

    quill.on("text-change", () => {
      onChangeRef.current(quill.getSemanticHTML());
    });

    return () => {
      host.innerHTML = "";
    };
  }, [initialHtml, readOnly]);

  return <div ref={hostRef} className="min-h-64 w-full" />;
}
