# ARIA backend — container image

One image, two roles. The API and the worker share the whole `da_platform` +
`silos` codebase, so they ship as a single image and the role is chosen at
container start (ARIA design decision D22).

| Role | What runs | Needs an ALB? |
|---|---|---|
| `api` (default) | `uvicorn api.app:app --host 0.0.0.0 --port 8080` | **Yes** |
| `worker` | `python -m api.backend.worker` | **No** — listens on no port |
| `migrate` | stub; exits 1 with an explanation | — |
| anything else | executed verbatim, so the image doubles as a debug shell | — |

> **Deploying this?** Configuration requirements — environment variables, secrets,
> IAM, network egress, health check paths — are in
> [`../OCEAN_ENV_VARS.md`](../OCEAN_ENV_VARS.md). This file covers only building
> and running the image.

---

## Build

Build context is **this directory (`ecs/`)** — *not* `ecs/python/`.
`requirements.txt` sits at `ecs/requirements.txt` while the source is under
`ecs/python/`, and `COPY` cannot reach above the build context, so a `python/`
context could not see the requirements file.

```bash
docker build --platform linux/amd64 -t aria-backend ecs/
```

`--platform linux/amd64` matters: the ECS task definition pins
`RuntimePlatform: X86_64 / LINUX`, so an arm64 image will not start.

### Corporate TLS interception is handled

The AbbVie proxy re-signs outbound TLS with a corporate CA that a stock `python`
image does not trust, so `pip` would otherwise fail on every index request:

```
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: self-signed certificate in certificate chain'))
```

The Dockerfile passes `pip --cert /tmp/ca-bundle.pem`, using the bundle already
committed at `certs/ca-bundle.pem` (certifi's public roots plus the AbbVie chain) —
the same file the app uses at runtime for LDAPS, Iliad and S3. **No credential is
required** to install from pypi.org.

To install from Artifactory or CodeArtifact instead, no Dockerfile change is needed:

```bash
docker build --build-arg PIP_INDEX_URL=https://... \
             --build-arg PIP_TRUSTED_HOST=... ecs/
```

### Why the build looks the way it does

- **Two stages.** `build-essential`, `gcc` and `libpq-dev` compile `psycopg2` in the
  builder and never reach the runtime image; only `libpq5` ships.
- **`psycopg2`, not `psycopg2-binary`.** Deliberate — it avoids editing
  `requirements.txt`, which is not ours. Verified working against real RDS over TLS.
- **Debian slim, not Alpine.** PyMuPDF, Pillow and pypdfium2 ship *manylinux*
  (glibc) wheels; on musl they would fall back to a large source build of MuPDF.
- **`oracledb` runs in thin mode** — no Oracle Instant Client needed.

---

## Run locally

On **Git Bash for Windows**, prefix with `MSYS_NO_PATHCONV=1`, or MSYS rewrites
paths like `/tmp/storage` into `C:/Program Files/Git/tmp/storage` and the container
fails with `PermissionError: 'C:'`.

### Minimal — no credentials, no external dependencies

`DA_ENV=local` is what makes `create_all()` build the schema.

```bash
export MSYS_NO_PATHCONV=1

docker run -d --name aria-api -p 8080:8080 \
  -e DA_ENV=local -e JWT_SECRET=local-only \
  -e DATABASE_URL=sqlite:////tmp/aria.db \
  aria-backend

curl http://localhost:8080/api/healthz     # -> {"status":"ok","database":"ok"}
```

Worker role — note it is selected by an environment variable:

```bash
docker run -d --name aria-worker -e ROLE=worker \
  -e DA_ENV=local -e JWT_SECRET=local-only \
  -e DATABASE_URL=sqlite:////tmp/aria.db \
  aria-backend
```

### Full stack against a real Postgres

`docker-compose.yml` in the repo root describes this, but if `docker compose` is
unavailable the equivalent with plain `docker run` is:

```bash
export MSYS_NO_PATHCONV=1

# CMCDW_DSN must use an FQDN — see "Oracle DNS" below.
DSN=$(grep '^CMCDW_DSN=' .env | sed 's/^CMCDW_DSN=//' \
      | sed 's/HOST=uq00604p)/HOST=uq00604p.abbvienet.com)/')

docker network create aria-stack

docker run -d --name aria-db --network aria-stack \
  -e POSTGRES_USER=aria -e POSTGRES_PASSWORD=aria-local-only -e POSTGRES_DB=aria \
  -p 15432:5432 -v aria-pgdata:/var/lib/postgresql/data postgres:16-alpine

docker run -d --name aria-api --network aria-stack -p 18080:8080 \
  --env-file .env \
  -e ROLE=api -e DA_ENV=local \
  -e DATABASE_URL='postgresql+psycopg2://aria:aria-local-only@aria-db:5432/aria' \
  -e JWT_SECRET=local-only -e CMCDW_DSN="$DSN" \
  aria-backend

docker run -d --name aria-worker --network aria-stack \
  --env-file .env \
  -e ROLE=worker -e DA_ENV=local -e LLM_MAX_CONCURRENCY=4 \
  -e DATABASE_URL='postgresql+psycopg2://aria:aria-local-only@aria-db:5432/aria' \
  -e JWT_SECRET=local-only -e CMCDW_DSN="$DSN" \
  --stop-timeout 300 \
  aria-backend
```

`--env-file` supplies only the credentials (Iliad, AWS, S3, Oracle). Explicit `-e`
flags override it — the later value wins.

**`docker --env-file` does not strip quotes**, unlike ARIA's own loader
(`settings.py:48`). Keep values in `.env` unquoted: `JWT_SECRET="REDACTED"` arrives as
the literal `"abc"` including quotes.

**`.env` never enters the image.** It is read by the Docker CLI on the host and
injected as environment variables. `.dockerignore` excludes it as a security
control, not an optimisation — `settings.py:81` *would* load `/app/.env` if it
existed. Verified: adding `.env` to the directory produced an identical image ID,
so Docker never saw it.

### As a diagnostic shell

An unrecognised role is executed verbatim:

```bash
docker run --rm --env-file .env -e DA_ENV=dev -e JWT_SECRET=x \
  aria-backend python -m scripts.check_iliad
docker run --rm -it aria-backend sh
```

Available diagnostic scripts: `check_iliad.py`, `check_ldap.py`, `diagnose_tls.py`,
`build_ca_bundle.py`, `seed_dev.py`. (`diagnose_aws.py` was removed on `dev`; for an
S3 check, use `boto3` directly with `settings.ca_bundle_path` as `verify`.)

---

## Oracle DNS

`CMCDW_DSN` as normally configured uses the bare hostname `uq00604p`, which **no
container can resolve**. Use the FQDN:

```
(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=uq00604p.abbvienet.com)(PORT=1521))(CONNECT_DATA=(SID=...)))
```

A DNS search domain is not a reliable substitute — Docker's embedded DNS sets
`options ndots:0` on user-defined networks, which suppresses search-suffix expansion
for single-label names. Verified: bare name fails, FQDN resolves to 10.72.24.196.
Affects the ATR/MFGR silo only.

---

## What the image guarantees

- Listens on `0.0.0.0:${PORT}` (default **8080**), plain HTTP
- Runs as non-root `appuser` (uid 1000, gid 1000)
- The application process is **PID 1**, so SIGTERM from `docker stop` or an ECS task
  drain reaches it directly. Verified: graceful shutdown, exit code 0, 2–3s.
- Logs to stdout/stderr only, unbuffered (`PYTHONUNBUFFERED=1`)
- `linux/amd64`, Debian slim
- Contains no `.env`, no `.venv`, no `tests/`
- Fails fast on a missing `JWT_SECRET` outside `DA_ENV=local` — exit 1, error printed
  once. This is deliberate: `UVICORN_WORKERS` defaults to **1** because with more,
  uvicorn's supervisor restarts dead children forever and ECS would report a
  container as `RUNNING` while it serves nothing.
- **No `HEALTHCHECK` instruction** — Fargate ignores the image-level directive and
  honours only the task definition's `healthCheck`, so including one would mean
  installing `curl` for something that never runs in the target environment.

## Image-level defaults

All overridable by the task definition.

| Variable | Default | Note |
|---|---|---|
| `ROLE` | `api` | `api` or `worker` |
| `PORT` | `8080` | matches Ocean's `ContainerPort` default |
| `UVICORN_WORKERS` | `1` | scale with ECS desired count instead |
| `UVICORN_GRACEFUL_TIMEOUT` | `30` | |
| `SILOS_DIR` | `/app/src/api/backend/silos` | moved down with the source |
| `STORAGE_DIR` | `/tmp/aria-storage` | **not** `settings.py`'s default of `/app/dev_storage`, which is inside the root-owned code directory and cannot be created by a non-root user |
| `CA_BUNDLE_PATH` | `/app/certs/ca-bundle.pem` | outbound TLS trust |
| `PYTHONPATH` | `/app/src:/app` | `/app/src` for the `api.*` packages, `/app` so `python -m scripts.*` works |

### Why the image preserves the source tree's depth

`settings.py` derives two anchors from its own file location:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[4]   # .env, certs, dev.db, dev_storage
BACKEND_ROOT = Path(__file__).resolve().parents[1]   # silos
```

The Dockerfile therefore copies `python/` as a single tree into `/app`, which puts
`settings.py` at `/app/src/api/backend/da_platform/settings.py` and makes
`PROJECT_ROOT=/app`, `BACKEND_ROOT=/app/src/api/backend`. Both defaults then resolve,
and they are set explicitly anyway so the contract is visible.

**Do not flatten the packages into `/app`.** `parents[4]` needs four directory levels
above `da_platform/`; flattened, it runs off the filesystem root and raises
`IndexError` at import — before the app object exists, so the failure is a container
that will not start rather than a route that 500s.

---

## Pipeline gates

- **ASH v2.0.1** security scan at the repo root. There is no `.bandit` file in this
  directory yet; the Ocean sample uses `exclude = **/tests/**`.
- **pytest coverage > 90%.** Tests run against the source tree, not inside the image
  (`python/tests/` is excluded from the build context). `pytest` and `httpx` are in the
  single `requirements.txt`; there is no separate test requirements file and no
  pytest config.
