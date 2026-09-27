import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PanelNavLink, Sidebar } from "./sidebar";

// Map `to` → `href` so <a> receives role="link" (simulates TanStack Router behavior)
vi.mock("@tanstack/react-router", () => ({
  createLink: (Component: any) => {
    return ({ to, ...props }: any) => <Component href={to} {...props} />;
  },
}));

describe("Sidebar", () => {
  it("renders as a complementary landmark", () => {
    render(
      <Sidebar>
        <div>Content</div>
      </Sidebar>
    );
    expect(screen.getByRole("complementary")).toBeInTheDocument();
  });

  it("is expanded by default", () => {
    render(
      <Sidebar>
        <div>Content</div>
      </Sidebar>
    );
    expect(
      screen.getByRole("button", { name: "Toggle panel" })
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("collapses when toggle is clicked", async () => {
    const user = userEvent.setup();
    render(
      <Sidebar>
        <div>Content</div>
      </Sidebar>
    );
    await user.click(screen.getByRole("button", { name: "Toggle panel" }));
    expect(
      screen.getByRole("button", { name: "Toggle panel" })
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("renders children", () => {
    render(
      <Sidebar>
        <div>Item 1</div>
        <div>Item 2</div>
      </Sidebar>
    );
    expect(screen.getByText("Item 1")).toBeInTheDocument();
    expect(screen.getByText("Item 2")).toBeInTheDocument();
  });

  it("forwards className to the panel", () => {
    render(
      <Sidebar className="custom-class">
        <div>Content</div>
      </Sidebar>
    );
    expect(screen.getByRole("complementary")).toHaveClass("custom-class");
  });

  it("forwards extra props to the panel", () => {
    render(
      <Sidebar data-testid="my-sidebar">
        <div>Content</div>
      </Sidebar>
    );
    expect(screen.getByTestId("my-sidebar")).toBeInTheDocument();
  });
});

describe("PanelNavLink", () => {
  it("renders as a link with the correct href", () => {
    render(
      <Sidebar>
        <PanelNavLink to="/about">About</PanelNavLink>
      </Sidebar>
    );
    expect(screen.getByRole("link", { name: "About" })).toHaveAttribute(
      "href",
      "/about"
    );
  });

  it("renders multiple nav items", () => {
    render(
      <Sidebar>
        <PanelNavLink to="/">Home</PanelNavLink>
        <PanelNavLink to="/about">About</PanelNavLink>
      </Sidebar>
    );
    expect(screen.getByRole("link", { name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "About" })).toBeInTheDocument();
  });

  it("renders a start icon", () => {
    render(
      <Sidebar>
        <PanelNavLink to="/" startIcon={<span data-testid="nav-icon" />}>
          Home
        </PanelNavLink>
      </Sidebar>
    );
    expect(screen.getByTestId("nav-icon")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Home" })).toBeInTheDocument();
  });

  it("remains accessible after sidebar collapses", async () => {
    const user = userEvent.setup();
    render(
      <Sidebar>
        <PanelNavLink to="/">Home</PanelNavLink>
      </Sidebar>
    );
    await user.click(screen.getByRole("button", { name: "Toggle panel" }));
    // Collapsed — link is still in the DOM and accessible
    expect(screen.getByRole("link", { name: "Home" })).toBeInTheDocument();
  });
});
