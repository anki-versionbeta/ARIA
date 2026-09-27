import { PanelSection } from "@abbvie-unity/react";
import { useSilos } from "@/api/queries";
import { PanelNavLink, Sidebar } from "@/components/sidebar/sidebar";
import { useCurrentSiloId } from "@/shell/use-current-silo";
import { getSiloModule } from "@/silos/registry";

export function AppSidebar() {
  return (
    // Starts collapsed so signing in shows the Home page rather than a wall of chrome.
    <Sidebar defaultExpanded={false}>
      <nav>
        {/* No section title: with three self-explanatory links it only added a heading to
            read past. The per-silo settings section below still names its silo, because
            there the grouping is not obvious. */}
        <PanelSection>
          <PanelNavLink to="/" startIcon={["fas", "house"]}>
            Home
          </PanelNavLink>
          {/* Replaces the type dropdown that used to sit at the foot of this panel. A link
              keeps starting a document reachable from any page, as the dropdown was, while
              the choice itself lives on one page with room to say what each type needs. */}
          <PanelNavLink to="/new" startIcon={["fas", "plus"]}>
            New document
          </PanelNavLink>
          <PanelNavLink to="/history" startIcon={["fas", "clock-rotate-left"]}>
            History
          </PanelNavLink>
        </PanelSection>
        <SiloSettingsLinks />
      </nav>
    </Sidebar>
  );
}

/**
 * Settings for the silo you are currently in — not for every silo that has them.
 * On the history list there is no silo context, so this section is absent.
 */
function SiloSettingsLinks() {
  const siloId = useCurrentSiloId();
  const { data: silos } = useSilos();

  if (!siloId) return null;
  // Only offered if the silo is actually available and contributes a settings screen.
  const silo = silos?.find((item) => item.id === siloId);
  if (!silo || !getSiloModule(siloId)?.screens?.config) return null;

  return (
    <PanelSection title={silo.label}>
      <PanelNavLink
        to="/silos/$siloId/config"
        params={{ siloId }}
        startIcon={["fas", "sliders"]}
      >
        Prompts
      </PanelNavLink>
    </PanelSection>
  );
}
