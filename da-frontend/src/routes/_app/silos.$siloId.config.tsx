import { Alert } from "@abbvie-unity/react";
import { createFileRoute } from "@tanstack/react-router";
import { getSiloModule } from "@/silos/registry";

export const Route = createFileRoute("/_app/silos/$siloId/config")({
  component: SiloConfigPage,
});

function SiloConfigPage() {
  const { siloId } = Route.useParams();
  const Config = getSiloModule(siloId)?.screens?.config;

  if (!Config) {
    return <Alert status="info">This document type has no settings.</Alert>;
  }
  return <Config />;
}
