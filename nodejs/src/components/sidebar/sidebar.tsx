import {
  Panel,
  PanelContext,
  type PanelItem,
  type PanelProps,
  PanelToggle,
  Role,
  Tooltip,
  usePanelState,
} from "@abbvie-unity/react";
import { useContext } from "react";
import { cn } from "@/utils/cn";
import { RouterPanelItem } from "../router-panel-item/router-panel-item";

/**
 * `Sidebar` is a collapsible left-side panel. `PanelNavLink` is available for internal route navigation links.
 *
 * `defaultExpanded` seeds the initial state only — the toggle owns it from then on. It
 * defaults to expanded, which is the Unity template's behaviour.
 */
export function Sidebar({
  className,
  children,
  defaultExpanded = true,
  ...props
}: PanelProps & { defaultExpanded?: boolean }) {
  const panel = usePanelState({ side: "left", expanded: defaultExpanded });

  const widthClasses = {
    collapsed: "",
    expanded: "w-[300px] min-w-[300px] max-w-[300px]",
  };

  return (
    <Panel
      render={<aside />}
      expanded={panel.expanded}
      state={panel}
      className={cn(
        "shrink-0 overflow-y-auto overscroll-contain",
        widthClasses[panel.expanded ? "expanded" : "collapsed"],
        className
      )}
      {...props}
    >
      <PanelToggle className="mb-2" aria-label="Toggle panel" />
      {children}
    </Panel>
  );
}

/**
 * A router-aware sidebar nav item built on `RouterPanelItem`.
 * Automatically marks itself active when its route is current.
 * Shows a tooltip with the item label when the sidebar is collapsed.
 * Use this as the standard building block for sidebar navigation entries.
 */
export const PanelNavLink = ({
  children,
  startIcon,
  to,
  ...props
}: React.ComponentProps<typeof PanelItem> & {
  to: string;
  // Dynamic routes such as /silos/$siloId/config need their params.
  params?: Record<string, string>;
}) => {
  const panel = useContext(PanelContext);
  return (
    <Tooltip
      placement="right"
      showTimeout={1000}
      content={!panel?.expanded ? children : undefined}
    >
      <RouterPanelItem
        to={to}
        activeProps={{ active: true }}
        startIcon={startIcon}
        render={<Role.a />}
        {...props}
      >
        {children}
      </RouterPanelItem>
    </Tooltip>
  );
};
