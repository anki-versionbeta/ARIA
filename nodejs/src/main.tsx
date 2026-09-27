import { Spinner, UnityProvider } from "@abbvie-unity/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import ReactDOM from "react-dom/client";
import { setUnauthorizedHandler } from "@/api/http";
import { loadRuntimeConfig } from "@/config/runtime";
import { routeTree } from "./routeTree.gen";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, staleTime: 30 * 1000 },
  },
});

const router = createRouter({
  routeTree,
  context: {},
  scrollRestoration: true,
  scrollRestorationBehavior: "instant",
  scrollToTopSelectors: ["main"],
  defaultPendingComponent: () => <Spinner className="h-9" />,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

setUnauthorizedHandler(() => {
  queryClient.clear();
  router.navigate({ to: "/login", replace: true });
});

async function bootstrap() {
  // Must resolve before the router mounts: the API base URL is read from a file
  // at boot so that one built image can serve every environment (D21).
  await loadRuntimeConfig();

  const rootElement = document.getElementById("app");
  if (!rootElement || rootElement.innerHTML) return;

  ReactDOM.createRoot(rootElement).render(
    <StrictMode>
      <UnityProvider
        defaultMode="system"
        density="balanced"
        withRootStyles
        storageKey="da-platform-ui-theme"
      >
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </UnityProvider>
    </StrictMode>
  );
}

bootstrap().catch((error: unknown) => {
  const rootElement = document.getElementById("app");
  if (rootElement) {
    rootElement.textContent =
      error instanceof Error
        ? `Application configuration failed to load: ${error.message}`
        : "Application configuration failed to load.";
  }
});
