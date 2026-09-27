import { configure } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import "@testing-library/jest-dom";

// Testing Library's 1s default is too tight for the route-level tests: the real router is
// driven over a memory history, Unity's DataTable paints its rows on a later tick, and v8
// instrumentation slows every one of those steps. At 1s a handful of tests failed on
// roughly one run in three while asserting nothing slow — raising the ceiling removes a
// whole class of false failures without weakening any assertion, since `findBy*` still
// resolves as soon as the element appears. Kept under vitest's own 20s testTimeout so a
// genuinely stuck query still reports as a timeout rather than hanging the worker.
configure({ asyncUtilTimeout: 15000 });

// Mock ResizeObserver for tests
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
