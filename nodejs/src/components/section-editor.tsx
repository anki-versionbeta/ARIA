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
  useSaveSection,
  useSections,
  useVersion,
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

  // Which version is loaded into the editor, or null for the live revision. Picking one
  // is a read — it loads the old text to edit, and saving is what writes a new version.
  const [pickedNum, setPickedNum] = useState<number | null>(null);

  const save = useSaveSection(documentId);
  const { data: versions } = useVersions(documentId, selectedKey);
  const { data: picked } = useVersion(documentId, selectedKey, pickedNum);

  // Select the first section once they arrive.
  useEffect(() => {
    if (!selectedKey && sections && sections.length > 0) {
      setSelectedKey(sections[0].section_key);
    }
  }, [sections, selectedKey]);

  const current =
    sections?.find((item) => item.section_key === selectedKey) ?? null;

  // Reset the draft when the selected section or its stored content changes. A picked
  // version owns the draft while it is loaded, so this stands aside for it.
  useEffect(() => {
    if (pickedNum !== null) return;
    setDraft(current?.content_html ?? "");
    setDirty(false);
  }, [current?.content_html, pickedNum]);

  // The body of the picked version, or undefined when the live text is showing. One
  // value rather than a (number, object) pair, so the effect below depends on exactly
  // what it reads.
  const pickedHtml = pickedNum === null ? undefined : picked?.content_html;

  // An older version's text is an unsaved edit: it differs from what is stored, so Save
  // is live immediately and committing it appends a version through the normal path.
  useEffect(() => {
    if (pickedHtml === undefined) return;
    setDraft(pickedHtml);
    setDirty(pickedHtml !== (current?.content_html ?? ""));
  }, [pickedHtml, current?.content_html]);

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
          {/* The list scrolls inside the card so a long one cannot push the editor
              below the fold. Capped only from `large` up, where the two panels sit
              side by side; stacked on narrow screens the page scroll is correct. */}
          <div className="large:max-h-[60vh] overflow-y-auto overscroll-contain">
            <SectionTree
              sections={sections}
              selected={selectedKey}
              // A picked version belongs to the section it came from, so changing
              // section drops it rather than showing one section's history over
              // another's text.
              onSelect={(key) => {
                setSelectedKey(key);
                setPickedNum(null);
              }}
            />
          </div>
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
                    onChange={(value) => setPickedNum(Number(value))}
                  />
                </Field>
              ) : null}

              <Button
                disabled={!canEdit || !dirty || save.isPending || !current}
                onClick={() => {
                  if (!selectedKey || !current) return;
                  // `current.revision` even when an older version is loaded: the write
                  // lands on top of the live revision, which is what the server checks.
                  save.mutate(
                    {
                      sectionKey: selectedKey,
                      html: draft,
                      revision: current.revision,
                    },
                    {
                      onSuccess: () => {
                        setDirty(false);
                        setPickedNum(null);
                      },
                    }
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

          {canEdit ? null : (
            <P className="text-muted">
              Read-only — make your own copy to edit this document.
            </P>
          )}

          {pickedNum !== null && pickedHtml === undefined ? (
            <div className="flex justify-center py-12">
              <Spinner className="h-9" />
            </div>
          ) : current ? (
            <RichTextEditor
              // Remounts when the loaded body changes, because Quill only reads
              // `initialHtml` on mount.
              key={
                pickedNum !== null
                  ? `${selectedKey}:v${pickedNum}`
                  : `${current.section_key}:r${current.revision}`
              }
              initialHtml={pickedHtml ?? current.content_html}
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
