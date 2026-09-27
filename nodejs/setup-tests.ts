import "@testing-library/jest-dom/vitest";
import "@testing-library/jest-dom";

// Mock ResizeObserver for tests
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
