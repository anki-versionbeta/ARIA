import { Button, Tooltip } from "@abbvie-unity/react";
import { useNavigate } from "@tanstack/react-router";
import type { CurrentUser } from "@/api/types";

/**
 * Pending access requests, in the header where an admin sees them without going looking.
 *
 * One Button and nothing else: a pill with the bell and the count as its label. The
 * overlaid badge this replaced was positioned by hand and coloured with `bg-red-600` and
 * `text-white`, neither of which exists here -- styles.css loads Unity's tokens plus
 * `tailwindcss/utilities.css`, not Tailwind's default palette, so those two classes
 * resolved to nothing and left an unstyled black number floating off the corner of the
 * icon. Letting the component draw the whole control means the fill, radius, spacing,
 * focus ring and dark mode all come from the design system, and there is no geometry left
 * for me to get wrong.
 *
 * Renders nothing when the queue is empty. A bell that is always present is furniture, and
 * furniture is not noticed on the day it matters.
 */
export function RequestBell({ user }: { user: CurrentUser }) {
  const navigate = useNavigate();
  const count = user.pending_access_requests;

  if (user.role !== "admin" || count < 1) return null;

  const label = `${count} access request${count === 1 ? "" : "s"} waiting`;

  return (
    <Tooltip content={label}>
      <Button
        variant="primary"
        shape="pill"
        startIcon="bell"
        aria-label={label}
        onClick={() => navigate({ to: "/users" })}
      >
        {count > 99 ? "99+" : count}
      </Button>
    </Tooltip>
  );
}
