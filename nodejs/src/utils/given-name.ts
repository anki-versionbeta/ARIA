/**
 * The name to greet someone by, from an Active Directory display name.
 *
 * AD here returns "Surname, Firstname", so the given name is after the comma — taking the
 * first word instead greets people by their surname with a trailing comma. Plain
 * "Firstname Surname" is also handled, because the directory is not guaranteed to be
 * consistent and the seeded dev users use that form.
 *
 * Returns "" when there is nothing usable, so the caller can drop the name rather than
 * greet someone by a login id: display_name falls back to the username when the directory
 * read fails.
 */
export function givenName(displayName: string | undefined | null): string {
  const name = displayName?.trim();
  if (!name) return "";

  const [beforeComma, afterComma] = name.split(",", 2);
  const chosen = afterComma?.trim() ? afterComma : beforeComma;
  return chosen.trim().split(/\s+/)[0] ?? "";
}
