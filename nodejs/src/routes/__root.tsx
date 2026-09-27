import { createRootRoute, Outlet } from "@tanstack/react-router";

/**
 * Deliberately bare. The app chrome lives in the `_app` layout route so /login
 * can render without a header, sidebar, or footer.
 */
export const Route = createRootRoute({
  component: () => <Outlet />,
});
