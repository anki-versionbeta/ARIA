import { Button, Menu, MenuItem, ProfilePicture } from "@abbvie-unity/react";
import { useNavigate } from "@tanstack/react-router";
import { useLogout } from "@/api/queries";
import type { CurrentUser } from "@/api/types";

function initialsOf(displayName: string) {
  return displayName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

export function UserMenu({ user }: { user: CurrentUser }) {
  const navigate = useNavigate();
  const logout = useLogout();

  // Built as an array rather than inline conditionals: Menu's children are typed as
  // Element | Element[], so a `cond ? <MenuItem/> : null` branch does not type check.
  const items = [];

  // Admin-only, and hidden rather than disabled -- a menu item nobody else can use is
  // just clutter. The route and the API both re-check, so hiding it is only cosmetic.
  if (user.role === "admin") {
    items.push(
      <MenuItem
        key="users"
        startIcon="users-gear"
        onClick={() => navigate({ to: "/users" })}
      >
        User management
        {user.pending_access_requests > 0
          ? ` (${user.pending_access_requests})`
          : ""}
      </MenuItem>
    );
  }

  items.push(
    <MenuItem
      key="sign-out"
      startIcon="right-from-bracket"
      disabled={logout.isPending}
      onClick={() =>
        logout.mutate(undefined, {
          onSuccess: () => navigate({ to: "/login", replace: true }),
        })
      }
    >
      Sign out
    </MenuItem>
  );

  return (
    <Menu
      disclosure={
        <Button variant="tertiary">
          <ProfilePicture
            size="small"
            name={user.display_name}
            initials={initialsOf(user.display_name)}
          />
          {user.display_name}
        </Button>
      }
    >
      {items}
    </Menu>
  );
}
