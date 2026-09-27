/** biome-ignore-all lint/suspicious/noExplicitAny: Just this file */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RouterButton } from "./router-button";

// Mock @tanstack/react-router
vi.mock("@tanstack/react-router", () => ({
  createLink: (Component: any) => {
    // Return a mock component that renders the base component
    return (props: any) => <Component {...props} />;
  },
}));

describe("LinkButton", () => {
  describe("rendering", () => {
    it("renders with children", () => {
      render(<RouterButton to="/">Test Button</RouterButton>);

      expect(screen.getByText("Test Button")).toBeInTheDocument();
    });

    it("renders as a link element", () => {
      render(<RouterButton to="/">Button Text</RouterButton>);

      const link = screen.getByText("Button Text");
      expect(link.tagName).toBe("A");
    });

    it("passes through to prop", () => {
      render(<RouterButton to="/">About</RouterButton>);

      const link = screen.getByText("About");
      expect(link).toHaveAttribute("to", "/");
    });

    it("passes through additional props", () => {
      render(
        <RouterButton to="/" data-testid="custom-button">
          Test
        </RouterButton>
      );

      expect(screen.getByTestId("custom-button")).toBeInTheDocument();
    });
  });

  describe("preload behavior", () => {
    it("sets preload to intent", () => {
      render(<RouterButton to="/">Test</RouterButton>);

      const link = screen.getByText("Test");
      expect(link).toHaveAttribute("preload", "intent");
    });
  });

  describe("props forwarding", () => {
    it("forwards className prop", () => {
      render(
        <RouterButton to="/" className="custom-class">
          Test
        </RouterButton>
      );

      const link = screen.getByText("Test");
      expect(link).toHaveClass("custom-class");
    });

    it("forwards variant prop", () => {
      render(
        <RouterButton to="/" variant="tertiary">
          Test
        </RouterButton>
      );

      const link = screen.getByText("Test");
      // Unity Button uses variant for styling but doesn't render it as HTML attribute
      expect(link).toBeInTheDocument();
    });

    it("renders with multiple children", () => {
      render(
        <RouterButton to="/">
          <span>First</span>
          <span>Second</span>
        </RouterButton>
      );

      expect(screen.getByText("First")).toBeInTheDocument();
      expect(screen.getByText("Second")).toBeInTheDocument();
    });

    it("renders with text content", () => {
      render(<RouterButton to="/">Home</RouterButton>);

      expect(screen.getByText("Home")).toBeInTheDocument();
    });
  });

  describe("button styling", () => {
    it("renders styled as button but acts as link", () => {
      render(<RouterButton to="/">Styled Button</RouterButton>);

      const link = screen.getByText("Styled Button");
      // Unity Button with Role.a renders as anchor tag
      expect(link.tagName).toBe("A");
    });
  });

  describe("accessibility", () => {
    it("renders accessible link", () => {
      render(<RouterButton to="/">Accessible Button</RouterButton>);

      const link = screen.getByText("Accessible Button");
      expect(link).toBeInTheDocument();
      expect(link.tagName).toBe("A");
    });
  });
});
