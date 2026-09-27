import {
  Alert,
  createColumnHelper,
  DataTable,
  EmptyState,
  H1,
  P,
  type PaginationState,
  type SortingState,
  Spinner,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";
import { useCurrentUser, useDocuments } from "@/api/queries";
import type {
  DocumentSort,
  DocumentSummary,
  RunStatus,
  SortOrder,
} from "@/api/types";
import { RUN_STATUSES } from "@/api/types";
import { HistoryFilters } from "@/components/history-filters";
import { RouterLink } from "@/components/router-link/router-link";
import { StatusBadge } from "@/components/status-badge";

const PAGE_SIZE = 25;
const SORT_KEYS: DocumentSort[] = ["date", "name", "user"];

/**
 * Every field is optional so that linking to `/history` needs no search params.
 * The values are always normalised by `normalizeSearch` before use.
 */
type HistorySearch = {
  q?: string;
  silo?: string;
  status?: string;
  mine?: boolean;
  sort?: DocumentSort;
  order?: SortOrder;
  page?: number;
};

function normalizeSearch(
  search: Record<string, unknown>
): Required<HistorySearch> {
  const sort = String(search.sort ?? "date") as DocumentSort;
  const status = String(search.status ?? "");
  return {
    q: String(search.q ?? ""),
    silo: String(search.silo ?? ""),
    status: RUN_STATUSES.includes(status as RunStatus) ? status : "",
    mine: search.mine === true || search.mine === "true",
    sort: SORT_KEYS.includes(sort) ? sort : "date",
    order: search.order === "asc" ? "asc" : "desc",
    page: Math.max(Number(search.page ?? 1) || 1, 1),
  };
}

export const Route = createFileRoute("/_app/history")({
  // Filters live in the URL so a filtered view can be shared and survives a
  // reload, which is the same reasoning that makes documents addressable.
  validateSearch: (search: Record<string, unknown>): HistorySearch =>
    normalizeSearch(search),
  component: HistoryPage,
});

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const columnHelper = createColumnHelper<DocumentSummary>();

const columns = [
  columnHelper.accessor("title", {
    id: "name",
    header: "Document",
    cell: (info) => (
      <RouterLink
        to="/documents/$documentId"
        params={{ documentId: info.row.original.id }}
      >
        {info.getValue()}
      </RouterLink>
    ),
  }),
  columnHelper.accessor("silo_id", {
    id: "silo",
    header: "Type",
    enableSorting: false,
    cell: (info) => info.getValue().toUpperCase(),
  }),
  columnHelper.accessor("status", {
    id: "status",
    header: "Status",
    enableSorting: false,
    cell: (info) => <StatusBadge status={info.getValue()} />,
  }),
  columnHelper.accessor((row) => row.owner.display_name, {
    id: "user",
    header: "Owner",
  }),
  columnHelper.accessor("started_at", {
    id: "date",
    header: "Started",
    cell: (info) => dateFormatter.format(new Date(info.getValue())),
  }),
];

function HistoryPage() {
  const search = normalizeSearch(Route.useSearch());
  const navigate = useNavigate({ from: Route.fullPath });
  const { data: currentUser } = useCurrentUser();

  const patchSearch = useCallback(
    (patch: Partial<HistorySearch>) => {
      navigate({
        search: (previous) => ({
          ...previous,
          ...patch,
          page: patch.page ?? 1,
        }),
        replace: true,
      });
    },
    [navigate]
  );

  const { data, isPending, isError, error } = useDocuments({
    q: search.q || undefined,
    silo: search.silo || undefined,
    status: (search.status || undefined) as RunStatus | undefined,
    user: search.mine ? currentUser?.username : undefined,
    sort: search.sort,
    order: search.order,
    page: search.page,
  });

  const sorting: SortingState = [
    { id: search.sort, desc: search.order === "desc" },
  ];
  const pagination: PaginationState = {
    pageIndex: search.page - 1,
    pageSize: PAGE_SIZE,
  };

  const handleSortingChange = (
    updater: SortingState | ((old: SortingState) => SortingState)
  ) => {
    const next = typeof updater === "function" ? updater(sorting) : updater;
    const [first] = next;
    if (!first) return;
    patchSearch({
      sort: first.id as DocumentSort,
      order: first.desc ? "desc" : "asc",
    });
  };

  const handlePaginationChange = (
    updater: PaginationState | ((old: PaginationState) => PaginationState)
  ) => {
    const next = typeof updater === "function" ? updater(pagination) : updater;
    patchSearch({ page: next.pageIndex + 1 });
  };

  const hasFilters = Boolean(
    search.q || search.silo || search.status || search.mine
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <H1 styledAs="h2">Documents</H1>
        <P className="text-muted">
          Every document authored on the platform, with its inputs and outputs.
        </P>
      </div>

      <HistoryFilters
        values={{
          q: search.q,
          silo: search.silo,
          status: search.status,
          mine: search.mine,
        }}
        onChange={patchSearch}
      />

      {isError ? (
        <Alert
          status="error"
          subtitle={error instanceof Error ? error.message : undefined}
        >
          Could not load documents
        </Alert>
      ) : isPending ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-9" />
        </div>
      ) : data.total === 0 && !hasFilters ? (
        <EmptyState
          icon={{ icon: "folder-open", size: 40 }}
          title="No documents yet"
          headerLevel={2}
        >
          Documents generated on the platform will be listed here.
        </EmptyState>
      ) : (
        <DataTable
          data={data.items}
          columns={columns}
          enableSorting
          manualSorting
          enablePagination
          manualPagination
          rowCount={data.total}
          state={{ sorting, pagination }}
          onSortingChange={handleSortingChange}
          onPaginationChange={handlePaginationChange}
          noResultsMessage="No documents match these filters."
          // The server fixes the page size, so only that option is offered.
          paginatorProps={{ viewOptions: [PAGE_SIZE] }}
        />
      )}
    </div>
  );
}
