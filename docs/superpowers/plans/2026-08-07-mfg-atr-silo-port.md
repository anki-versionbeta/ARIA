# MFG ATR Silo Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

## Context

Phase 4 (the ISO silo) is committed. This adds a fourth silo, absorbing the ATR/MFGR report generator at
`C:\Python_files\MFG_ATR\ATR-mfgr-report-generation-main 1 for aria\ATR-mfgr-report-generation-main`
— roughly 8,600 lines of Python across a FastAPI `api/` package, a Streamlit `ui/`, and the report
pipeline in `src/`.

**This is not a fourth ISO-shaped port.** Three structural differences drive the whole design:

1. **There is no uploaded document.** The input is an identifier chosen from a live dropdown — a CMC
   request id (`CMC-10352`) for ATR, a batch id (`BAX000584`) for MFGR. The platform's create-document
   endpoint takes an `UploadFile` and the silo contract is built around `ACCEPTS` and
   `ctx.storage.read_input(run)`. Neither applies.
2. **The data comes from an Oracle data warehouse**, not from a file. This is the `warehouse/` layer the
   spec already plans as phase 5 ("CMC read-only query layer"), and nothing warehouse-shaped exists in
   the platform yet.
3. **It is two report types, sharing a flow but almost no content** (see the comparison below). The
   decision is to ship them as **one silo with a report-type selector**, so the stage skeleton is shared
   and everything domain-specific dispatches on type.

The governing porting rule is unchanged: reproduce the logic exactly. These are GxP documents with an
ALCOA+ audit trail, so output drift and audit drift are both validation problems.

### Decisions taken (confirmed with the user)

| Decision | Choice |
|---|---|
| One silo or two | **One silo with a report-type selector.** Stages are shared; ATR/MFGR domain logic dispatches on type |
| No-upload input | **Silo-owned creation endpoint.** `router.py` creates the run from an identifier; the platform's upload endpoint is untouched |
| Oracle driver | **Driver first — done.** `oracledb` 4.0.2 is installed and imports in thin mode, so no Oracle Instant Client is needed |

### The driver is installed, and how

`oracledb` 4.0.2 is in the pyenv interpreter. Getting it there needed two things together, and the recipe
is worth keeping because the machine's defaults fight it:

* Point pip at the Artifactory PyPI mirror (`abbvie.jfrog.io/artifactory/api/pypi/pypi/simple`) using the
  read-only service account already committed in `da-frontend/.npmrc`. pypi.org is `CATEGORY_DENIED`.
* **Override `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`** for the command. Both are set machine-wide to
  `C:\ILIAD_certificate\AbbVieFullChain.pem`, which has no public roots, and requests honours those env
  vars *over* pip's own `--cert`. Artifactory serves the index through the intercepting proxy (AbbVie
  chain) but redirects the wheel to `*.s3.amazonaws.com` (public roots), so the download fails while the
  index succeeds. `da-backend/certs/ca-bundle.pem` already contains both.

Everything else the pipeline needs is present: `reportlab`, `pypdf`, `python-docx`, `openpyxl`, `pandas`.
`truststore` is absent and not needed — it exists in the original to trust the Windows cert store,
whereas the platform does explicit CA-bundle handling.

---

## ATR vs MFGR: what is actually shared

This table is the reason the silo is built as one skeleton with two implementations rather than one
merged pipeline. Read it before writing any code.

| | ATR | MFGR |
|---|---|---|
| Input | `request_id`; `normalize_request_id` → `{short, display, query}` | `batch_id`; `normalize_batch_id` → `{short, display, query, registration}` |
| Id forms | `10352` / `CMC-10352` → query `PEGA-PROD-CMC-10352`, display `CMC-10352` | `BAX000584` (NEST) → `nest-br-prod-BAX000584`; `BA259821` (manual) → bare id |
| Offline mode | **Yes** — `fetch_fixture` reads captured JSON | **No** — live database only |
| Queries | 6 named statements in `QUERIES` (`header`, `methods`, `results`, `scope_summary`, `validation`, `batch_lots`), all bound on `:request_id` | Its own `QUERIES`, plus `resolve_query_root(conn, short)` run first |
| Report builder | `build_atr_report(display, raw)` | `build_mfgr_report(short, raw)` |
| Editable fields | **5** | **19** |
| Defaults dataclass | `ManualDefaults` — names **differ** from the form fields (`spec_id`↔`hqc_spec_id`, `hqc`↔`hqc_assessment`) | `MfgrDefaults` — names match the form fields **1:1** |
| Empty result | `_has_data(raw)` gate → returns `generated: false, reason: no_data` (**not** an error) | any exception → HTTP 502 |
| Audit | `record_generation(request_ids=…)`, 3-key id dict | separate `mfgr_audit` module, `batch_ids=…`, 2-key id dict |

**Genuinely shared:** `OUTPUT_DIR`, `DbConfig`/`get_connection`, the S3 run-folder layout, process-id
minting, the flag shape (`severity`/`area`/`message`), the PDF+DOCX output pair, and the
generate → edit → finalize flow including AcroForm-then-flatten.

**Genuinely different:** every piece of domain logic. Do not attempt to unify the queries, the report
objects, the builders or the field sets.

### The editable fields, exactly

These drive the UI, so they are reproduced here in full.

**ATR — 5 AcroForm fields**, the only interactive parts of the document:

| Form field | `ManualDefaults` | Values |
|---|---|---|
| `regulatory` | `regulatory` | `"Yes"` / `"No"` / `""` |
| `hqc_spec_id` | `spec_id` | free text |
| `hqc_assessment` | `hqc` | one of `meets`, `not_meets`, `not_spec`, `other` |
| `hqc_remarks` | `remarks` | free text |
| `hqc_remarks_na` | `remarks_na` | checkbox; true for `yes`/`on`/`true`/`1`/`checked` |

`HQC_OPTIONS` label text must be copied verbatim from `src/pdf_builder.py:42-49` — it is regulatory
wording, not UI copy.

**MFGR — 19 AcroForm fields**, names matching `MfgrDefaults` 1:1: `clinical_phase`, `ds_quality`,
`ds_site`, `ds_form`, `ds_characteristics`, `storage_temp`, `fill_volume`, `dose_lyo`, `recon_medium`,
`recon_volume`, `recon_concentration`, `format_desc`, `stopper_desc`, `crimp_desc`, `dev_samples`,
`compatibility`, `conclusion`, `procedure_notes`, `topic_notes`.

---

## Architecture

Silo id `mfg_atr`, `LABEL = "ATR / MFGR — Analytical & Manufacturing Reports"`, `ACCEPTS = []` (nothing to
upload).

```
STAGES = ["fetch", "generate", "await_edits", "finalize"]
```

| Stage | Does | Replaces |
|---|---|---|
| `fetch` | normalise the id, query the warehouse (or read the fixture), apply ATR's `_has_data` gate, store the raw snapshot as media | the first half of `POST /generate` |
| `generate` | build the report object, write the GxP audit record, render the clean PDF + DOCX, attach both as outputs, upload to the compliance S3 prefix | the second half of `POST /generate` |
| `await_edits` | park for the human to fill the editable fields | the in-memory TTL cache between generate and finalize |
| `finalize` | rebuild the report from the stored snapshot, re-bake PDF + DOCX with the author's values, flatten the AcroForm, attach both | `POST /finalize` |

**Checkpoint contract:**

| Stage | Output |
|---|---|
| `fetch` | `{"report_type": "atr"\|"mfgr", "ids": {...}, "source": "live"\|"fixture", "raw_key": str, "row_counts": {...}, "has_data": bool}` |
| `generate` | `{"generated_at": str, "flags": [...], "audit_key": str, "preview_pdf": str, "preview_docx": str}` |
| `await_edits` | `{"field_values": {...}}` — written by `router.py` via `queue.complete_paused_stage` |
| `finalize` | `{"pdf_bytes": int, "docx_bytes": int}` |

### The most important design decision: no report cache

The original holds the built report object in a TTL cache between generate and finalize, and answers
*"This report run has expired — regenerate it, then download"* when it lapses. That is the same failure
the platform exists to remove — ISO's "Session not found", BOP's in-memory `JOBS`.

**Instead: persist the raw query snapshot in `fetch`, and rebuild the report in `finalize`.** Both
builders are pure functions of `(identifier, raw)`, so rebuilding is deterministic and cheap, and it
removes the expiry failure mode entirely without changing a line of report logic. It also means the audit
snapshot and the rebuild read the *same* bytes, which is what the ALCOA+ trail claims.

### GxP audit — preserve exactly

`src/audit.py:record_generation` is a compliance artefact, not logging. It writes two things:

* a line appended to `output/audit/atr_audit.jsonl`;
* a per-run snapshot at `<output>.audit.json` containing the **full source data**.

The record's fields are fixed: `timestamp_utc`, `os_user`, `app_user`, `host`, `request_id`, `queries`
(the exact SQL), `bind_values`, `row_counts`, `source_data_sha256`, `review_flags`, `output_path`,
`finalized`. Keep every key and its spelling.

Two things need care in the port, both flagged rather than silently changed:

* **The JSONL append is a shared mutable file.** Object stores do not append, and several workers may
  finish at once. Write one immutable object per run instead (`audit/<run_id>.jsonl` alongside the
  snapshot) and keep the record shape byte-identical. Do **not** try to emulate append-in-place.
* **`os_user` becomes the worker's service account**, not the author. That is correct — `app_user` already
  carries the authenticated identity — but it changes what the field means, so it must be recorded.

`QUERY_REQUEST_IDS` is deliberately **not** in `QUERIES` ("a convenience lookup, not part of the audited
per-request Query Pack"). Keep it out of the audited set.

### Platform additions — exactly one

Everything else in this port is silo-level. The single platform change, confirmed with the user:

**`da-backend/da_platform/warehouse.py` plus a `ctx.warehouse` property** — the spec's read-only CMC query
layer. Resolves the connection parameters from settings, builds the `oracledb` connection, sets
`oracledb.defaults.fetch_lobs = False` (needed so ATR's Q4 CLOB `scope`/`summary_conclusion` come back as
`str`; a lazy LOB cannot be read after the connection closes), and yields a connection from a context
manager. Resolved lazily on `StageContext`, exactly like `ctx.textract`, so constructing a context still
requires no database.

This exists because `tests/test_silo_isolation.py` fails the build on `os.environ` / `os.getenv` in any
silo file, with the guidance *"receive configuration through the stage context"*. Credentials stay in the
platform `.env`; the silo never reads them.

**The frontend needs no platform change.** `silos.$siloId.new.tsx:36` already reads `screens.upload`, and
`screens.review` gained its call site during the ISO port. Both silo screens are pure additions.

**Five environment reads have to move, not just the three credentials.** `src/config.py` also reads
`CMCDW_SCHEMA`, `CMCDW_RESULTS_OBJECT`, `CMC_REQUEST_PREFIX` and `MFGR_BATCH_PREFIX` from the
environment, and those live in what becomes silo code:

* `CMCDW_SCHEMA` and `CMCDW_RESULTS_OBJECT` name database objects, so they belong to the warehouse
  config and are exposed as `ctx.warehouse.schema` / `.results_object`.
* `CMC_REQUEST_PREFIX` (`PEGA-PROD-CMC-`) and `MFGR_BATCH_PREFIX` (`nest-br-prod-`) are id-format
  constants, not warehouse config. They become plain module constants at their current defaults, which
  **drops the ability to override them by environment variable** — see the FLAG below.

---

## File structure

`da-backend/silos/mfg_atr/` — shared skeleton, per-type implementations.

| File | Ported from | Responsibility |
|---|---|---|
| `location.py` | `api/s3.py:32-34` | `STORAGE_PREFIX = "ATR_MFG"`, folder map. Preserves the compliance layout `<base>/{ATR\|MFGR}_<pid>_<id>/{pdf,docx}/` |
| `silo.py` | `api/routers/{atr,mfgr}.py` | `LABEL`, `ACCEPTS = []`, `STAGES`, the four stages, dispatch on `report_type` |
| `router.py` | `api/routers/*` pickers | Run creation from an identifier, plus `request-ids`, `batch-ids`, `exists`, `status`, and the `field-values` completion for `await_edits` |
| `ids.py` | `api/ids.py` | `new_process_id`, `safe_segment` — verbatim; the process id names the S3 folder |
| `config.py` | `src/config.py:1-95` | ATR ids, `DWH_SCHEMA`, `RESULTS_OBJECT`, `QUDT_UNIT_MAP`, `ATTACHMENT_MARKER` |
| `mfgr_config.py` | `src/config.py:96-238` | MFGR ids, `UNIT_MAP`, `UNIT_OP_MAP`, `EXPECTED_UNIT_OP_LABELS`, `parse_batch_name` |
| `atr_data.py` | `src/data_access.py` | The 6-query pack, `fetch_all`, `fetch_fixture`, `fetch_request_ids` |
| `atr_report.py` | `src/atr_report.py` | `build_atr_report` |
| `atr_pdf.py` | `src/pdf_builder.py` | `ManualDefaults`, `HQC_OPTIONS`, the 5 AcroForm fields, `generate_atr_pdf` |
| `atr_docx.py` | `src/atr_docx.py` | `generate_atr_docx` |
| `docx_common.py` | `src/docx_common.py` | Shared docx helpers (used by both types) |
| `mfgr_data.py` | `src/mfgr_data_access.py` | MFGR queries, `resolve_query_root`, `fetch_report` |
| `mfgr_report.py` | `src/mfgr_report.py` | `build_mfgr_report` |
| `mfgr_pdf.py` | `src/mfgr_pdf_builder.py` | `MfgrDefaults`, the 19 AcroForm fields, `generate_mfgr_pdf` |
| `mfgr_docx.py` | `src/mfgr_docx.py` | `generate_mfgr_docx` |
| `audit.py` | `src/audit.py` + `src/mfgr_audit.py` | Both `record_generation` variants, record shape preserved |
| `finalize.py` | `src/finalize.py` + `src/mfgr_finalize.py` | AcroForm flattening for both types |
| `workspace.py` | *new* | Scratch dir, because both builders write to a path rather than returning bytes |

### Frontend — reuse `web/`, do not rebuild it

The source repo contains a React SPA at `web/` on **the same stack as `da-frontend`** —
`@abbvie-unity/react` 5, TanStack Router, Vite 7, vitest, Biome, Tailwind 4 — down to identical
`router-link/`, `router-button/`, `mode-select/` components and `utils/cn.ts`. They are siblings from the
same Unity template, so this is adaptation, not a rewrite.

| `web/src` file | Lines | Disposition |
|---|---|---|
| `components/pdf-overlay-editor/pdf-overlay-editor.tsx` | 388 | **Reuse.** The AcroForm overlay editor over a PDF.js render — the hardest piece of the whole frontend, and it is driven by the form field names, which do not change. Rewiring is limited to how it obtains the PDF bytes and how it submits values |
| `components/review-flags/review-flags.tsx` | 25 | **Reuse** as-is; the flag shape (`severity`/`area`/`message`) survives the port |
| `components/download-menu/download-menu.tsx` | 37 | **Reuse**, simplified — see the cookie note below |
| `components/ready-panel/ready-panel.tsx` | 30 | **Reuse** |
| `routes/atr.tsx` | 354 | **Decompose, don't copy.** The picker half becomes the silo's `upload` screen; the edit half becomes its `review` screen; the progress polling and download plumbing are **deleted** because the platform already does them |
| `routes/mfgr.tsx` | 397 | Same decomposition |
| `lib/api.ts`, `lib/session.ts`, `lib/auth.tsx` | 284 | **Discard.** Bearer JWT in `sessionStorage`; the platform uses an httpOnly cookie with `api/http.ts` + TanStack Query |
| `components/login-page/login-page.tsx` | 97 | **Discard** — the platform owns login |
| `router-link`, `router-button`, `mode-select`, `utils/cn` | ~200 | **Skip** — already present in `da-frontend`, identical template origin |

Net: roughly 480 lines reused close to as-is, 750 decomposed into two silo screens, 380 discarded.

**Cookie auth makes downloads simpler.** `web/` fetches every file through `fetchBlobUrl` /
`fetchArrayBuffer` because a Bearer token cannot ride on an `<a href>`. With the platform's httpOnly
cookie the browser sends credentials automatically, so downloads become a plain anchor exactly as
`documents.$documentId.tsx` already does. Only the PDF.js preview still needs an explicit fetch, and that
becomes `credentials: "include"` rather than a header.

**New dependency:** `pdfjs-dist` (`^6.2.108`) is in `web/package.json` and **not** in `da-frontend`. It
must be added, and the install goes through Artifactory with the CA-bundle override noted above.

**Files:** `da-frontend/src/silos/mfg_atr/` — `index.ts` (contributing `upload` **and** `review`),
`mfg-atr-new.tsx` (report-type selector + identifier picker, from the two routes' picker halves),
`atr-edit-form.tsx` (5 fields), `mfgr-edit-form.tsx` (19 fields), and `pdf-overlay-editor.tsx` +
`review-flags.tsx` + `download-menu.tsx` carried over.

---

## Tasks

`PY=/c/Users/MOHANAX25/pyenv/Scripts/python.exe`. Every task: failing test first, exact command and
expected failure, then *"Port `<file>:AAAA-BBBB` verbatim, then make exactly these edits: …"*, then the
passing run, then a commit. No commits until the user has manually tested — they have asked for that
explicitly, so accumulate the work and commit on request.

### Group 1 — platform warehouse layer
- [ ] `da_platform/warehouse.py` with `DbConfig` from settings, `fetch_lobs = False`, a connection
      context manager, and a `reachable()` probe
- [ ] `ctx.warehouse`, resolved lazily so building a context needs no database
- [ ] Tests with a fake connection; one integration test skipped unless `CMCDW_DSN` is set

### Group 2 — silo skeleton and creation endpoint
- [ ] `location.py`, `ids.py`, `silo.py` with the four stages, `ACCEPTS = []`
- [ ] `router.py`: create-from-identifier, the two pickers, `exists`, `status`, `field-values`
- [ ] Tests: discovery, `ACCEPTS = []` is tolerated by the registry, run creation without a file,
      bad identifier is a 400 with the original message, non-owner 403, resume skips the paused stage

### Group 3 — ATR pipeline
- [ ] `config.py`, `atr_data.py`, `atr_report.py`
- [ ] `atr_pdf.py`, `atr_docx.py`, `docx_common.py`
- [ ] `audit.py` (ATR variant) with the record shape pinned by a test
- [ ] Verified end to end against the repo's own JSON fixture — no database needed

### Group 4 — MFGR pipeline
- [ ] `mfgr_config.py`, `mfgr_data.py`, `mfgr_report.py`
- [ ] `mfgr_pdf.py`, `mfgr_docx.py`, `audit.py` (MFGR variant)
- [ ] Verify against the two captured MFGR fixtures, `tests/fixtures/BAX000584.json` (a NEST batch, 5
      query keys) and `BAX000701.json` (8 keys, so a fuller batch). Note these are *test* fixtures — the
      MFGR router itself has no user-facing fixture source, and adding one would be new behaviour

### Group 5 — finalize, frontend, verification
- [ ] `finalize.py` — AcroForm flattening for both types
- [ ] The identifier/selector screen and both edit forms
- [ ] Golden comparison against the four example PDFs in `ATR_Examples/` and the MFGR examples in
      `MFGR_Examples/`, reusing `tests/docx_projection.py` from the ISO work for the DOCX side

---

## Verification

```bash
cd da-backend && $PY -m pytest -q                 # existing 307 must stay green
cd da-frontend && npm run check && npx tsc --noEmit && NODE_OPTIONS=--experimental-require-module npx vitest run
```

**Offline:** ATR is fully exercisable through `fetch_fixture` against `tests/fixtures`. That is the
primary verification path and it needs no database and no credentials.

**Against the warehouse:** needs `CMCDW_USER`, `CMCDW_PASSWORD`, `CMCDW_DSN`. Probe reachability before
assuming anything works — the driver installing says nothing about the DSN being routable or the account
being valid.

**Golden comparison:** `ATR_Examples/` holds four real ATR PDFs and `MFGR_Examples/` holds real MFGR
output including a DOCX. Compare structurally, not byte-for-byte. These are **copyrighted internal
documents and must not be committed** — read them from an env-var path and skip when unset, exactly as
`tests/test_iso_golden.py` does with `ISO_CORPUS_DIR`.

**Manual:** log in → pick ATR or MFGR → choose an identifier → watch `fetch`/`generate` → land on the
edit form → fill the fields → finalize → download both PDF and DOCX → confirm the audit snapshot exists
and its `source_data_sha256` matches the stored raw snapshot.

---

## FLAGs

**Must resolve before the port is trustworthy**
- **`UNIT_OP_MAP` is entirely unverified.** `src/config.py` says so in its own comment: "Every entry below
  is UNVERIFIED (guessed by inspection) — the client owns the DSDT ontology and must give us the
  authoritative lookup." Unknown URIs surface as review flags. Port as-is, but this is an open item owned
  by the client, not something to quietly firm up.
- **The captured fixtures do not match the current query packs, and both builders must tolerate that.**
  `tests/fixtures/cmc10352.json` carries only 5 of ATR's 6 query keys — `batch_lots` (Q6, the batch-id →
  DP/SAP lot-number lookup) is **absent**, so the fixture predates it. The two MFGR fixtures disagree with
  each other too: `BAX000584.json` has 5 keys while `BAX000701.json` has 8 (`material_props`,
  `process_steps`, `quality_standard` extra). Confirm what each builder does with a missing key before
  relying on fixture runs as proof of correctness — a fixture that silently exercises fewer branches is
  the failure mode to avoid here.
- **MFGR is offline-testable, but only in tests.** Fixture data exists, yet the MFGR router has no
  `source: fixture` option the way ATR does. Use the fixtures for verification; do **not** add a
  user-facing fixture mode to MFGR, which would be new behaviour.

**Ported deliberately**
- ATR's `_has_data` gate returns *success with `generated: false`*, not an error. On the platform this
  should be a completed run carrying a "no data" message, not a failed one — a failure would invite a
  pointless retry.
- MFGR caches the whole response by identifier and skips re-auditing on a cache hit ("MUI-9 semantics").
  With the cache gone, decide explicitly whether a second run of the same batch id writes a second audit
  record. The safest reading of ALCOA+ is yes — every generation is a generation.
- `s3.upload_file` **never raises**; archival is best-effort by design. Keep that, but note it means a
  silent loss of the compliance copy.
- ATR's `ManualDefaults` field names differ from its form field names while MFGR's match 1:1. Do not
  "tidy" either — the mapping in `_defaults_from_fields` is the contract the front end binds to.

**New behaviour, no original**
- **Two id-format environment overrides are dropped.** `CMC_REQUEST_PREFIX` and `MFGR_BATCH_PREFIX`
  become module constants at their current defaults (`PEGA-PROD-CMC-`, `nest-br-prod-`), because a silo
  may not read the environment. If anyone actually sets either variable in a deployed environment this is
  a behaviour change and they need a home in the warehouse config instead — worth asking before release
  rather than discovering it when a query returns nothing.
- Per-run audit objects instead of an appended JSONL file (object stores cannot append).
- `os_user` now names the worker's service account rather than the author's machine.
- Progress reporting inside the query and render stages, so a slow warehouse query does not let the
  reaper reclaim a live run (`stale_claim_timeout_s` is 900s).

---

## Critical files

**Source of truth:** `C:\Python_files\MFG_ATR\ATR-mfgr-report-generation-main 1 for aria\ATR-mfgr-report-generation-main`
— `src/` for the pipeline, `api/routers/{atr,mfgr}.py` for the flow, `ui/{atr,mfgr}_view.py` for what the
human edits, `docs/FUNCTIONALITY_INVENTORY.md` and `docs/CLIENT_REQUIREMENTS.md` for the requirement ids
referenced in code comments (`NF-4`, `MUI-9`).

**Platform files to change:** `da_platform/warehouse.py` (new), `da_platform/engine/context.py`
(`ctx.warehouse`).

**Reuse, do not reinvent:** `silos/iso/workspace.py` (the scratch-dir pattern) · `silos/iso/router.py`
(the pause-completion pattern) · `tests/docx_projection.py` and `tests/test_iso_golden.py` (structural
comparison and corpus gating) · `tests/iso_fakes.py` (stub conventions) · `da_platform/engine/queue.py`
(`complete_paused_stage`).
