import {
  Alert,
  Button,
  ButtonGroup,
  Caption,
  Card,
  CardBody,
  CardFooter,
  Checkbox,
  EmptyState,
  Field,
  H1,
  H2,
  PanelItem,
  PanelSection,
  ProfilePicture,
  Select,
  Spinner,
  Tag,
  TextInput,
  Tooltip,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiErrorMessage } from "@/api/http";
import {
  useAccessRequests,
  useAdminUsers,
  useApproveAccessRequest,
  useCurrentUser,
  useModules,
  useRejectAccessRequest,
  useSetUserModules,
  useSetUserRole,
} from "@/api/queries";
import { ROLE_LABELS, type Role } from "@/api/types";

export const Route = createFileRoute("/_app/users")({
  component: UsersPage,
});

const ROLE_OPTIONS = (Object.keys(ROLE_LABELS) as Role[]).map((role) => ({
  value: role,
  label: ROLE_LABELS[role],
}));

/** Sits under the Select, where it is read while deciding. */
const ROLE_HELP: Record<Role, string> = {
  admin: "Every module, and can manage users.",
  super_user: "Every module, automatically.",
  user: "Only the modules ticked below.",
};

const ROLE_TAG_KIND: Record<Role, "primary" | "secondary" | "neutral"> = {
  admin: "primary",
  super_user: "secondary",
  user: "neutral",
};

function initialsOf(displayName: string) {
  return displayName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function messageOf(error: unknown, fallback: string) {
  // The API sends a usable message for every case an admin can hit (fixed admin, last
  // admin, unknown role), so show that instead of "request failed".
  return apiErrorMessage(error) ?? fallback;
}

function sameMembers(a: string[], b: string[]) {
  return a.length === b.length && [...a].sort().join() === [...b].sort().join();
}

function UsersPage() {
  const navigate = useNavigate();
  const currentUser = useCurrentUser();
  const isAdmin = currentUser.data?.role === "admin";

  const users = useAdminUsers(isAdmin);
  const modules = useModules(isAdmin);
  const setRole = useSetUserRole();
  const setModules = useSetUserModules();

  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftRole, setDraftRole] = useState<Role | null>(null);
  const [draftModules, setDraftModules] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Non-admins are sent away rather than shown a locked page: this screen is not theirs
  // to look at. The menu entry is already hidden and the API refuses them, so this is the
  // third of three -- the only one that matters is the API.
  useEffect(() => {
    if (currentUser.data && currentUser.data.role !== "admin") {
      navigate({ to: "/", replace: true });
    }
  }, [currentUser.data, navigate]);

  const rows = useMemo(() => {
    const all = users.data ?? [];
    const term = search.trim().toLowerCase();
    if (!term) return all;
    return all.filter(
      (row) =>
        row.display_name.toLowerCase().includes(term) ||
        row.username.toLowerCase().includes(term)
    );
  }, [users.data, search]);

  const selected = useMemo(
    () => (users.data ?? []).find((row) => row.id === selectedId) ?? null,
    [users.data, selectedId]
  );

  // Select the first person on arrival, and drop a selection the search has hidden, so
  // the right-hand pane is never an empty frame.
  useEffect(() => {
    if (rows.length === 0) return;
    if (!selectedId || !rows.some((row) => row.id === selectedId)) {
      setSelectedId(rows[0].id);
    }
  }, [rows, selectedId]);

  // The form edits a draft, reset whenever the selection or the server row changes. That
  // is what makes Discard correct without tracking each field.
  useEffect(() => {
    setDraftRole(selected?.role ?? null);
    setDraftModules(selected ? [...selected.modules] : null);
    setError(null);
    setSaved(false);
  }, [selected?.id, selected?.role, selected?.modules, selected]);

  const effectiveRole = draftRole ?? selected?.role ?? "user";
  const dirty =
    selected !== null &&
    draftRole !== null &&
    draftModules !== null &&
    (draftRole !== selected.role ||
      (draftRole === "user" && !sameMembers(draftModules, selected.modules)));

  const busy = setRole.isPending || setModules.isPending;
  const locked = selected?.protected === true;

  const toggleModule = useCallback((moduleId: string, checked: boolean) => {
    setDraftModules((current) => {
      if (current === null) return current;
      if (checked) {
        return current.includes(moduleId) ? current : [...current, moduleId];
      }
      return current.filter((id) => id !== moduleId);
    });
  }, []);

  const save = useCallback(async () => {
    if (!selected || draftRole === null || draftModules === null) return;
    setError(null);
    setSaved(false);
    try {
      // Modules first: a role that grants everything makes the grant moot, and demoting
      // to "user" should land on the ticked set rather than on the old grants.
      if (
        draftRole === "user" &&
        !sameMembers(draftModules, selected.modules)
      ) {
        await setModules.mutateAsync({
          userId: selected.id,
          modules: draftModules,
        });
      }
      if (draftRole !== selected.role) {
        await setRole.mutateAsync({ userId: selected.id, role: draftRole });
      }
      setSaved(true);
    } catch (err) {
      setError(
        messageOf(err, `Could not save changes for ${selected.display_name}.`)
      );
    }
  }, [selected, draftRole, draftModules, setModules, setRole]);

  const discard = useCallback(() => {
    setDraftRole(selected?.role ?? null);
    setDraftModules(selected ? [...selected.modules] : null);
    setError(null);
    setSaved(false);
  }, [selected]);

  if (currentUser.isLoading || !isAdmin) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-9" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <H1 styledAs="h4">User management</H1>

      {users.isError ? (
        <Alert status="error">
          {messageOf(users.error, "Could not load the list of users.")}
        </Alert>
      ) : null}

      <AccessRequestQueue />

      <div className="flex large:flex-row flex-col gap-4">
        <Card className="large:min-w-72 large:max-w-80">
          <CardBody className="flex flex-col gap-3">
            <H2 styledAs="h5">People</H2>

            <Field aria-label="Search people" block>
              <TextInput
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search people"
              />
            </Field>

            {users.isLoading ? (
              <div className="flex justify-center py-8">
                <Spinner className="h-8" />
              </div>
            ) : rows.length === 0 ? (
              <Caption className="py-6 text-center text-muted">
                Nobody matches “{search.trim()}”.
              </Caption>
            ) : (
              <nav className="-mx-2 flex large:max-h-[60vh] flex-col overflow-y-auto">
                <PanelSection>
                  {rows.map((row) => (
                    <PanelItem
                      key={row.id}
                      active={row.id === selectedId}
                      onClick={() => setSelectedId(row.id)}
                      endIcon={row.protected ? "lock" : undefined}
                    >
                      {row.display_name}
                    </PanelItem>
                  ))}
                </PanelSection>
              </nav>
            )}
          </CardBody>
        </Card>

        <Card className="flex-1">
          {selected === null ? (
            <CardBody>
              <EmptyState
                icon={{ icon: "user", size: 40 }}
                title="Nobody selected"
                headerLevel={2}
              >
                Choose someone to manage their access.
              </EmptyState>
            </CardBody>
          ) : (
            <>
              <CardBody className="flex flex-col gap-5">
                <div className="flex flex-wrap items-center gap-3">
                  <ProfilePicture
                    name={selected.display_name}
                    initials={initialsOf(selected.display_name)}
                  />
                  <div className="flex min-w-0 flex-col">
                    <div className="flex flex-wrap items-center gap-2">
                      <H2 styledAs="h5" className="truncate">
                        {selected.display_name}
                      </H2>
                      <Tag kind={ROLE_TAG_KIND[selected.role]}>
                        {ROLE_LABELS[selected.role]}
                      </Tag>
                      {selected.protected ? (
                        <Tooltip content="Fixed administrator — cannot be changed here">
                          <Tag kind="warning" startIcon="lock">
                            Fixed
                          </Tag>
                        </Tooltip>
                      ) : null}
                    </div>
                    {selected.email ? (
                      <Caption className="truncate text-muted">
                        {selected.email}
                      </Caption>
                    ) : null}
                  </div>
                </div>

                {locked ? (
                  <Alert status="info">
                    {selected.display_name} is a fixed administrator. Their role
                    and access cannot be changed here.
                  </Alert>
                ) : null}
                {error ? <Alert status="error">{error}</Alert> : null}
                {saved && !error && !dirty ? (
                  <Alert status="success">Saved.</Alert>
                ) : null}

                <div className="flex flex-col gap-1">
                  <Field label="Role" floatingLabel block>
                    <Select
                      options={ROLE_OPTIONS}
                      value={effectiveRole}
                      onChange={(value) => setDraftRole(String(value) as Role)}
                      disabled={locked || busy}
                      className="max-w-72"
                    />
                  </Field>
                  <Caption className="text-muted">
                    {ROLE_HELP[effectiveRole]}
                  </Caption>
                </div>

                <div className="flex flex-col gap-2">
                  <H2 styledAs="subhead2">Module access</H2>

                  {effectiveRole !== "user" ? (
                    // Ticking a box would be a no-op: the role already grants everything.
                    <Caption className="text-muted">
                      Every module, from the {ROLE_LABELS[effectiveRole]} role.
                      Switch to User to choose individual modules.
                    </Caption>
                  ) : modules.isLoading ? (
                    <Spinner className="h-6" />
                  ) : (
                    <>
                      <div className="flex flex-col gap-2">
                        {(modules.data ?? []).map((module) => (
                          <Checkbox
                            key={module.id}
                            label={module.label}
                            checked={(draftModules ?? []).includes(module.id)}
                            disabled={locked || busy}
                            onChange={(event) =>
                              toggleModule(module.id, event.target.checked)
                            }
                          />
                        ))}
                      </div>
                      {(draftModules ?? []).length === 0 ? (
                        <Caption className="text-muted">
                          Nothing ticked — they can sign in but will not see any
                          modules.
                        </Caption>
                      ) : null}
                    </>
                  )}
                </div>
              </CardBody>

              <CardFooter className="flex flex-wrap items-center justify-between gap-3">
                <Caption className="text-muted">
                  {locked
                    ? ""
                    : dirty
                      ? "Unsaved changes"
                      : "All changes saved"}
                </Caption>
                <ButtonGroup>
                  <Button
                    variant="tertiary"
                    onClick={discard}
                    disabled={!dirty || busy}
                  >
                    Discard
                  </Button>
                  <Button
                    variant="primary"
                    onClick={save}
                    disabled={!dirty || busy || locked}
                  >
                    {busy ? "Saving…" : "Save changes"}
                  </Button>
                </ButtonGroup>
              </CardFooter>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

/**
 * Pending access requests, above the user list because it is the one thing on this screen
 * with somebody waiting on it. Approving grants exactly what was asked for, on top of
 * whatever the person already has.
 */
function AccessRequestQueue() {
  const { data: requests, isLoading } = useAccessRequests();
  const { data: modules } = useModules();
  const approve = useApproveAccessRequest();
  const reject = useRejectAccessRequest();
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);

  const pending = requests ?? [];
  if (isLoading || pending.length === 0) return null;

  function decide(
    requestId: string,
    action: "approve" | "reject",
    displayName: string
  ) {
    setError(null);
    setActing(requestId);
    const mutation = action === "approve" ? approve : reject;
    mutation.mutate(
      { requestId },
      {
        onError: (err) =>
          setError(
            apiErrorMessage(err) ??
              `Could not ${action} the request from ${displayName}.`
          ),
        onSettled: () => setActing(null),
      }
    );
  }

  return (
    <Card>
      <CardBody className="flex flex-col gap-3">
        <H2 styledAs="h5">Access requests ({pending.length})</H2>

        {error ? <Alert status="error">{error}</Alert> : null}

        <div className="flex flex-col gap-3">
          {pending.map((request) => (
            <div
              key={request.id}
              className="flex flex-wrap items-start justify-between gap-3 border-separator border-b pb-3 last:border-b-0 last:pb-0"
            >
              <div className="flex min-w-0 flex-col gap-1">
                <span className="font-medium">{request.display_name}</span>
                <span className="flex flex-wrap items-center gap-2">
                  {request.modules.map((id) => (
                    <Tag key={id} kind="primary">
                      {modules?.find((module) => module.id === id)?.label ?? id}
                    </Tag>
                  ))}
                </span>
                {request.note ? (
                  <Caption className="text-muted">“{request.note}”</Caption>
                ) : null}
                <Caption className="text-muted">
                  {request.username} · asked{" "}
                  {new Date(request.created_at).toLocaleString()}
                </Caption>
              </div>
              <ButtonGroup>
                <Button
                  variant="tertiary"
                  disabled={acting !== null}
                  onClick={() =>
                    decide(request.id, "reject", request.display_name)
                  }
                >
                  Reject
                </Button>
                <Button
                  variant="primary"
                  disabled={acting !== null}
                  onClick={() =>
                    decide(request.id, "approve", request.display_name)
                  }
                >
                  {acting === request.id ? "Saving…" : "Approve"}
                </Button>
              </ButtonGroup>
            </div>
          ))}
        </div>
      </CardBody>
    </Card>
  );
}
