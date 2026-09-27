import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RouterLink } from "./router-link";

// Mock @tanstack/react-router
vi.mock("@tanstack/react-router", () => ({
  createLink: (Component: any) => {
    // Return a mock component that renders the base component
    return (props: any) => <Component {...props} />;
  },
}));

describe("LinkLink", () => {
  describe("rendering", () => {
    it("renders with children", () => {
      render(<RouterLink to="/">Test Link</RouterLink>);

      expect(screen.getByText("Test Link")).toBeInTheDocument();
    });

    it("renders as a link element", () => {
      render(<RouterLink to="/">Link Text</RouterLink>);

      const link = screen.getByText("Link Text");
      expect(link.tagName).toBe("A");
    });

    it("passes through to prop", () => {
      render(<RouterLink to="/">About</RouterLink>);

      const link = screen.getByText("About");
      expect(link).toHaveAttribute("to", "/");
    });

    it("passes through additional props", () => {
      render(
        <RouterLink to="/" data-testid="custom-link">
          Test
        </RouterLink>
      );

      expect(screen.getByTestId("custom-link")).toBeInTheDocument();
    });
  });

  describe("preload behavior", () => {
    it("sets preload to intent", () => {
      render(<RouterLink to="/">Test</RouterLink>);

      const link = screen.getByText("Test");
      expect(link).toHaveAttribute("preload", "intent");
    });
  });

  describe("props forwarding", () => {
    it("forwards className prop", () => {
      render(
        <RouterLink to="/" className="custom-class">
          Test
        </RouterLink>
      );

      const link = screen.getByText("Test");
      expect(link).toHaveClass("custom-class");
    });

    it("renders with multiple children", () => {
      render(
        <RouterLink to="/">
          <span>First</span>
          <span>Second</span>
        </RouterLink>
      );

      expect(screen.getByText("First")).toBeInTheDocument();
      expect(screen.getByText("Second")).toBeInTheDocument();
    });

    it("renders with text content", () => {
      render(<RouterLink to="/">Home</RouterLink>);

      expect(screen.getByText("Home")).toBeInTheDocument();
    });
  });

  describe("accessibility", () => {
    it("renders accessible link", () => {
      render(<RouterLink to="/">Accessible Link</RouterLink>);

      const link = screen.getByText("Accessible Link");
      expect(link).toBeInTheDocument();
      expect(link.tagName).toBe("A");
    });
  });
});
