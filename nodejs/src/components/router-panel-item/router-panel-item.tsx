import { PanelItem } from "@abbvie-unity/react";
import { createLink, type LinkComponent } from "@tanstack/react-router";

const PanelItemLinkComponent = createLink(PanelItem);

/**
 * `RouterPanelItem` is a Unity `PanelItem` integrated with TanStack Router.
 * Use this for sidebar/panel navigation items that navigate to internal routes.
 * Accepts all Unity `PanelItem` props plus TanStack Router `Link` props (e.g. `to`, `params`, `activeProps`).
 */
export const RouterPanelItem: LinkComponent<typeof PanelItem> = (props) => {
  return <PanelItemLinkComponent preload="intent" {...props} />;
};
