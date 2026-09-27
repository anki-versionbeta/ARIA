import { Spinner } from "@abbvie-unity/react";
import type { ReactNode } from "react";

/**
 * Spinner + explanation of what is being waited on, for the five in-flight states on this page.
 *
 * A `div`, not a `p`: Unity's `Spinner` renders a `div`, which is invalid inside a paragraph (jsdom
 * flags it, browsers silently close the `p` and reflow the text out of it). `role="status"` announces
 * the wait to a screen reader without stealing focus.
 */
export function PendingNote({ children }: { children: ReactNode }) {
  return (
    <div className="my-2 flex items-center gap-2 text-muted" role="status">
      <Spinner className="h-5" /> {children}
    </div>
  );
}
