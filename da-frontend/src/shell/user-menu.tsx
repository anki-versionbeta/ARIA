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
      <MenuItem
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
    </Menu>
  );
}
