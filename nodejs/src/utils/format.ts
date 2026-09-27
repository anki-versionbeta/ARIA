export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatDateTime(value: string): string {
  return dateTime.format(new Date(value));
}

/**
 * Turn a stage or dotted section key into something readable:
 * `operating_procedure.setup.equipment` -> "Setup › Equipment".
 */
export function humanizeKey(key: string): string {
  return (
    key.split(".").slice(1).map(humanizeSegment).join(" › ") ||
    humanizeSegment(key)
  );
}

export function humanizeSegment(segment: string): string {
  return segment
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
