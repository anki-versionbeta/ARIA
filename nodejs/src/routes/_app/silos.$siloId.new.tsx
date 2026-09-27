import { Alert, H1, P, Spinner } from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCreateDocument, useSilos } from "@/api/queries";
import { UploadPanel } from "@/components/upload-panel";
import { getSiloModule } from "@/silos/registry";

export const Route = createFileRoute("/_app/silos/$siloId/new")({
  component: NewDocumentPage,
});

function NewDocumentPage() {
  const { siloId } = Route.useParams();
  const navigate = useNavigate();
  const { data: silos, isPending } = useSilos();
  const create = useCreateDocument(siloId);

  if (isPending) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-9" />
      </div>
    );
  }

  const silo = silos?.find((item) => item.id === siloId);
  if (!silo) {
    // Either an unknown id or a silo shipped dark via ENABLED_SILOS.
    return (
      <Alert status="error">
        "{siloId}" is not an available document type.
      </Alert>
    );
  }

  // A silo may replace the shared upload screen; most do not need to.
  const Custom = getSiloModule(siloId)?.screens?.upload;
  if (Custom) return <Custom siloId={siloId} />;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <H1 styledAs="h2">New {silo.label}</H1>
        <P className="text-muted">
          Upload the source document. Generation runs in the background, so you
          can leave this page and come back to it.
        </P>
      </div>

      <UploadPanel
        accepts={silo.accepts}
        isUploading={create.isPending}
        error={create.isError ? create.error : null}
        onUpload={(file) =>
          create.mutate(file, {
            onSuccess: ({ id }) =>
              navigate({
                to: "/documents/$documentId",
                params: { documentId: id },
              }),
          })
        }
      />
    </div>
  );
}
