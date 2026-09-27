/** Mirrors the API surface in design spec sections 6 and 7. */

export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_user"
  | "complete"
  | "failed";

export const RUN_STATUSES: RunStatus[] = [
  "queued",
  "running",
  "awaiting_user",
  "complete",
  "failed",
];

export type DocumentSort = "date" | "name" | "user";
export type SortOrder = "asc" | "desc";

export type Role = "admin" | "super_user" | "user";

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Admin",
  super_user: "Super user",
  user: "User",
};

export type CurrentUser = {
  id: string;
  username: string;
  display_name: string;
  email: string | null;
  role: Role;
  /** Silo ids this user may reach. Advisory: the API re-checks every request. */
  modules: string[];
};

export type Module = {
  id: string;
  label: string;
};

export type AdminUser = {
  id: string;
  username: string;
  display_name: string;
  email: string | null;
  role: Role;
  modules: string[];
  /** True when `modules` is implied by the role rather than granted individually. */
  modules_from_role: boolean;
  /** A fixed administrator: role and modules cannot be changed from this screen. */
  protected: boolean;
  last_seen: string;
};

export type UserRef = {
  id: string;
  username: string;
  display_name: string;
};

export type Silo = {
  id: string;
  label: string;
  /** Bare extensions, e.g. [".pdf"] — passed straight to the shared Dropzone. */
  accepts: string[];
  stages: string[];
};

export type RunFile = {
  id: string;
  kind: "input" | "output";
  filename: string;
  size_bytes: number;
  content_type: string | null;
  created_at: string;
};

export type DocumentSummary = {
  id: string;
  silo_id: string;
  title: string;
  status: RunStatus;
  stage: string | null;
  progress_pct: number;
  progress_message: string | null;
  owner: UserRef;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
};

export type DocumentDetail = DocumentSummary & {
  error_message: string | null;
  files: RunFile[];
  forked_from_run_id: string | null;
  /**
   * Resolved server-side from ownership (D14) so the authorization rule has a
   * single source of truth rather than being re-derived in the client.
   */
  can_edit: boolean;
};

export type Paginated<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
};

export type DocumentQuery = {
  user?: string;
  silo?: string;
  status?: RunStatus;
  q?: string;
  sort?: DocumentSort;
  order?: SortOrder;
  page?: number;
};

/** Light payload for the 2s poll. */
export type DocumentStatus = {
  id: string;
  status: RunStatus;
  stage: string | null;
  progress_pct: number;
  progress_message: string | null;
  error_message: string | null;
  finished_at: string | null;
};

export type Section = {
  section_key: string;
  content_html: string;
  revision: number;
  updated_at: string;
};

export type SectionSaved = {
  section_key: string;
  revision: number;
  version_num: number;
  label: string | null;
};

export type SectionVersion = {
  version_num: number;
  label: string | null;
  created_at: string;
};

export type SectionVersionDetail = SectionVersion & {
  content_html: string;
};

/** Body of the 403 returned when a non-owner attempts an edit (D14/D15). */
export type ForbiddenWithFork = {
  can_fork: true;
};

/** Body of the 409 returned when the edited copy is out of date. */
export type StaleRevision = {
  message: string;
  current_revision: number;
};
