# Unified Document Authoring Platform — Design

**Date:** 2026-08-04
**Status:** Approved for phased implementation
**Diagram:** `C:\Python_files\DA Roadmap\DA_Platform_Architecture.drawio` (page 1 logical, page 2 deployment contract)

---

## 1. Context

Document generation today lives in independent silo apps, one per report type. Two are in scope as reference and migration targets:

| | ISO (`C:\Python_files\ISO\code_07162026`) | BOP (`C:\Python_files\BOP\app_07302026`) |
|---|---|---|
| Size | `main.py` ~4,780 lines | `main.py` ~2,590 lines |
| Stack | Flask 3.1.2, vanilla JS + jQuery | Flask injected by Dataiku, vanilla JS |
| Input | one PDF + start/end section index | one PDF or DOCX |
| Workflow | one-shot generate-and-download | async job, 2s polling, human edit pause |
| Human edit | none | Quill editor, 20 in-memory versions |
| Ingest depth | garble detection, 5-level TOC fallback, Textract figures/tables, formula detection + LLM triage | text extraction, vision OCR fallback |
| Generation | vision LLM per page; row extraction | up to 15 parallel LLM calls per run |
| Storage | S3 + local mirror under `<temp>/iso_doc_gen` | Dataiku managed folders |
| State | module-level `sessions = {}` | module-level `JOBS = {}` + reaper thread |
| Auth | none | none (delegated to Dataiku) |
| Output | `.docx` only | `.docx` only |

Both call the same internal LLM gateway (Iliad, `iliad-emerging-api.abbvienet.com`), both render PDF pages at 150 DPI for vision OCR when there is no usable text layer, both emit `.docx` via `python-docx`, and both lose all state on restart.

### Problems being solved

1. **No single entry point.** Each report type is a separate app with its own URL and no shared identity.
2. **No record of what was produced.** Nobody can answer "who generated what, when, from which input".
3. **Nothing survives a restart.** Run state lives in a process-local dict, so a deploy destroys in-flight work, and neither app can run more than one worker — BOP already returns 404 when a status poll lands on a worker that did not handle the upload.
4. **Good ideas are trapped.** BOP's edit-and-version loop is what ISO needs; ISO's Textract media extraction is what BOP needs. Neither can reach the other.
5. **Credentials are mishandled.** BOP has a live Iliad API key hardcoded in source (line 63). ISO holds a short-lived AD JWT and temporary STS credentials in cached client singletons with no refresh, so it fails silently after expiry. Both disable TLS verification globally.

### Goals

- One application, one login, one codebase per tier, into which each silo is absorbed.
- Adding a new silo is **purely additive** — new folders only, no platform file changes.
- A durable, searchable record of every authored document with its inputs and outputs.
- Support 15+ concurrent generations without degradation.
- Fix the credential and TLS handling as part of the migration.

### Non-goals

- Not a home page that proxies to separately deployed silo servers. One codebase, one deployment.
- No approval or e-signature workflow.
- No document deletion (records are permanent).
- No cross-silo document composition.

---

## 2. Decision log

Each row records what was decided and why, so future readers can judge whether the reasoning still holds.

| # | Decision | Rationale |
|---|---|---|
| D1 | Left nav shell with a document-type dropdown; selected silo renders in the right pane | Explicit product requirement |
| D2 | Each silo owns its own React screens; **not** manifest-driven generic screens | Preserves ISO's TOC range picker and BOP's editor exactly; migration becomes a port rather than a redesign |
| D3 | A shared UI component library that silo screens compose from | Blunts D2's main cost — silos should not each rebuild a file uploader |
| D4 | Two GitHub repos: `da-frontend`, `da-backend` | Explicit requirement |
| D5 | FastAPI, not Flask | Almost no valuable code touches Flask (ISO's TOC engine, Textract prescan, BOP's gold calibration and `_DocxEmitter` are plain Python + `requests`), so porting is cheap; Pydantic makes the silo contract enforceable rather than aspirational, which matters with shared team ownership |
| D6 | Platform shares: auth, silo registry, records/history, storage, **LLM client**, run engine, sections/versions, fork | See D7 and D9 |
| D7 | Ingest, OCR, Textract media extraction and docx rendering stay **inside each silo** | Deliberate scope limit — ship sooner, port silos nearly as-is. Extraction deferred until duplication costs something |
| D8 | The LLM client is the one exception to D7 and is shared immediately | Small surface (~1 file), already identical across silos, and the status quo is both a security exposure (committed key) and a reliability bug (unrefreshed expiring tokens). Also the only place a **global** rate cap can live |
| D9 | Run state, progress, checkpoints and resume live in the database; heavy work runs in separate worker processes | Required by the 15+ concurrent target — in-memory run state cannot span processes. Restart-safety and resume come along as a consequence rather than as extra work |
| D10 | Postgres from the dev environment onward; the job queue is `SELECT ... FOR UPDATE SKIP LOCKED` on the same database | Long jobs at low arrival rate do not need Redis or Celery; progress writes land in the same row the API reads, so there is no result backend to reconcile |
| D11 | Documents are durable records users return to | Explicit requirement. Also makes the history table double as the resume mechanism |
| D12 | LDAP authenticates only; every silo is visible to every authenticated user | Explicit choice — no AD group to silo mapping to maintain |
| D13 | Every document is readable by every authenticated user | Explicit choice — favours reuse and handover over privacy of work in progress |
| D14 | A document is editable **only by its owner**. A non-owner who wants to change it forks their own copy; the original is untouched | Explicit requirement. Replaces conflict resolution with a single authorization rule, preserves originals, and attributes every change to a person |
| D15 | Forking is explicit: someone else's document opens read-only with a "Create my own copy" button | Nothing ambiguous about whose document you are editing |
| D16 | Both completed and in-progress documents can be forked | Supports handover of unfinished work |
| D17 | Prompt overrides are **per user**, layered over the silo default | One person's experiment must not change everyone's output. Consequence: generated content varies by author, so D18 is required |
| D18 | The resolved prompt text is snapshotted into `runs.metadata` at generation time | Given D17, a document must record what produced it |
| D19 | `@abbvie-unity/react` is the frontend design system | AbbVie internal tool; inherits theming and accessibility, and makes silo panes visually coherent without effort |
| D20 | Frontend is built and shipped as an nginx container serving static assets | Keeps dev and prod serving identical; DevOps can front it with CloudFront later without a code change |
| D21 | `API_BASE_URL` is read at boot from a runtime config file, not baked at build | Vite bakes env vars into the bundle, which silently forces one image per environment |
| D22 | One backend image, three roles selected by command (`api`, `worker`, `migrate`) | api and worker cannot drift out of sync; one build |
| D23 | Migrations run as a separate one-off command before rollout, never on api startup | Multiple api replicas start simultaneously and would race each other |
| D24 | **Local PC:** SQLite + local filesystem, no containers required. **Dev:** EC2 + docker compose, Postgres as a container. **Production orchestration: deferred** — not designed in this spec | Explicit constraint. Keeping prod undecided costs nothing because the application is orchestrator-agnostic: it is three images plus an env var contract |
| D25 | Local development uses **SQLite** (Python stdlib) and local filesystem storage | No database can be installed on the development machine. SQLite 3.45.3 ships with Python 3.12, supports WAL and `RETURNING` — verified |
| D26 | All dialect-specific SQL is confined to one module | Keeps the eventual Postgres switch a config change rather than a rewrite |
| D27 | Migration order: platform skeleton → LLM client → **BOP** → ISO → CMC warehouse → load test | BOP is half the size yet exercises far more of the platform (pause-for-user, editing, versions, fork, parallel LLM). Validating the contract on the cheaper silo makes ISO's port mechanical |

---

## 3. Architecture

### Processes

```
browser ──HTTPS 443──▶ ingress (ALB prod / nginx dev)
                          │  /*      → frontend:80
                          │  /api/*  → api:8000
                          ▼
              ┌──── api container (N replicas) ────┐
              │  auth · upload · history · sections│
              │  fork · download · status polling  │
              │  NEVER runs a stage                │
              └───────────────┬────────────────────┘
                              │
            ┌─────────────────┴──────────────────┐
            ▼                                    ▼
      ┌───────────┐                      ┌──────────────┐
      │ database  │◀────claim/checkpoint──│   workers    │
      │ runs      │      progress         │ (M replicas) │
      │ sections  │      heartbeat        │ silo stages  │
      │ queue     │                       └──────┬───────┘
      └───────────┘                              │
            ▲                                    │
            └────────── object storage ◀──────────┘
                        runs/<run_id>/{input,output,media}/

externals: Iliad LLM · AWS Textract · CMC warehouse · LDAP · secret store
```

**The load-bearing property: the api never does slow work.** ISO's generate step currently holds an HTTP request open for minutes running Textract and vision calls; BOP holds a thread for up to an hour waiting on a human. Both become "api writes a row, worker picks it up, browser polls".

### Environment matrix

| Concern | Local PC (installs nothing) | Dev on EC2 (docker compose) |
|---|---|---|
| Database | SQLite file `./dev.db`, WAL mode | Postgres container |
| Object storage | local folder `./dev_storage/` | MinIO container, or a real S3 bucket |
| Auth | dev provider, seeded users, any password | LDAP search-then-bind |
| Ingress | uvicorn + Vite dev server | nginx container, port 443 |
| Workers | one process, `python -m worker` | N worker containers, `docker compose up --scale worker=N` |
| LLM / Textract | real endpoints | real endpoints |
| CMC warehouse | stub until credentials exist | real, read-only (phase 5) |

**Not testable on the local PC:** 15-way concurrency. SQLite serialises writers, so local runs prove the logic is correct but not that the claim mechanism holds under contention. That verification happens on EC2 against the Postgres container (phase 6), which is also where the `postgresql` branch of the dialect module first gets exercised — so both dialects are covered before anything reaches production.

Production is deliberately not designed here. The application is three images plus the env var contract in section 11; whichever orchestrator is eventually chosen consumes that unchanged, provided it satisfies the requirements in section 11.1.

---

## 4. Repositories

### `da-backend`

```
platform/
  auth/        LDAP + dev provider, JWT sessions, current-user dependency
  engine/      run state machine, queue claim, stage dispatch, checkpoints,
               resume, heartbeat, reaper
  records/     start_run / attach_output / finish_run / fail_run, history queries
  sections/    section content, revisions, version history, fork
  llm/         Iliad client, credential refresh, retry, global rate limiter
  storage/     object storage interface — S3 and local filesystem adapters
  warehouse/   CMC read-only query layer (phase 5)
  db/          SQLAlchemy models, dialect module, Alembic migrations
  api/         auth, silos, documents, sections, downloads, health
silos/
  iso/         descriptor.py, stages.py, router.py, prompts/, + ported ISO code
  bop/         descriptor.py, stages.py, router.py, prompts/, templates/, gold/
worker/        entrypoint
tests/
Dockerfile
docker-compose.yml
```

### `da-frontend`

```
src/
  shell/       login, nav, doc-type dropdown, history page, document layout
  components/  Dropzone, StepProgress, SectionTree, DocumentEditor,
               VersionDropdown, ResultCard, ReadOnlyBanner
  api/         typed client, polling hook, runtime config loader
  silos/
    iso/       index.ts, IsoUpload, IsoTocRange, IsoProgress, IsoResult
    bop/       index.ts, BopConverter, BopEditor, BopConfig
Dockerfile
```

Discovery is additive on both sides: `silos/*/descriptor.py` and `src/silos/*/index.ts` are found by scan, so no central list needs editing. `ENABLED_SILOS` (comma list) allows shipping a silo dark.

---

## 5. Silo contract

The engine owns **state, progress, checkpointing, resume**. Everything domain-specific stays in the silo.

```python
# silos/iso/descriptor.py
descriptor = SiloDescriptor(
    id="iso",
    label="ISO Applicability Assessment",
    accepts=[FileSpec(extensions=[".pdf"], required=True)],
    stages=["ingest", "await_range", "generate", "build"],
    router=router,                      # optional, silo-specific endpoints
)
```

**Stages are checkpoints, not a framework.** They exist so resume has somewhere to resume from and the progress bar has something to report. A silo that does not care may declare a single stage.

```python
def ingest(run, ctx):
    pdf = ctx.storage.open_input(run)
    garbled = iso_garble.detect(pdf)             # silo's own code, unchanged
    toc = iso_toc.extract(pdf, vision=garbled)   # silo's own 5-level chain
    ctx.progress("Table of contents extracted", pct=25)
    return {"toc": toc, "garbled": garbled}      # checkpointed

def await_range(run, ctx):
    return ctx.pause_for_user()                  # persisted; no thread held
```

`ctx` exposes exactly four platform capabilities: `ctx.llm`, `ctx.storage`, `ctx.progress()`, `ctx.pause_for_user()`.

Whatever a stage returns is checkpointed, so a worker crash or a deploy mid-run resumes at the last completed stage. `ctx.pause_for_user()` replaces BOP's `threading.Event.wait()`: the run parks as `awaiting_user`, the worker is released, and a user action resumes it minutes or weeks later.

Checkpoint granularity is per stage. A restart mid-`generate` replays that stage — for BOP, 15 LLM calls, a few minutes and some spend. Acceptable; finer per-section checkpointing is available if it ever matters.

### Effort split, measured against the existing code

Of ISO's 4,780 lines roughly 1,100 are genuinely ISO-specific (prompts, row extraction, its two-column docx); of BOP's 2,590 roughly 1,050 are BOP-specific (mostly gold calibration and section schemas). So a new silo is roughly a quarter to a third of the work it would be standalone, plus 3–5 thin frontend screens composed from shared components.

---

## 6. Data model

```
users
  id · username · display_name · email · first_seen · last_seen

runs
  id · silo_id · user_id · status · stage · progress_pct · progress_message
  title · started_at · finished_at · duration_ms · error_message
  forked_from_run_id → runs.id (nullable)
  claimed_by · claimed_at · heartbeat_at
  metadata (json — includes the resolved prompt snapshot, per D18)
  indexes: (user_id, started_at) · (silo_id, started_at) · (status) · (title)

run_files
  id · run_id · kind ('input'|'output') · filename · storage_key
  size_bytes · content_type · created_at

run_stage_checkpoints
  run_id · stage · output (json) · completed_at        PK (run_id, stage)

document_sections
  run_id · section_key · content_html · revision
  updated_at · updated_by                             PK (run_id, section_key)

document_section_versions
  run_id · section_key · version_num · content_html · label
  created_at · created_by                             append-only

silo_prompt_overrides
  user_id · silo_id · prompt_key · content · updated_at
  PK (user_id, silo_id, prompt_key)
```

Notes:

- `run_files` rather than columns on `runs`, because a future silo may take several inputs or emit several outputs. Costs nothing now.
- `metadata` is `JSON().with_variant(JSONB(), "postgresql")` so one model definition serves both dialects.
- `document_section_versions` being a table removes BOP's 20-version memory cap and its JSON file spill entirely.
- **Every section save is a synchronous write before the API returns.** Not a background flush. This is what makes an afternoon of editing survive a restart; BOP's current in-memory primary store does not.
- Nothing is ever deleted, so forks may safely reference the original's input `storage_key` rather than duplicating large PDFs.

### Statuses

`queued → running → awaiting_user → queued → running → complete`, plus `failed` from any running state.

---

## 7. API surface

### Platform

```
POST   /api/auth/login                       → session cookie (httpOnly, SameSite)
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/silos                            drives the doc-type dropdown

POST   /api/silos/{silo}/documents           multipart: files + JSON inputs → {id}
GET    /api/documents                        ?user= &silo= &status= &q=
                                                     &sort=date|name|user &order= &page=
GET    /api/documents/{id}                   owner, status, stage, files, lineage
GET    /api/documents/{id}/status            light polling payload
POST   /api/documents/{id}/fork              → {id} of the new copy
POST   /api/documents/{id}/build             resume from awaiting_user
GET    /api/documents/{id}/files/{file_id}   download input or output
GET    /api/healthz                          checks database
```

### Sections (platform-owned, silo-agnostic)

```
GET    /api/documents/{id}/sections
PUT    /api/documents/{id}/sections/{key}              {html, revision}
GET    /api/documents/{id}/sections/{key}/versions
GET    /api/documents/{id}/sections/{key}/versions/{n}
POST   /api/documents/{id}/sections/{key}/restore/{n}
```

`PUT` returns `403 {can_fork: true}` when the caller is not the owner (D14), and `409` when `revision` is stale — the same user in two browser tabs, which is easy to do when runs take minutes.

### Silo-specific

```
POST   /api/silos/iso/documents/{id}/toc-range   {start_idx, end_idx} → resumes run
GET    /api/silos/bop/config/prompts             per-user resolved prompts
POST   /api/silos/bop/config/prompts/{which}
POST   /api/silos/bop/config/prompts/{which}/reset
```

### Data flow — ISO

```
upload PDF → api stores to storage, INSERT run(queued) → 202 {id}
worker claims → ingest (garble detect, TOC extract) → checkpoint
  → awaiting_user  ── browser polls, shows ISO's range picker
POST /toc-range → queued → worker: generate (Textract, media, rows)
  → checkpoint → build (docx) → complete, output stored, row in history
```

### Data flow — BOP

```
upload manual → ingest (text, or vision OCR if scanned)
  → generate (15 LLM calls via the shared rate limiter)
  → review (LLM critique)
  → awaiting_user ── editor; each save writes to the database immediately
POST /build → worker builds docx from current sections → complete
```

Progress is polled every 2 seconds, as BOP already does. At 15 concurrent runs this is a trivial indexed query and it degrades more gracefully than SSE.

### Documents are addressable

Every document has a URL, `/documents/{id}`, and in-progress runs appear in the history list with a "Continue editing" action. Durability on the server is worthless without a route back to it — BOP today keeps `currentJobId` only in a JavaScript variable, so a browser refresh already loses the document even when the server is healthy.

---

## 8. Ownership, forking, lineage

```
Anyone      → read any document
Owner       → edit in place; versions attributed to them
Non-owner   → read-only view + "Create my own copy"; original untouched
```

`POST /api/documents/{id}/fork` produces a new document owned by the caller:

| | Behaviour |
|---|---|
| Section content | copied |
| Stage checkpoints | copied, so the fork continues from the same point |
| Input files | new `run_files` rows pointing at the **same** storage key |
| Version history | not copied; fork starts with one version, "Forked from &lt;original&gt;" |
| Output docx | not copied; the fork has no output until its owner builds |
| Title | `Copy of <original title>`, editable |
| Lineage | `forked_from_run_id` set |

Version history deliberately does not carry over — the fork's history is its own, and the link back is the lineage pointer rather than a merged timeline.

---

## 9. Auth and secrets

**LDAP:** service-account search-then-bind, tolerating varying OU structures. A `users` row is created from LDAP attributes on first login. **No password is ever stored or cached.** Session is a signed JWT in an httpOnly, secure, SameSite cookie, 8-hour expiry — stateless, no session table.

**Dev auth provider:** accepts any password for a small list of seeded usernames. Seeding two or three users is required, not cosmetic — ownership and fork-on-edit cannot be exercised with a single identity. Fails closed: enabled only by explicit env var, never the default.

### Secret fixes carried by this migration

| Problem today | Fix |
|---|---|
| BOP: Iliad API key hardcoded in source (line 63) | env var only; CI secret scanning so it cannot return |
| ISO: AD JWT read once into a cached client, never refreshed | credential provider; refresh on 401, rebuild client, retry once |
| ISO: STS credentials expire, boto3 singletons never refresh | refreshable session; recreate client on `ExpiredToken` |
| Both: `verify=False` + `urllib3.disable_warnings()` | CA bundle baked into the image, explicit `verify=<path>`, warnings re-enabled |
| ISO: CA bundle downloaded from S3 at import time | baked at build time; no network call on import |

Silently-expiring credentials are tolerable in an app restarted between documents. In a continuously running platform they present as intermittent unexplained failures.

---

## 10. Concurrency, throughput, failure

### Rate limiting is required, not optional

BOP fires up to **15 parallel LLM calls per run** (6 sections + 9 operating-procedure phases). Fifteen concurrent BOP runs is **225 simultaneous Iliad requests**. ISO fires one vision call per page with 4 workers, plus parallel Textract per page. Each silo today throttles only itself and knows nothing of the others.

`platform/llm` therefore holds a **global semaphore plus token bucket per provider** (Iliad, Textract), configured by cap, with exponential backoff and jitter on 429 and 5xx. This is the concrete reason the LLM client is shared (D8).

### Failure handling

- **Transient errors** — retried inside `platform/llm`, invisible to the silo.
- **Stage failure** — run marked `failed` with the message; traceback stored, not returned to non-admins. History offers **Retry**, which re-enqueues from the last completed checkpoint.
- **Worker death** — runs carry `claimed_by` and `heartbeat_at`; a reaper re-queues anything whose worker stopped heartbeating. Without this, `SKIP LOCKED` leaves a killed worker's runs stuck forever.
- **SIGTERM** — the worker finishes the current stage, checkpoints, releases the claim, and exits.
- **Uploads** — max size enforced and streamed to storage. ISO currently reads the whole file into memory with no limit; 15 concurrent large-standard uploads alone could exhaust the container.

### Ephemeral filesystems

Containers are replaceable, so **nothing durable is written to local disk**. This is a concrete port task, not a deployment note: ISO currently writes cropped figure/table/formula PNGs to `memry/<session_id>/` and re-reads them later in the same run, and writes vision-extraction JSON to disk to bridge upload and generate. Both move to object storage under `runs/<run_id>/media/`.

---

## 11. Container contract

Three roles from one backend image, selected by command (D22):

| Container | Port | Health | Notes |
|---|---|---|---|
| `frontend` | 80 | `GET /healthz` | nginx + built assets; `API_BASE_URL` read at boot |
| `api` | 8000 | `GET /api/healthz` (checks db) | N replicas; never runs a stage |
| `worker` | none | heartbeat row + process | M replicas; on EC2 scaled manually via `--scale worker=N` |
| `migrate` | none | exit code | one-off, before rollout; api refuses to start on schema mismatch |

### Environment variables

```
Data/storage   DATABASE_URL* · S3_BUCKET · S3_PREFIX · AWS_REGION · MAX_UPLOAD_MB
Auth           LDAP_URL · LDAP_BIND_DN · LDAP_BIND_PASSWORD* · LDAP_BASE_DN
               JWT_SECRET* · JWT_TTL_HOURS · DEV_AUTH_USERS (dev only)
LLM/OCR        ILIAD_BASE_URL · ILIAD_API_KEY* · ILIAD_USER_TOKEN*
               LLM_TEXT_MODEL · LLM_VISION_MODEL
               LLM_MAX_CONCURRENCY · TEXTRACT_MAX_CONCURRENCY
Warehouse      CMC_DSN* · CMC_QUERY_TIMEOUT_S
Runtime        ROLE=api|worker · WORKER_CONCURRENCY · WORKER_HEARTBEAT_S
               STALE_CLAIM_TIMEOUT_S · LOG_LEVEL · CA_BUNDLE_PATH · ENABLED_SILOS
Frontend       API_BASE_URL
```

`*` must come from the secret store, never the repo.

### DevOps provides — dev environment on EC2

EC2 instance with Docker and Compose · TLS certificate for the nginx container · egress to Iliad, LDAPS, CMC and Textract · AWS credentials or an instance role covering `textract:AnalyzeDocument` and, if a real bucket is used instead of MinIO, `s3` read/write · a location for secret values that is not the repo (SSM Parameter Store, or a `.env` file outside version control) · a container registry, or build on the instance.

Postgres, MinIO and nginx all run as compose services on the same instance, so nothing else needs provisioning for dev.

**We hand over:** the three images (or the compose file that builds them), the env var list above, health check paths, and the migration command.

### 11.1 Requirements for any future production orchestrator

Recorded so the eventual production choice is evaluated against real needs rather than made and then discovered:

1. **Graceful shutdown longer than the longest stage.** Stages run for minutes — a BOP generate is 15 LLM calls, an ISO ingest can be a 200-page vision pass. A 30-second default grace period would hard-kill in-flight stages on every deploy, forcing the reaper to re-queue them and wasting the LLM spend already incurred. This is the single most likely thing to be got wrong.
2. **A one-off task or job primitive** for migrations, run to completion before the api rolls.
3. **Secret injection as environment variables** from a store, never baked into images.
4. **AWS access without static keys** — an instance or workload identity.
5. **Independent scaling of api and worker**, ideally driven by pending-run depth, since that is the metric that actually predicts worker starvation.
6. **Managed Postgres** with backups.

---

## 12. Local development

The dialect divergence is confined to one module (D26):

```python
# platform/db/dialect.py — the only dialect-specific code
def claim_next_run(session, worker_id, now):
    if session.bind.dialect.name == "postgresql":
        # SELECT id ... WHERE status='queued' ORDER BY created_at
        #   FOR UPDATE SKIP LOCKED LIMIT 1, then UPDATE
    else:
        # BEGIN IMMEDIATE, then
        # UPDATE runs SET status='running', claimed_by=:w, claimed_at=:t
        #   WHERE id = (SELECT id FROM runs WHERE status='queued'
        #               ORDER BY created_at LIMIT 1) RETURNING id
```

Everything else stays single-source through SQLAlchemy type variants. Expected divergence: roughly 40–60 lines in one file.

Verified on the development machine: Python 3.12.4, SQLite 3.45.3 (WAL confirmed, `RETURNING` supported), Node 22.2.0, Docker 26.1.4 installed.

Locally testable: upload, all stages, checkpointing, resume after killing the worker, editing, versions, fork, history filters, docx output.

---

## 13. Testing

- **Silo contract tests** run automatically against every registered silo — descriptor validates, stages are callable, declared stage names match implementations. A new silo is checked by tests nobody wrote for it.
- **Golden-file docx tests.** `_test_output_baseline.docx` and `_test_output_fixed.docx` already exist in the ISO folder and become fixtures, so the port can be proven not to change output.
- **Recorded LLM fixtures** (cassette style) so CI runs full pipelines without Iliad access or spend.
- **Resume test**: start a run, kill the worker mid-stage, assert it resumes from the last checkpoint.
- **Ownership tests**: non-owner `PUT` returns 403 with `can_fork`; fork produces an independent document; stale `revision` returns 409.
- **Playwright golden path**: log in → pick document type → upload → poll to completion → edit a section → build → download → appears in history with correct attribution.

---

## 14. Phasing

Each phase gets its own implementation plan.

| Phase | Deliverable |
|---|---|
| 1 | Platform skeleton: FastAPI, SQLAlchemy + Alembic, dev auth + LDAP, storage adapters, records, run engine, queue, worker entrypoint, Unity shell, history page. **No silos** — log in, see an empty history |
| 2 | `platform/llm`: Iliad client, credential refresh, global rate limiter, verified against the real gateway |
| 3 | **Port BOP** — exercises pause-for-user, editing, versions, fork, parallel LLM |
| 4 | **Port ISO** — adds mid-pipeline `await_range`, heavy CPU ingest, media to object storage |
| 5 | CMC warehouse layer plus the first concrete prefill and content-sourcing use case |
| 6 | Load test at 15+ concurrent against Postgres; tune worker count and rate caps |

---

## 15. Risks

| Risk | Mitigation |
|---|---|
| Iliad / Textract rate limits at target concurrency | Global limiter (D8); phase 6 load test establishes real caps |
| Textract cost — ISO calls per page, so 15 concurrent 200-page runs is ~3,000 page analyses | Confirm budget before phase 4; cap concurrency |
| GIL contention in workers on PyMuPDF rendering and docx assembly | Worker process count matches cores; workers scale independently of api |
| Local SQLite cannot prove concurrency correctness | Phase 6 against Postgres is mandatory, not optional |
| Memory on large PDFs | Streamed uploads, enforced max size |
| Behaviour drift while porting | Golden-file docx fixtures from the existing ISO outputs |

---

## 16. Deliberately deferred

- **Duplicated vision-OCR fallback and docx helpers** remain duplicated across ISO and BOP (D7). First candidates for extraction into `platform/` when it starts to cost something — likely the second time the same OCR bug is fixed twice.
- **Manifest-driven generic screens.** If the platform reaches five or six silos and the screens turn out nearly identical anyway, D2 can be revisited. Because the shared component library (D3) exists from day one, that would be a refactor rather than a rewrite.
- **Per-section checkpointing** inside `generate`.
- **Excel export of the history table** — a legitimate use for DuckDB/Excel tooling, as an output rather than a store.

---

## 17. Open items

These are genuinely unknown and must be supplied before the phase they block.

| Item | Needed by | Owner |
|---|---|---|
| VPC id, security group id, account id | phase 1 deploy | DevOps |
| Production orchestrator — deliberately deferred; evaluate against section 11.1 | before production | DevOps |
| EC2 instance for the dev environment, with Docker and egress | phase 3 dev deploy | DevOps |
| LDAP server URL, bind DN, base DN | phase 1 LDAP path | DevOps / IT |
| CMC warehouse connection details and driver | phase 5 | Data team |
| Textract cost budget | phase 4 | Owner of the ISO silo |
| Iliad rate limits (documented caps) | phase 2 | Iliad platform team |
