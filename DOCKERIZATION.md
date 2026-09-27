# ARIA — Dockerization for Ocean

Working state document. Started 2026-08-20, current as of 2026-08-26.
**Read this first when resuming.**

| | |
|---|---|
| Branch | `feature/dockerization`, tracking `origin/feature/dockerization` |
| Based on | `origin/dev` — merged 2026-08-26 to pick up the ECS restructure (c67bf1a) |
| Target | AWS Fargate behind an ALB, via Ocean's `container-ecs-alb.yaml` |
| Status | **Images built and proven end to end on the restructured layout.** Awaiting Ocean-side answers. |
| Handover to Ocean | [`OCEAN_ENV_VARS.md`](OCEAN_ENV_VARS.md) — everything the platform must provide |

No PR opened, by request. Branch pushed only.

### The 2026-08-26 restructure

`dev` renamed and reorganised both apps, so everything below uses the new paths:

| Was | Is |
|---|---|
| `da-backend/` | **`ecs/`** — `requirements.txt` at its root, source under `python/` |
| `da-backend/da_platform/` | `ecs/python/src/api/backend/da_platform/` |
| `da-backend/certs/`, `scripts/` | `ecs/python/certs/`, `ecs/python/scripts/` |
| `da-frontend/` | **`nodejs/`** — internally unchanged |
| `da_platform.main:app` | **`api.app:app`** |
| `python -m worker` | **`python -m api.backend.worker`** |

Our `.env` now belongs at `ecs/python/.env` and `.npmrc` at `nodejs/.npmrc`.

## 0. Resuming from cold

Images are torn down between sessions; the `aria-pgdata` volume is kept, so the
local admin role and previously generated runs survive.

```bash
cd ARIA
export MSYS_NO_PATHCONV=1        # required on Git Bash for Windows -- see §6

# 1. Has dev moved? If behind, rebase before doing anything else.
git fetch origin && git log --oneline HEAD..origin/dev

# 2. Rebuild. Build with the stack DOWN -- WSL has crashed otherwise (§8).
docker build --platform linux/amd64 -t aria-backend:dev  ecs/
docker build --platform linux/amd64 -t aria-frontend:dev nodejs/

# 3. Bring the stack up: exact commands in ecs/README.Docker.md,
#    "Full stack against a real Postgres". Four containers on a user-defined
#    network, with CMCDW_DSN rewritten to the FQDN form.

# 4. Verify: the checks in §6. Then http://localhost:8088
```

**Expect S3 to fail with `ExpiredToken`** on any session more than a few hours after
the `.env` was last refreshed — the AWS keys in it are short-lived `ASIA...` STS
credentials. Refresh them, then **recreate `aria-api` and `aria-worker`**:
`--env-file` values are captured at container creation, so a running container keeps
the stale ones.

Frontend rebuilds reuse cached layers and finish in seconds. To genuinely re-verify
`npm ci`, the Artifactory token and `tsc`, use `--no-cache` (~4 min).

## 1. Companion documents

| Document | Contents |
|---|---|
| [`OCEAN_ENV_VARS.md`](OCEAN_ENV_VARS.md) | **The handover.** Env vars, secrets, IAM, egress, health checks, failure modes. Give this to the Ocean team. |
| [`ecs/README.Docker.md`](ecs/README.Docker.md) | Building and running the backend image |
| [`nodejs/README.Docker.md`](nodejs/README.Docker.md) | Building and running the frontend image |
| [`docker-compose.yml`](docker-compose.yml) | Local four-service stack. **Written but never executed** — see §8. |

## 2. Working constraints

- **The requester did not write ARIA.** Raise findings about application code only
  when they directly block containerization. Everything recorded here meets that bar.
- **Scope is Docker artifacts plus a change list.** We do not author Ocean's
  `infrastructure/` CloudFormation, `samconfig.toml`, or `.ocean.yaml`. Requirements
  are handed over in `OCEAN_ENV_VARS.md`.
- **Repo split is out of scope.** ARIA stays one repo with `ecs/` and
  `nodejs/`.
- **Ownership boundary:** anything that changes the Dockerfile, entrypoint or image
  contents is ours. Anything else is flagged once, with an owner named, and handed
  over.

## 3. Reference material

| What | Where |
|---|---|
| Ocean scaffold from the platform team | `C:\Users\PALANVX6\Documents\SampleOcean` |
| PDF Redaction's **real** shipped Dockerfiles | reviewed via screenshots, 2026-08-20 |
| ARIA's design spec | `docs/superpowers/specs/2026-08-04-unified-document-authoring-platform-design.md` |
| **The existing non-container deployment** | `deploy/` — only on `dev`, not `main` |

`SampleOcean` is a *scaffold*, not PDF Redaction's shipped output. Where they
disagree, the shipped Dockerfiles win. Three corrections the real files forced:

1. **`public.ecr.aws` is not mandatory** — the shipped files use plain Docker Hub.
2. **Port 8080 is not mandatory** — shipped frontend uses 3000, backend 5001.
   `ContainerPort` is a per-app parameter. We chose 8080 (the template default).
3. **Custom `HealthCheckPath` is normal** — the shipped frontend uses `/healthz`.

### `deploy/` — the current production deployment (dev branch only)

Five systemd units on a dev EC2 (10.224.134.56), documented as of 2026-08-12:

| Unit | What it runs |
|---|---|
| `aria-api.service` | `python -m uvicorn da_platform.main:app --host 127.0.0.1 --port 8000` — **no `--workers`** |
| `aria-web.service` | the **Vite dev server** on 0.0.0.0:443 with TLS from env-named files |
| `aria-worker@1..5` | five worker processes, `TimeoutStopSec=300` |
| `aria-redirect.service` | a Node script redirecting :80 → :443 |

Three things from its README that directly shaped our work:

- *"The frontend is a Vite **dev** server ... replace with `vite build` behind
  **nginx**."* — the app team's own stated intent, matching spec decision D20.
- *"`DA_ENV=local` is the only reason cookies work over plain http. Changing it sets
  `cookie_secure=True` and every login breaks until the site is served over https."*
- *"`JWT_SECRET` is unset, so sessions are signed with the repo's well-known dev
  constant. Set a real one — it logs everyone out, so do it off-hours."*

`deploy/sql/` holds three migrations. See §7 for why `001` must not reach Ocean.

**Note:** those systemd units were written before the 2026-08-26 restructure and
still reference `da-backend`, `da_platform.main:app` and `python -m worker`. They are
now stale — see §7.6.

## 4. Files we created

```
ecs/      Dockerfile  docker-entrypoint.sh  .dockerignore  .gitattributes
          README.Docker.md
nodejs/   Dockerfile  nginx.conf.template  docker-entrypoint.sh  .dockerignore
          .gitattributes  README.Docker.md  certs/ca-bundle.pem
root/     docker-compose.yml  DOCKERIZATION.md  OCEAN_ENV_VARS.md
```

The backend Dockerfile sits at `ecs/`, **not** `ecs/python/`, because `ecs/` is the
only workable build context: `requirements.txt` is at `ecs/requirements.txt` while
the source is under `ecs/python/`, and `COPY` cannot reach above the context.

| Image | Base | Size |
|---|---|---|
| `aria-backend` | `python:3.12-slim` | 365 MB |
| `aria-frontend` | `nginx:1.27-alpine` | 51.6 MB |

Both `linux/amd64`, non-root, plain HTTP on 8080, logging to stdout.

## 5. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **One backend image, two roles** | API and worker share all of `da_platform` + `silos`. Matches ARIA spec D22. |
| D2 | **Role from a `ROLE` env var**, not a command override | Ocean's template injects `Environment` but never overrides `Command`. An explicit arg is also honoured. |
| D3 | **No `CMD`** | `CMD ["api"]` would always occupy `$1`, making `ROLE` unreachable. |
| D4 | **Debian slim, not Alpine** (backend) | PyMuPDF, Pillow, pypdfium2 ship *manylinux* (glibc) wheels; Alpine forces a MuPDF source build. |
| D5 | **Keep `psycopg2`, compile it** | Avoids editing `requirements.txt`. `build-essential`+`gcc`+`libpq-dev` in the builder, `libpq5` at runtime. |
| D6 | **`pip --cert` with the repo's CA bundle** | The proxy re-signs TLS; a stock python image fails every index request. **No credential needed.** |
| D7 | **`UVICORN_WORKERS=1`** | With more, uvicorn's supervisor restarts dead children forever — an import failure leaves a container ECS still reports `RUNNING` while serving nothing. Verified: 147 tracebacks, still "Up". |
| D8 | **`exec` in both entrypoints** | App becomes PID 1 so SIGTERM reaches it. Critical for the worker's own handler. |
| D9 | **nginx for the frontend, proxying `/api`** | Ocean's decision, spec D20, and `deploy/README.md`'s own Known gaps. The proxy is required: login uses an httpOnly cookie and the backend has **no CORS middleware**, so it must be same-origin. |
| D10 | **`STORAGE_DIR=/tmp/aria-storage`** | `settings.py`'s default is inside root-owned `/app`; a non-root container cannot create it and dies at boot. |
| D11 | **`.mjs` → `application/javascript`** | nginx's stock `mime.types` has no `.mjs` entry; the pdf.js worker was served as `octet-stream` and browsers refuse to execute it as a module. |
| D12 | **nginx `resolver` from `/etc/resolv.conf`** | nginx resolves a literal `proxy_pass` host once at startup and caches it forever; an internal ALB's IPs rotate, causing later intermittent 502s. |
| D13 | **`NPM_TOKEN` build arg**, not a `.npmrc` in the repo | `.npmrc` is gitignored, so a CI checkout has none. The Dockerfile writes it from a build arg, so CI only injects a secret. |
| D14 | **Copy `python/` as one tree and preserve its depth** — do not flatten | `settings.py` derives `PROJECT_ROOT` from `parents[4]` and `BACKEND_ROOT` from `parents[1]`. Flattening the packages into `/app` the way the pre-restructure image did would leave `parents[4]` running off the filesystem root and raise `IndexError` at import, before the app object exists. |

Non-obvious invariant worth preserving. `settings.py` now derives **two** anchors
from its own file location:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[4]   # .env, certs, dev.db, dev_storage
BACKEND_ROOT = Path(__file__).resolve().parents[1]   # silos
```

Copying `ecs/python/` to `/app` puts `settings.py` at
`/app/src/api/backend/da_platform/settings.py`, which makes `PROJECT_ROOT=/app` and
`BACKEND_ROOT=/app/src/api/backend` — both correct, so `CA_BUNDLE_PATH` and
`SILOS_DIR` resolve. Verified in the image.

The consequence for any future reshuffle: **the depth matters, not just the paths.**
`parents[4]` needs four directory levels above `da_platform/`, so the image cannot
flatten the tree.

## 6. Verified — 2026-08-21, re-verified 2026-08-25 and 2026-08-26

All on `linux/amd64`, Docker 26.1.4.

**Post-restructure round, 2026-08-26.** Both images rebuilt on the new layout and the
full stack brought up. Backend: `PROJECT_ROOT=/app`, `BACKEND_ROOT=/app/src/api/backend`,
`silos_dir` exists, `/api/healthz` 200, `GET /` 404, all three silos mounted (now
logged by `api.app`), worker PID 1 is `python -m api.backend.worker`, graceful stop
exit 0, fail-fast on missing `JWT_SECRET` exit 1. `python -m scripts.check_iliad`
still works, confirming `PYTHONPATH=/app/src:/app`. Frontend: all routes, `/config.json`
as JSON, `.mjs` MIME correct, `/api` proxy 200, and the `pdf.worker` asset hash was
unchanged, so the rename did not alter the build output. No `.env` anywhere in either
image. Postgres, Oracle, LDAPS and Iliad all reachable; S3 failed on `ExpiredToken`
from a stale `.env` only.

**Pre-push round, 2026-08-25.** Both images rebuilt from nothing — backend full
`pip install` with `psycopg2` compiled (2m53s), frontend with `--no-cache` (3m48s,
325 packages, 1668 modules, `tsc` clean). Asset hashes came out **identical** to the
2026-08-21 build, so the output is reproducible. Every check below passed, plus all
five external dependencies. The only failure was S3 returning `ExpiredToken` on the
stale `.env`; refreshing the credentials and recreating the containers cleared it,
and read/put/get/delete all passed. Worker log: 0 errors.

**Image and process hygiene**
- Both images build; `psycopg2` compiles from source
- Non-root: backend `uid=1000(appuser)`, frontend `uid=101(nginx)`
- Application is **PID 1** in all three roles
- Graceful shutdown: **exit 0** in 2–3s for api, worker and nginx
- Missing `JWT_SECRET` **fails fast** — exit 1, error printed once
- **No secrets in either image** — `.env`, `.venv`, `tests/`, `.npmrc`,
  `node_modules` all absent. Adding `.env` to the context produced an *identical
  image ID*, proving `.dockerignore` kept it out entirely.

**Endpoints**
- `GET /api/healthz` → 200 `{"status":"ok","database":"ok"}`
- `GET /` on the backend → **404** (why `HealthCheckPath` must be overridden)
- Frontend `/healthz` → 200; `/config.json` → `application/json` + `no-store`;
  SPA fallback returns `index.html` for every router path; a missing asset 404s
- Correct MIME for `.js`, `.css`, `.woff2`, `.mjs`; gzip working (945 KB → 341 KB)
- **3 MB upload through nginx → 401 from the backend, not 413** (default limit is 1 MB)

**Every external dependency, from inside the containers**
- **PostgreSQL** via compiled `psycopg2` over TLS — real RDS *and* local Postgres 16
- **S3** list, put, get, delete
- **Iliad** text chat, vision chat, RAG sources
- **Oracle CMC warehouse** — thin mode, no Instant Client
- **LDAPS** — TLS 1.3, cert validated against our bundled CA, issuer
  `AbbVie Global Sub CA01`; **a real interactive login succeeded**
- **SMTP** not yet exercised

**Full application behaviour** (nginx + api + worker + Postgres)
- Real AD login through the proxy; httpOnly cookie survives the hop
- `create_all()` built the whole schema — 9 tables including the new
  `access_requests` and `user_module_access`, so **a fresh database needs no SQL
  migrations**
- **Admin approval flow**: request → approve → `user_module_access` row created
- **Document generation across all three silos, zero errors:**

| Silo | Input | Output | Duration |
|---|---|---|---|
| BOP | `Helix_Manual.pdf` — **37 MB** | `BOP_Helix_Manual.docx` | ~7 min |
| ISO | `ISO+15223-1-2021.pdf` (3.8 MB) | `ISO_15223-12021_Assessment.docx` (5.2 MB) | ~66 s |
| ATR/MFGR | Oracle warehouse | `.pdf` + `.docx` + `_edited` variants | — |

All landed in S3 under the correct per-silo prefixes. The ATR run also proved the
edit-and-rebuild cycle.

**Two bugs found only by running it** — `STORAGE_DIR` crashing the container at
boot, and the `.mjs` MIME type breaking the PDF preview. Neither would have
surfaced from endpoint testing.

### Local test recipe

`MSYS_NO_PATHCONV=1` is **required** on Git Bash for Windows, or MSYS rewrites
`/tmp/storage` into a Windows path and the container fails with
`PermissionError: 'C:'`.

```bash
export MSYS_NO_PATHCONV=1

# Self-contained: no credentials, no external dependencies.
docker run -d --name aria-api -p 18080:8080 \
  -e DA_ENV=local -e JWT_SECRET=local-only \
  -e DATABASE_URL=sqlite:////tmp/aria.db \
  aria-backend:dev
curl http://localhost:18080/api/healthz

# Worker role
docker run -d --name aria-worker -e ROLE=worker \
  -e DA_ENV=local -e JWT_SECRET=local-only \
  -e DATABASE_URL=sqlite:////tmp/aria.db \
  aria-backend:dev
```

The full four-container stack is in `docker-compose.yml`. Because `docker compose`
is unavailable on this workstation (§8), it was brought up with plain `docker run` —
`db → api → worker → web` on a user-defined network, with `CMCDW_DSN` rewritten to
the FQDN form. The backend `README.Docker.md` has the exact commands.

Note: an unrecognised role is executed verbatim, so the image doubles as a
diagnostic shell — `docker run --rm -it aria-backend:dev sh`.

`docker --env-file` does **not** strip quotes, unlike ARIA's own loader
(`settings.py:48`). Keep `.env` values unquoted.

## 7. Open items

### Not ours — flagged and handed off

These are recorded because we found them. **Neither changes anything about the
Dockerfiles.** Both are detailed in `OCEAN_ENV_VARS.md`.

1. **`deploy/sql/001_worker_allowlist_guard.sql` must not reach the Ocean
   database.** A trigger on `runs` rejects claims from hosts outside an allowlist
   containing only the dev EC2's hostname prefix. Container hostnames are random
   task IDs, and ECS does not support the `hostname` field in `awsvpc` mode.

   Failure shape is the worst available: `worker/main.py:96` catches the exception,
   logs it, sleeps 2s and retries forever. The task stays `RUNNING`, has no port to
   health-check, emits ~43,000 tracebacks per worker per day, and **every run sits
   in `queued` forever**. No alarm fires.

   Ocean's `migrate.sh` runs `find sql/ -name "*.sql" | sort` — *all* files — so it
   would be applied automatically. Must be excluded deliberately. Note the
   credential-expiry problem it guards against **cannot occur on Fargate**, because
   the task role renews itself.

   *Owner:* deployer / DBA.

2. **No base-schema SQL.** `create_all()` runs only when `DA_ENV=local`, and
   `deploy/sql/002` and `003` are incremental (they `ALTER TABLE users`). A fresh
   database comes up empty and `/api/healthz` **still returns 200**, because it only
   runs `SELECT 1`. The task goes healthy, joins the ALB, and every real query
   fails.

   Only part touching us: we recommend that health-check path, so the handover says
   plainly that a green check does not imply a populated schema. The entrypoint has
   a `ROLE=migrate` stub that exits 1 with an explanation — a hook, not a commitment.

   *Owners:* ARIA developers (schema definition — already planned per
   `db/session.py:44`); deployer (running it).

3. **`CMCDW_DSN` uses the bare hostname `uq00604p`**, which no container can
   resolve. Docker's embedded DNS sets `options ndots:0`, which suppresses
   search-suffix expansion, so a search domain is not a reliable fix. Verified: bare
   name fails, `uq00604p.abbvienet.com` → 10.72.24.196. *Owner:* whoever sets the
   task-definition environment. One-line change.

4. **A registry service account** for the pipeline build. We built with a personal
   JFrog token; CI must not depend on an individual's AD account.

5. **`GET /docs` returns 200** — FastAPI's Swagger UI, unauthenticated, along with
   `/redoc` and `/openapi.json`. Today the API binds `127.0.0.1:8000` and the Vite
   dev server proxies only `/api`, so these are unreachable from outside. In Ocean's
   model the backend gets **its own internal ALB**, making them reachable within the
   VPC — a change in exposure created by the deployment topology, not by the image.

   Verified that **our frontend does not expose them**: `/docs` through nginx returns
   `index.html` with zero Swagger content, because only `/api/` is proxied. The ALB
   is also `Scheme: internal`.

   Fix if wanted is an app-side one-liner —
   `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` outside local.
   *Owners:* ARIA developers to decide, Ocean's security review to say whether it
   matters. Does not block anything; the code is unchanged from what already runs.

6. **`deploy/` was not updated for the restructure** (noticed 2026-08-26). The
   systemd units still carry `WorkingDirectory=.../da-backend`,
   `PYTHONPATH=.../da-backend`, `ExecStart=... -m uvicorn da_platform.main:app` and
   `ExecStart=... -m worker`. None of those paths or modules exist any more, so the
   dev EC2's services will fail on the next `git pull` and restart.

   Nothing to do with the containers — flagged because it is a direct consequence of
   the restructure and easy to miss. *Owner:* Arjun / whoever maintains the EC2.

### Ours

7. **Backend restructure — DONE 2026-08-26.** Landed on `dev` as `9240192 Move the
   app onto the ECS layout`, merged into this branch and adapted. What actually
   changed on our side: the Dockerfile moved to `ecs/`, one `COPY python/ ./`
   replaced five separate copies, `PYTHONPATH` became `/app/src:/app`, `SILOS_DIR`
   moved down, the uvicorn target became `api.app:app`, the worker became
   `python -m api.backend.worker`, and every `.dockerignore` pattern needed a leading
   `**/` — a bare `.env` matched only `ecs/.env` and would have MISSED
   `ecs/python/.env`, silently breaking the control that keeps credentials out of the
   image.
8. **`docker-compose.yml` is unvalidated** (§8). Needs someone whose Docker install
   has readable `cli-plugins`.
9. **SMTP delivery untested** — approving a request emails via `notify.py`. Never
   exercised; the code is best-effort so a failure should not fail the approval.
   Adds `smtp.abbvienet.com:25` to the egress list.

## 8. Workstation limitations worth knowing

Not ARIA problems, but they shaped how things were tested and cost real time.

- **`C:\Program Files\Docker\cli-plugins` is unreadable**, so **`docker compose`
  and `buildx` are both unavailable**. `docker-compose.exe` on PATH is only a shim
  that calls `docker compose`. Consequences: the compose file has never been
  executed, and BuildKit secret mounts could not be used (hence the `NPM_TOKEN`
  build arg).
- **Docker Desktop's WSL distro terminated abruptly twice**, both times during a
  frontend build. No runaway process found; the daemon and both distros were healthy
  afterwards. Likely memory pressure — there is no `.wslconfig`, so WSL takes up to
  50% of host RAM. Mitigation: do not run builds with the full stack up. A
  `.wslconfig` capping `memory`/`processors` and enabling `sparseVhd` is the
  standard fix.
- `fnms-docker-monitor.exe` (Flexera's Docker inventory agent) is running and is a
  plausible "external entity" per the crash dialog.
- Housekeeping: build iterations accumulate dangling images fast. `docker image
  prune -f` and removing stopped containers reclaimed ~3.4 GB. **Never prune
  volumes** — `aria-pgdata` holds the local test data.

### Browser caching gotcha

`/assets/` is served with `Cache-Control: public, max-age=31536000, immutable`,
correct for fingerprinted files. But it means **changing a header on an
already-served asset does not reach a browser that has cached it** — the filename
never changes, so there is nothing to invalidate. The `.mjs` MIME fix required
DevTools' "Disable cache" or an incognito window; a hard reload was **not** enough,
because the pdf.js worker is fetched by a runtime dynamic `import()` rather than
from the HTML, and hard reload does not bypass the cache for those.

## 9. Next steps

Branch pushed 2026-08-25 and the team notified by email — Arjun (dev), Nitish
(deployment / Ocean), Krunalkumar (FYI). Arjun's ECS restructure landed 2026-08-26
and has been merged and adapted. **Waiting on Ocean-side answers; nothing further to
build until they arrive.**

Open questions put to Nitish:

1. **Can Ocean deploy an ECS service with no ALB?** The blocking one — the worker
   listens on no port. If not, the fallback is a second container in the API task,
   which is a different shape.
2. **Confirm `deploy/sql/001` will be excluded** from the Ocean database (§7.1).
3. **Will the backend task definition gain `Environment` and `Secrets` blocks**, and
   is he aware the **execution role** lacks `secretsmanager` + `kms:Decrypt`? See
   `OCEAN_ENV_VARS.md` §1.
4. **Confirm the Dockerfile path and build context** we chose. The restructure has
   landed and we settled on `ecs/Dockerfile` with build context `ecs/`, because
   `requirements.txt` sits at `ecs/requirements.txt` while the source is under
   `ecs/python/` and `COPY` cannot reach above the context. Worth confirming the
   pipeline looks there. Frontend is `nodejs/Dockerfile`, context `nodejs/`.
5. Optional: is **path-based ALB routing** available? One ALB with `/api/*` → backend
   would let us delete the nginx proxy entirely.

Then:

6. Apply the backend restructure when it lands; rebuild and re-run §6.
7. Have someone validate `docker-compose.yml`.
8. Open a PR into `dev` when Ocean has signed off (deliberately not opened yet).
9. Chase the workstation permissions (§8) — `cli-plugins` is still unreadable as of
   2026-08-25, even though repo write access was granted.
