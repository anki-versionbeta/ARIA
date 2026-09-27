import {
  Button,
  Card,
  CardBody,
  H1,
  H2,
  P,
  Subhead1,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCurrentUser } from "@/api/queries";
import { RouterLink } from "@/components/router-link/router-link";
import { givenName } from "@/utils/given-name";

export const Route = createFileRoute("/_app/")({
  component: HomePage,
});

/**
 * Where signing in lands you, with the sidebar collapsed. It has to answer "what is this?"
 * for someone seeing ARIA for the first time, then get out of the way.
 *
 * Get Started goes to /new, which is the one place that offers the document types. Naming
 * them here as well would be a second list to keep correct.
 */
function HomePage() {
  const { data: user } = useCurrentUser();
  const navigate = useNavigate();
  const firstName = givenName(user?.display_name);

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        <H1 styledAs="h2">{firstName ? `Welcome, ${firstName}` : "Welcome"}</H1>
        <Subhead1>
          ARIA — AI-Driven Report Intelligence &amp; Automation
        </Subhead1>
        <P className="max-w-[70ch] text-muted">
          ARIA reads your source documents, then drafts the regulatory and
          manufacturing reports. You review the judgement calls; ARIA does the
          transcription, the tables and the paperwork.
        </P>
      </div>

      <div className="grid grid-cols-1 medium:grid-cols-3 gap-4">
        <Capability
          title="Reads the source"
          body="Uploaded source files and data from CMC Data Warehouse"
        />
        <Capability
          title="Drafts the document"
          body="Sections, tables and figures assembled into a template."
        />
        <Capability
          title="Keeps you in control"
          body="Anything uncertain is flagged for review."
        />
      </div>

      <Card>
        <CardBody className="flex flex-col items-start gap-4">
          <H2 styledAs="h4">Ready when you are</H2>
          <P className="max-w-[70ch] text-muted">
            Pick a document type and ARIA takes it from there.
          </P>
          <Button
            endIcon={["fas", "arrow-right"]}
            onClick={() => navigate({ to: "/new" })}
          >
            Get Started
          </Button>
        </CardBody>
      </Card>

      <P className="text-muted">
        Looking for something you authored earlier?{" "}
        <RouterLink to="/history">Browse the document history</RouterLink>.
      </P>
    </div>
  );
}

/**
 * The type scale every page in the app follows, so a heading is the same size wherever you
 * meet it:
 *
 *     H1 styledAs="h2"   page title
 *     Subhead1           the ARIA tagline, and nothing else — it is a brand line
 *     H2 styledAs="h4"   every card title, which is what the rest of the app already used
 *     P + text-muted     all body and lead copy
 *
 * No icon here on purpose: Unity carries FontAwesome as a devDependency, so only the glyphs
 * it bundles resolve by name and the rest render blank. Not worth risking three of them on a
 * page whose whole job is to make a good first impression.
 */
function Capability({ title, body }: { title: string; body: string }) {
  return (
    <Card>
      <CardBody className="flex flex-col gap-2">
        <H2 styledAs="h4">{title}</H2>
        <P className="text-muted">{body}</P>
      </CardBody>
    </Card>
  );
}
