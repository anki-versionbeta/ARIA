# PSA Silo — Parity Record and Open Items

> **Not an implementation plan.** The PSA silo is built and running; this is the record the other
> silos' port docs are, written after the fact so PSA has the same documentation the rest of the
> platform has. Where a checkbox appears it is an open item, not a step in a port.

## Context

PSA (Product Similarity Assessment) is ARIA's fourth silo, absorbing the Cap Colour / similarity
assessment tool that produced the QPP11-04-001-G004-F01 form. It builds an assessment for one
product presentation: which other products made at the same site could be confused with it, and
which cap colours are still free.

It landed on `feat/PSA` after the 2026-08-26 dockerization round, so it is absent from
`origin/feature/iso-capture-parity-port` — the branch the other three silos ship from. That branch
stays behind deliberately; this release excludes PSA.

At the point this document was written PSA had **zero backend tests**, against 356 for bop, 661 for
iso and 172 for mfg_atr. That was the parity gap, and closing it is what `tests/test_psa_*.py`
(14 files, 551 test functions collecting as 668 cases once parametrised) now does.

### Structural facts

| | |
|---|---|
| Backend | `ecs/python/src/api/backend/silos/psa/` — 30 modules, 5,780 lines |
| Frontend | `nodejs/src/silos/psa/` — 20 files, 4 test files (the most of any silo) |
| Stages | `fetch → report`. Two, straight through, no human pause |
| `ACCEPTS` | `[]` — nothing is uploaded; a run starts from a program code and a Smartsheet row |
| Storage prefix | `psa`, with `reports/` and `images/`. No `input/` folder |
| Own routes | `POST /runs`, `GET /runs/{id}/result`, plus 14 standalone screen endpoints |

**PSA is mfg_atr's structural twin** — near-identical size, identifier-driven, `ACCEPTS = []`, its
own `/runs` route because the platform's upload endpoint would shadow `/documents`. That is why
`test_mfg_atr_plumbing.py` was the template for `test_psa_plumbing.py`.

### Decisions taken (confirmed with the user)

| Decision | Date | Consequence |
|---|---|---|
| A job is the REPORT, and nothing else | 2026-08-22 | Clicking "Generate PSA report" is what creates a history entry. The cap-colour recommendation is the screen's instant, unrecorded preview |
| The `confirm` pause is removed | 2026-08-22 | Reverses the four-stage design of 2026-08-21. **Nothing now records which cap colour a person chose, or that they chose it** — the recommendation is advisory. `git log` has the pause if it comes back |
| No supplier is named by default | 2026-08-21 | `vendor=None` means EVERY supplier. At assessment time the cap is often not yet tooled, so "no supplier named" means the supplier is still open |
| Clinical presentations are out of scope | 2026-07-17 | `scope.EXCLUDED_BATCH_TYPES`. Applied per presentation on `batch_type`, not per program on `status` |

## Architecture

### The most important design decision: capture once, rebuild deterministically

`build_db.main()` deletes and recreates the whole database, and every recommend/report/refresh used
to do that against the live sheet — so two requests raced over one file and two runs of "the same"
report could legitimately differ because the sheet moved underneath them.

Instead, following mfg_atr's stated reasoning, a run reads the sheet **once** in `fetch` and every
later stage rebuilds from those bytes:

    fetch     capture_bytes()   one REST read   →  ctx.storage.put_media(run, SNAPSHOT, raw)
    report    rebuild(raw)      no network      →  the .docx

Two properties follow. A run's recommendation and its report necessarily describe the same
catalogue, which they previously did not. And the snapshot is the audit record of what Smartsheet
said when the assessment was made — which is what a GxP reviewer would ask for.

`snapshot.capture_bytes` canonicalises with `sort_keys=True`, so the same sheet content always
produces the same bytes and "did the catalogue change?" is a byte comparison.

### The product photo travels with the snapshot

Captured in `fetch` alongside the sheet, as its own run-media object, and embedded by `report` from
there. A second trip at report time could return a different image than the assessment was based
on. Only the subject row's photo is fetched — a run assesses one presentation and the sheet has
~37, so downloading the other 36 to embed one would be waste. A row with no photo is normal:
the report renders a blank photo cell and `verify.py` accepts it.

### Why a Smartsheet `SystemExit` must not escape

`ingest_smartsheet_api` reports a 401, an HTTP error or an unreachable host by raising
`SystemExit` — reasonable for the CLI it was written for, dangerous in a worker. `SystemExit`
derives from `BaseException`, **not** `Exception`, so a worker that wraps a stage in
`except Exception` to mark the run failed would not catch it: the exception would unwind the worker
itself and take every other silo's queued runs down with it.

`silo.py`'s `fetch` converts it to `RuntimeError`; `engines.py` converts it on the three router
paths. `test_psa_ingest.py` pins that each failure really does raise `SystemExit` and that it is not
an `Exception`, so the reason the conversion exists stays on record.

### Platform additions — exactly one

Everything else is silo-level. The single platform change:

**`da_platform/credentials.py` gains `SmartsheetCredentials` and `smartsheet_credentials()`** —
resolving `SMARTSHEET_ACCESS_TOKEN` and `SMARTSHEET_SHEET_ID`, raising `MissingCredential` when
either is absent, with a redacting `__repr__`. Modelled on `WarehouseCredentials`.

This exists because `tests/test_silo_isolation.py` fails the build on `os.environ` / `os.getenv` in
any silo file, with the guidance *"receive configuration through the stage context"*. The token
stays in the platform's configuration; the silo never reads the environment. `psa/config.py` holds
the shape and repo-relative defaults, and `psa/aria.py` builds the real one.

**The token authenticates as a person**, not a service, and can read every sheet that person can
see. It is the one credential in the platform whose scope is wider than the feature using it, which
is why `PsaConfig.__repr__` redacts it and why `test_psa_config.py` treats that as a security
property rather than a formatting detail.

## File structure

| File | Responsibility |
|---|---|
| `silo.py` | `LABEL` / `ACCEPTS` / `STAGES`, the two stages, the `PCT` progress ladder |
| `router.py` | The 14 screen endpoints; installs the host config on the first request |
| `runs.py` | `POST /runs` and `GET /runs/{id}/result` — the only routes needing `da_platform` |
| `location.py` | `STORAGE_PREFIX` / `STORAGE_FOLDERS`, re-exported by `silo.py` for the registry |
| `snapshot.py` | Capture once, rebuild deterministically — the core design |
| `engines.py` | UI-agnostic data layer over the pipeline, plus the run lock |
| `workflow.py` | The document-driven path: identify the product, extract, populate, verify |
| `risk.py` | The mix-up risk engine and the Part D/E draft |
| `cap_recommend.py`, `cap_colors.py`, `palette.py`, `cap_queries.py` | The cap-colour recommendation and its catalogue |
| `populate_template.py`, `cap_export.py` | The two documents |
| `build_db.py`, `assets/schema.sql` | The derived SQLite catalogue and its seed |
| `config.py`, `paths.py`, `aria.py` | Configuration, resolved at call time |
| `asset_store.py`, `storage/` | The shipped assets and the object-store port |
| `downloads.py` | Opaque id → generated file, so no filesystem path reaches a client |
| `verify.py` | The post-generation self-check |

### Frontend — already the richest of the four silos

`nodejs/src/silos/psa/` has an `api/` layer, a `components/` folder and `test-support.tsx`, none of
which bop, iso or mfg_atr have; they consume the shared `@/api/http` directly. Registration is one
file (`index.ts`), discovered by `registry.ts`'s eager glob, and the routes are generic
(`silos.$siloId.new.tsx`) — so no platform file changes to add a silo.

## Verification

Run from `ecs/python`. There is no pytest config anywhere in the repo; `tests/conftest.py` sets
every environment variable at import time, so `PYTHONPATH=src` is the only thing needed.

```bash
# PSA's own suite — 668 tests across 14 files
PYTHONPATH=src python -m pytest tests/test_psa_*.py -q

# the whole backend suite
PYTHONPATH=src python -m pytest tests/ -q

# the other silos must be unaffected
PYTHONPATH=src python -m pytest tests/test_bop*.py tests/test_iso*.py \
  tests/test_mfg_atr*.py tests/test_silo_isolation.py -q

# frontend (187 tests, including PSA's 4 files)
cd ../../nodejs && npx vitest run
```

**Known-red, and not PSA's:** 9 failures and 109 collection errors pre-date this work. All are
missing local dev packages — `pypdf`, `reportlab` and `oracledb`, pinned in `ecs/requirements.txt`
but blocked at the corporate pypi proxy — plus three tests whose files the reference branch has
newer versions of. **PSA imports none of those packages**, so its suite runs green regardless.

## FLAGs

**Must resolve before PSA is enabled in a deployed environment**

- [ ] **`api.smartsheet.com` has no egress path.** Every other target in `OCEAN_ENV_VARS.md` §10 is
      in-VPC, an AWS endpoint or `abbvienet.com`. PSA is the platform's **first public-internet
      dependency**, and tasks run in private subnets with `AssignPublicIp: DISABLED` and no NAT
      gateway — so as configured, `fetch` cannot reach the sheet at all. Needs a NAT gateway or a
      corporate-proxy path (`HTTPS_PROXY`). This is an infrastructure decision, not a code change.
- [ ] **`SMARTSHEET_ACCESS_TOKEN` and `SMARTSHEET_SHEET_ID` are not in any task definition.** Now
      documented in `.env.example`, `OCEAN_ENV_VARS.md` §1 and §2, and `docker-compose.yml`, but
      documenting is not provisioning. A missing token is **silent**: the picker is simply empty,
      and `GET /api/silos/psa/health` reporting `smartsheet_live: false` is the only place that
      says why.

**Open items, no user-facing impact yet**

- [ ] **`aria.py:_scratch_dir()` never tears down.** `tempfile.mkdtemp(prefix="psa-run-")` once per
      process, holding `psa.db`, `output/` and `uploads/`, with no cleanup. iso and mfg_atr use a
      `workspace.py` that deletes on the way out "including on failure, so a worker that renders
      hundreds of reports does not fill its disk". PSA's disk grows over a worker's lifetime. Left
      alone here because it is a behaviour change.
- [ ] **`asset_store.py` reads assets from local disk, not `ctx.assets`.** Every other silo reads
      its templates from object storage (`bop/silo.py:122`). `asset_store.py`'s own docstring says
      *"At Stage C the three functions delegate to `ctx.assets`"* — that delegation was never done.
      Consequence: `PSA_template.docx` and `cap_palette.csv` are baked into the image and cannot be
      swapped per environment. Benign today, and it is why an S3-less stack can still produce a PSA
      report.
- [ ] **`identify()`'s unreadable guard is path-length dependent.** It strips only the
      `"(could not read"` prefix before measuring, so a long enough file path in a failure message
      can carry the remainder past the 20-character floor. Benign — it falls through to the
      authoritative filename code — and characterised in
      `test_psa_workflow.py::test_a_long_failure_message_still_reaches_the_filename_code`.
- [ ] **`risk.py` tuning constants are provisional.** `GRADE_THRESHOLDS`, `RISK_YES_LEVELS` and
      `cap_recommend.CLOSE_DELTA_E` are calibrated against three signed baselines and marked
      SME-tunable in the source. `test_psa_risk.py` pins them, so a retune is a deliberate change
      with a test to update.
- [ ] **`late_stage_sql` is a provisional proxy.** QPP11-04-001-G004 §1.3 defines a late-stage
      pipeline product as one that "has completed primary stability lots manufacturing with
      approved commercial product image". The Smartsheet exposes no such milestone, so the
      predicate is *in scope AND not discontinued* — a superset of the real definition. Tighten it
      once the data owners surface that signal.

**Accepted deliberately**

- **Nothing records which cap colour a person chose.** The direct consequence of the 2026-08-22
  decision to make a job the report and nothing else. Stated plainly in `silo.py`'s docstring.
- **PSA owns a private SQLite database.** No other silo does. It is a *derived* artefact, dropped
  and recreated from the run's own snapshot on every run, never touching `DATABASE_URL`, so
  `deploy/sql/` needs no PSA migration.
- **`ACCEPTS = []` means "no upload RESTRICTION", not "no upload".** `Silo.accepts_filename()`
  returns True for an empty list, so a file POSTed to `/silos/psa/documents` WOULD create a run —
  one with no program code. `_params()` therefore fails such a run loudly rather than half-running
  it, and `test_psa_plumbing.py` pins both the raise and the message that explains it.

## Critical files

**Source of truth**
- `ecs/python/src/api/backend/silos/psa/silo.py` — the contract and the two stages
- `ecs/python/src/api/backend/silos/psa/snapshot.py` — why the design is what it is

**Platform files PSA depends on**
- `da_platform/credentials.py:229-269` — `SmartsheetCredentials`, the one platform addition
- `da_platform/silo_registry.py` — folder-scan discovery; `ROOT_PACKAGE = "da_silos"`
- `api/app.py` — mounts each silo's router behind `_module_guard(silo.id)`
- `da_platform/auth/access.py:49` — `known_module_ids()` is discovery-driven, so PSA needs no
  registration in any hardcoded list

**Reuse, do not reinvent**
- `tests/test_mfg_atr_plumbing.py` — the template for a `ACCEPTS = []` silo's plumbing tests,
  including why the app fixture must mount `uploads` in `app.py`'s registration order
- `tests/test_iso_silo.py` — the delegate-stubbing pattern with a call-order recorder
- `tests/conftest.py:grant_every_module` — every module-gated route answers 403 without it
