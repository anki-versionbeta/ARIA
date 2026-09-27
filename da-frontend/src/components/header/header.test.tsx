import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Header } from "./header";

// Mock @tanstack/react-router
vi.mock("@tanstack/react-router", () => ({
  Link: ({ to, children, ...props }: any) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
}));

describe("Header", () => {
  const renderWithRouter = (component: React.ReactElement) => {
    return render(component);
  };

  it("renders without crashing", () => {
    renderWithRouter(<Header />);
    expect(screen.getByText("ARIA")).toBeInTheDocument();
  });

  it("displays the app logo", () => {
    renderWithRouter(<Header />);
    const logo = screen.getByRole("img");
    expect(logo).toBeInTheDocument();
  });

  it("displays the app name with correct heading level", () => {
    renderWithRouter(<Header />);
    const heading = screen.getByText("ARIA");
    expect(heading).toBeInTheDocument();
    expect(heading.tagName).toBe("H1");
  });

  it("has a clickable link to home page", () => {
    renderWithRouter(<Header />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/");
  });

  it("renders children content", () => {
    renderWithRouter(
      <Header>
        <div>Test Child Content</div>
      </Header>
    );
    expect(screen.getByText("Test Child Content")).toBeInTheDocument();
  });

  it("accepts additional props", () => {
    renderWithRouter(<Header data-testid="custom-header" />);
    expect(screen.getByTestId("custom-header")).toBeInTheDocument();
  });

  it("renders multiple children correctly", () => {
    renderWithRouter(
      <Header>
        <button type="button">Button 1</button>
        <button type="button">Button 2</button>
      </Header>
    );
    expect(
      screen.getByRole("button", { name: "Button 1" })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Button 2" })
    ).toBeInTheDocument();
  });
});
