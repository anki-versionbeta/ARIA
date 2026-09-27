import {
  Alert,
  Button,
  Card,
  CardBody,
  CardFooter,
  Field,
  H1,
  H2,
  Icon,
  P,
  Spinner,
  TextInput,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useSilos } from "@/api/queries";
import type { Silo } from "@/api/types";
import { matchesTypeSearch } from "@/utils/silo-search";

export const Route = createFileRoute("/_app/new")({
  component: NewDocumentPage,
});

/**
 * Choose what to author. This is the one place that offers the choice — it replaced a
 * dropdown in the sidebar, which was easy to miss and had no room to say what each type
 * actually takes as input.
 *
 * The list is GET /api/silos, so a silo shipped dark via ENABLED_SILOS never appears, and
 * adding a silo needs no change here.
 */
function NewDocumentPage() {
  const { data: silos, isPending, isError } = useSilos();
  const [query, setQuery] = useState("");

  const available = silos ?? [];
  const matches = available.filter((silo) => matchesTypeSearch(silo, query));
  // Nothing to search through with a single type, and the box would only be noise. It
  // appears on its own as more generators are added.
  const showSearch = available.length > 1;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <H1 styledAs="h2">What would you like to author?</H1>
        {/* Lead copy is P everywhere, as on Home — this was a Subhead1, which made the same
            kind of line bigger here than there. See the scale on the Home page. */}
        <P className="max-w-[70ch] text-muted">
          Pick a document type to get started.
        </P>
      </div>

      {showSearch ? (
        <Field aria-label="Search document types" block>
          <TextInput
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search document types"
            start={<Icon icon="magnifying-glass" />}
            className="medium:min-w-80 min-w-0"
          />
        </Field>
      ) : null}

      {isPending ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-9" />
        </div>
      ) : isError ? (
        <Alert status="error" subtitle="Reloading the page may recover it.">
          Could not load the available document types
        </Alert>
      ) : available.length === 0 ? (
        <Alert
          status="info"
          subtitle="Contact the platform team if you expected one here."
        >
          No document types are enabled right now
        </Alert>
      ) : matches.length === 0 ? (
        <Alert status="info" subtitle="Try a shorter or different term.">
          No document type matches “{query.trim()}”
        </Alert>
      ) : (
        <div className="grid grid-cols-1 large:grid-cols-3 medium:grid-cols-2 gap-4">
          {matches.map((silo) => (
            <SiloCard key={silo.id} silo={silo} />
          ))}
        </div>
      )}
    </div>
  );
}

/** What this type needs from you, which is the thing worth knowing before choosing. */
function inputSummary(silo: Silo): string {
  if (silo.accepts.length === 0) {
    return "Starts from an identifier — nothing to upload.";
  }
  return `Starts from ${silo.accepts.join(" or ")} you upload.`;
}

function SiloCard({ silo }: { silo: Silo }) {
  const navigate = useNavigate();

  return (
    // Column-flex so CardBody can absorb the spare height and CardFooter stays on the
    // card's bottom edge, which lines every Go button up across the row whatever the
    // label's length.
    <Card className="flex h-full flex-col">
      <CardBody className="flex flex-1 flex-col gap-2">
        {/* The app-wide card title — see the type scale on the Home page */}
        <H2 styledAs="h4">{silo.label}</H2>
        <P className="text-muted">{inputSummary(silo)}</P>
      </CardBody>

      {/* CardFooter is the component's own slot for actions — a button placed in the body
          with mt-auto sat on the card's edge and read as though it were outside it. */}
      <CardFooter>
        <Button
          endIcon={["fas", "arrow-right"]}
          aria-label={`Go — ${silo.label}`}
          onClick={() =>
            navigate({
              to: "/silos/$siloId/new",
              params: { siloId: silo.id },
            })
          }
        >
          Go
        </Button>
      </CardFooter>
    </Card>
  );
}
