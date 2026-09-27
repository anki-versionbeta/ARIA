import {
  ExperimentalSelect,
  type ExperimentalSelectOption,
  Icon,
  type IconProps,
  type Mode,
  ModeValues,
} from "@abbvie-unity/react";

const modes: Record<Mode, { label: string; icon: IconProps["icon"] }> = {
  light: { label: "Light", icon: "sun-bright" },
  dark: { label: "Dark", icon: "moon" },
  system: { label: "System", icon: "display" },
};

const options: ExperimentalSelectOption[] = ModeValues.map((mode) => ({
  value: mode,
  label: modes[mode].label,
  children: (
    <span className="flex items-center gap-2">
      <Icon fixedWidth icon={modes[mode].icon} />
      {modes[mode].label}
    </span>
  ),
}));

interface ModeSelectProps {
  mode: Mode;
  setMode: (mode: Mode) => void;
}

/**
 * Dropdown for switching the app's color mode (light, dark, system).
 * Uses ExperimentalSelect (listbox/option pattern) instead of Menu (command pattern).
 */
export function ModeSelect({ mode, setMode }: ModeSelectProps) {
  return (
    // Ability to hide dropdown arrow
    <ExperimentalSelect
      // Temporary focus classes until Unity updated the component
      className="min-w-0 max-w-fit outline-focus aria-expanded:outline-1 aria-expanded:[--border:var(--un-focus-color)]"
      value={mode}
      onChange={(val) => setMode(val as Mode)}
      options={options}
      aria-label="Change UI color mode"
      renderValue={(val) => (
        <span className="flex items-center gap-2">
          <Icon fixedWidth icon={modes[val as Mode].icon} />
          {modes[val as Mode].label}
        </span>
      )}
    />
  );
}
