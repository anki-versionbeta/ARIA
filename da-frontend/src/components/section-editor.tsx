import {
  Alert,
  Button,
  Caption,
  Card,
  CardBody,
  Field,
  H2,
  P,
  Select,
  Spinner,
} from "@abbvie-unity/react";
import { useEffect, useState } from "react";
import {
  isForbidden,
  isStaleRevision,
  useRestoreVersion,
  useSaveSection,
  useSections,
  useVersions,
} from "@/api/queries";
import { RichTextEditor } from "@/components/rich-text-editor";
import { SectionTree } from "@/components/section-tree";
import { formatDateTime, humanizeSegment } from "@/utils/format";

type Props = {
  documentId: string;
  canEdit: boolean;
};

/**
 * Section editing with version history. Platform-level, not silo-level: any silo that
 * produces sections gets this screen for free.
 */
export function SectionEditor({ documentId, canEdit }: Props) {
  const { data: sections, isPending } = useSections(documentId);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [dirty, setDirty] = useState(false);

  const save = useSaveSection(documentId);
  const restore = useRestoreVersion(documentId);
  const { data: versions } = useVersions(documentId, selectedKey);

  // Select the first section once they arrive.
  useEffect(() => {
    if (!selectedKey && sections && sections.length > 0) {
      setSelectedKey(sections[0].section_key);
    }
  }, [sections, selectedKey]);

  const current =
    sections?.find((item) => item.section_key === selectedKey) ?? null;

  // Reset the draft when the selected section or its stored content changes.
  useEffect(() => {
    setDraft(current?.content_html ?? "");
    setDirty(false);
  }, [current?.content_html]);

  if (isPending) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-9" />
      </div>
    );
  }

  if (!sections || sections.length === 0) {
    return (
      <Alert status="info">This document has no editable sections yet.</Alert>
    );
  }

  return (
    <div className="flex large:flex-row flex-col gap-4">
      <Card className="large:min-w-72 large:max-w-80">
        <CardBody>
          <H2 styledAs="h5" className="mb-2">
            Sections
          </H2>
          <SectionTree
            sections={sections}
            selected={selectedKey}
            onSelect={(key) => setSelectedKey(key)}
          />
        </CardBody>
      </Card>

      <Card className="flex-1">
        <CardBody className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-col">
              <H2 styledAs="h5">
                {selectedKey
                  ? humanizeSegment(selectedKey.split(".").pop() ?? "")
                  : ""}
              </H2>
              <Caption className="text-muted">
                {selectedKey}
                {current ? ` · revision ${current.revision}` : ""}
              </Caption>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              {versions && versions.length > 0 ? (
                <Field aria-label="Version history" block>
                  <Select
                    className="medium:min-w-56 min-w-0"
                    placeholder={`${versions.length} version${versions.length === 1 ? "" : "s"}`}
                    options={versions.map((version) => ({
                      value: String(version.version_num),
                      label: `v${version.version_num} · ${version.label ?? ""} · ${formatDateTime(version.created_at)}`,
                    }))}
                    disabled={!canEdit || restore.isPending}
                    onChange={(value) => {
                      if (!selectedKey) return;
                      restore.mutate({
                        sectionKey: selectedKey,
                        versionNum: Number(value),
                      });
                    }}
                  />
                </Field>
              ) : null}

              <Button
                disabled={!canEdit || !dirty || save.isPending || !current}
                onClick={() => {
                  if (!selectedKey || !current) return;
                  save.mutate(
                    {
                      sectionKey: selectedKey,
                      html: draft,
                      revision: current.revision,
                    },
                    { onSuccess: () => setDirty(false) }
                  );
                }}
              >
                {save.isPending ? "Saving…" : dirty ? "Save" : "Saved"}
              </Button>
            </div>
          </div>

          {save.isError ? (
            <Alert status="error">
              {isStaleRevision(save.error)
                ? "This section changed since you loaded it. Reload to see the newer version before saving again."
                : isForbidden(save.error)
                  ? "You do not own this document, so it cannot be edited."
                  : "Saving failed. Please try again."}
            </Alert>
          ) : null}

          {restore.isError ? (
            <Alert status="error">Restoring that version failed.</Alert>
          ) : null}

          {canEdit ? null : (
            <P className="text-muted">
              Read-only — make your own copy to edit this document.
            </P>
          )}

          {current ? (
            <RichTextEditor
              key={`${current.section_key}:${current.revision}`}
              initialHtml={current.content_html}
              readOnly={!canEdit}
              onChange={(html) => {
                setDraft(html);
                setDirty(html !== current.content_html);
              }}
            />
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}
