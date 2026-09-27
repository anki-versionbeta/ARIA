import { PanelItem, PanelSection } from "@abbvie-unity/react";
import type { Section } from "@/api/types";
import { humanizeSegment } from "@/utils/format";

type Props = {
  sections: Section[];
  selected: string | null;
  onSelect: (sectionKey: string) => void;
};

/**
 * Groups dotted section keys by their first segment, so
 * `operating_procedure.setup.equipment` appears under "Operating Procedure".
 * Generic: any silo whose keys are dotted gets a tree without writing one.
 */
export function SectionTree({ sections, selected, onSelect }: Props) {
  const groups = new Map<string, Section[]>();
  for (const section of sections) {
    const [head] = section.section_key.split(".");
    const bucket = groups.get(head);
    if (bucket) bucket.push(section);
    else groups.set(head, [section]);
  }

  return (
    <nav className="flex flex-col gap-2">
      {[...groups.entries()].map(([group, items]) => (
        <PanelSection key={group} title={humanizeSegment(group)}>
          {items.map((section) => (
            <PanelItem
              key={section.section_key}
              active={section.section_key === selected}
              onClick={() => onSelect(section.section_key)}
              // A section still at revision 1 is exactly as generated.
              endIcon={section.revision > 1 ? "pen" : undefined}
            >
              {leafLabel(section.section_key)}
            </PanelItem>
          ))}
        </PanelSection>
      ))}
    </nav>
  );
}

function leafLabel(key: string): string {
  const parts = key.split(".");
  if (parts.length === 1) return humanizeSegment(parts[0]);
  return parts.slice(1).map(humanizeSegment).join(" › ");
}
