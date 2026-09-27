import {
  Caption,
  Column,
  Footer,
  Grid,
  type HeaderProps,
  Logo,
  Spinner,
  useUnityTheme,
} from "@abbvie-unity/react";
import { createFileRoute, Outlet, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { useCurrentUser } from "@/api/queries";
import { Header } from "@/components/header/header";
import { ModeSelect } from "@/components/mode-select/mode-select";
import { AppSidebar } from "@/shell/app-sidebar";
import { RequestBell } from "@/shell/request-bell";
import { UserMenu } from "@/shell/user-menu";
import { cn } from "@/utils/cn";

export const Route = createFileRoute("/_app")({
  component: AppLayout,
});

function AppLayout() {
  const { mode, setMode } = useUnityTheme();
  const navigate = useNavigate();
  const { data: user, isPending, isError } = useCurrentUser();

  // The session is an httpOnly cookie, so "not logged in" is only observable as
  // a failed /auth/me rather than by inspecting a token.
  useEffect(() => {
    if (isError) navigate({ to: "/login", replace: true });
  }, [isError, navigate]);

  if (isPending) {
    return (
      <div className="flex h-dvh items-center justify-center">
        <Spinner className="h-9" />
      </div>
    );
  }

  if (!user) return null;

  return (
    <div>
      <div className="flex h-dvh flex-col">
        <AppHeader>
          <ModeSelect mode={mode} setMode={setMode} />
          <RequestBell user={user} />
          <UserMenu user={user} />
        </AppHeader>

        <div className="flex max-h-[calc(100dvh-var(--header-height))] flex-1 overflow-hidden">
          <AppSidebar />

          <main className="w-full overflow-y-auto">
            <Grid className="flex-1 overflow-auto py-8">
              <Column span="100%">
                <Outlet />
              </Column>
            </Grid>
          </main>
        </div>
      </div>

      <AppFooter />
    </div>
  );
}

const AppHeader = ({ className, ...props }: HeaderProps) => (
  // The layout depends on --header-height for alignment; changing it breaks
  // the max-height calculation on the row below.
  <Header className={cn("h-(--header-height)", className)} {...props} />
);

const AppFooter = () => (
  <Footer position="static" contentFullWidth>
    <Logo />
    <Caption className="ml-auto">
      Copyright © {new Date().getFullYear()} AbbVie Inc.
    </Caption>
  </Footer>
);
