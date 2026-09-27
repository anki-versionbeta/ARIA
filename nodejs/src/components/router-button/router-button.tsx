import { Role, Button as UnityButton } from "@abbvie-unity/react";
import { createLink, type LinkComponent } from "@tanstack/react-router";

function Button(props: React.ComponentProps<typeof UnityButton>) {
  return <UnityButton render={<Role.a />} {...props} />;
}

const LinkButtonComponent = createLink(Button);

/**
 * `RouterButton` is a Unity `Button` integrated with TanStack Router.
 * Use this anywhere you need a styled button that navigates to an internal route.
 * Accepts all Unity `Button` props plus TanStack Router `Link` props (e.g. `to`, `params`, `search`).
 */
export const RouterButton: LinkComponent<typeof Button> = (props) => {
  return <LinkButtonComponent preload="intent" {...props} />;
};
