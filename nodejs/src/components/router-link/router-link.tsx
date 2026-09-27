import { Link } from "@abbvie-unity/react";
import { createLink, type LinkComponent } from "@tanstack/react-router";

const LinkLinkComponent = createLink(Link);

/**
 * `RouterLink` is a Unity `Link` integrated with TanStack Router.
 * Use this for inline text links that navigate to internal routes.
 * Accepts all Unity `Link` props plus TanStack Router `Link` props (e.g. `to`, `params`, `search`).
 */
export const RouterLink: LinkComponent<typeof Link> = (props) => {
  return <LinkLinkComponent preload={"intent"} {...props} />;
};
