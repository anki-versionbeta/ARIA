import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ModeSelect } from "./mode-select";

describe("ModeSelect", () => {
  it("renders without crashing", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("displays the correct text for light mode", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);
    const combobox = screen.getByRole("combobox");
    expect(within(combobox).getByText("Light")).toBeInTheDocument();
  });

  it("displays the correct text for dark mode", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="dark" setMode={setMode} />);
    const combobox = screen.getByRole("combobox");
    expect(within(combobox).getByText("Dark")).toBeInTheDocument();
  });

  it("displays the correct text for system mode", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="system" setMode={setMode} />);
    const combobox = screen.getByRole("combobox");
    expect(within(combobox).getByText("System")).toBeInTheDocument();
  });

  it("has correct aria-label for accessibility", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);
    expect(
      screen.getByRole("combobox", { name: /change ui color mode/i })
    ).toBeInTheDocument();
  });

  it("opens listbox when combobox is clicked", async () => {
    const user = userEvent.setup();
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    await user.click(combobox);

    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("renders all three mode options in the listbox", async () => {
    const user = userEvent.setup();
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    await user.click(combobox);

    expect(screen.getByRole("option", { name: /light/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /dark/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /system/i })).toBeInTheDocument();
  });

  it("calls setMode with 'light' when Light option is clicked", async () => {
    const user = userEvent.setup();
    const setMode = vi.fn();
    render(<ModeSelect mode="dark" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    await user.click(combobox);

    const lightOption = screen.getByRole("option", { name: /light/i });
    await user.click(lightOption);

    expect(setMode).toHaveBeenCalledWith("light");
  });

  it("calls setMode with 'dark' when Dark option is clicked", async () => {
    const user = userEvent.setup();
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    await user.click(combobox);

    const darkOption = screen.getByRole("option", { name: /dark/i });
    await user.click(darkOption);

    expect(setMode).toHaveBeenCalledWith("dark");
  });

  it("calls setMode with 'system' when System option is clicked", async () => {
    const user = userEvent.setup();
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    await user.click(combobox);

    const systemOption = screen.getByRole("option", { name: /system/i });
    await user.click(systemOption);

    expect(setMode).toHaveBeenCalledWith("system");
  });

  it("updates display when mode prop changes", () => {
    const setMode = vi.fn();
    const { rerender } = render(<ModeSelect mode="light" setMode={setMode} />);

    const combobox = screen.getByRole("combobox");
    expect(within(combobox).getByText("Light")).toBeInTheDocument();

    rerender(<ModeSelect mode="dark" setMode={setMode} />);
    expect(within(combobox).getByText("Dark")).toBeInTheDocument();

    rerender(<ModeSelect mode="system" setMode={setMode} />);
    expect(within(combobox).getByText("System")).toBeInTheDocument();
  });

  it("does not call setMode on initial render", () => {
    const setMode = vi.fn();
    render(<ModeSelect mode="light" setMode={setMode} />);
    expect(setMode).not.toHaveBeenCalled();
  });
});
