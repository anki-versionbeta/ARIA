import {
  Alert,
  Button,
  Card,
  CardBody,
  Dropzone,
  H2,
  Icon,
  P,
} from "@abbvie-unity/react";
import { useState } from "react";
import { ApiError } from "@/api/http";
import { formatBytes } from "@/utils/format";

function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 413) return "That file is too large.";
    if (error.status === 415)
      return "That file type is not accepted for this document type.";
  }
  return "The upload failed. Please try again.";
}

type Props = {
  /** Extensions the silo accepts, e.g. [".pdf", ".docx"]. */
  accepts: string[];
  isUploading: boolean;
  error: unknown;
  onUpload: (file: File) => void;
};

/**
 * Shared across silos: the accepted extensions come from the silo's own contract, so
 * there is no per-silo upload code.
 */
export function UploadPanel({ accepts, isUploading, error, onUpload }: Props) {
  const [selected, setSelected] = useState<File | null>(null);
  const [rejected, setRejected] = useState<string | null>(null);

  const accept =
    accepts.length > 0 ? { "application/octet-stream": accepts } : undefined;

  return (
    <Card>
      <CardBody className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <H2 styledAs="h4">Upload a source document</H2>
          <P className="text-muted">
            {accepts.length > 0
              ? `Accepted: ${accepts.join(", ")}`
              : "Any file type is accepted."}
          </P>
        </div>

        {error ? <Alert status="error">{errorMessage(error)}</Alert> : null}
        {rejected ? <Alert status="error">{rejected}</Alert> : null}

        <Dropzone
          accept={accept}
          maxFiles={1}
          disabled={isUploading}
          mainText="Drag a document here"
          onUpload={(files) => {
            setRejected(null);
            if (files[0]) setSelected(files[0]);
          }}
          onReject={() =>
            setRejected(
              `That file was rejected. Accepted: ${accepts.join(", ")}`
            )
          }
        />

        {selected ? (
          <div className="flex flex-wrap items-center gap-4">
            <Icon icon="file-lines" />
            <P>
              {selected.name} · {formatBytes(selected.size)}
            </P>
            <Button
              className="ml-auto"
              disabled={isUploading}
              onClick={() => onUpload(selected)}
            >
              {isUploading ? "Uploading…" : "Start authoring"}
            </Button>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
