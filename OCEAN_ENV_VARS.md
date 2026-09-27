# ARIA — configuration required from Ocean

Handover document. Everything ARIA needs at runtime that the container images do
**not** and cannot provide.

The images are self-contained: no volumes, no code changes, no special runtime
flags. Every item below is an environment variable, plus a database and one
build-time registry credential. Locally these come from a `.env` file read by the
Docker CLI; in Fargate the ECS task definition has to play that role.

Three services are deployed. The API and worker are **the same image**, selected by
the `ROLE` variable.

| Service | Image | ALB | Health check |
|---|---|---|---|
| `aria-api` | `aria-backend` | yes | `GET /api/healthz` |
| `aria-worker` | `aria-backend` | **no** — listens on no port | none possible |
| `aria-web` | `aria-frontend` | yes | `GET /healthz` |

---

## 1. Secrets — must NOT be plaintext in the task definition

These belong in Secrets Manager, referenced from the container definition's
`Secrets` / `valueFrom` block.

| Variable | Used by | If missing |
|---|---|---|
| `JWT_SECRET` | api, worker | **Container will not start.** Raises at module import. Verified: exit code 1. |
| `DATABASE_URL` | api, worker | Contains the database password — see §5 |
| `ILIAD_API_KEY` | api, worker | LLM calls fail at request time |
| `ILIAD_USER_TOKEN` | api, worker | ISO silo RAG calls fail |
| `CMCDW_USER` | api, worker | ATR/MFGR silo fails at request time |
| `CMCDW_PASSWORD` | api, worker | ATR/MFGR silo fails at request time |

> **`JWT_SECRET` is not in the local `.env` file.** It works locally only because
> `DA_ENV=local` falls back to a hardcoded development constant. Outside `local`
> it is mandatory. It is the single most important item on this page.
>
> Changing it invalidates every existing session, so it logs all users out. Set it
> off-hours (noted in `deploy/README.md`).

### Required IAM change

For `Secrets` / `valueFrom` to work, the **ECS execution role** needs
`secretsmanager:GetSecretValue` and `kms:Decrypt`. Note this is the *execution*
role, not the task role — the ECS agent fetches secrets before the container
starts.

In the Ocean starter templates the roles are confusingly named:

```yaml
ExecutionRoleArn: !GetAtt ExecutionRole.Arn        # the execution role
TaskRoleArn:      !GetAtt TaskExecutionRole.Arn    # the TASK role, despite its name
```

The frontend template's existing `SecretManagerAccess` policy is attached to
`TaskExecutionRole` — the **task** role. Neither template's `ExecutionRole`
references `secretsmanager` or `ssm` at all. Adding a `Secrets` block without also
granting the execution role produces:

```
ResourceInitializationError: unable to pull secrets or registry auth ... AccessDeniedException
```

which reads like an ECR problem and is not one.

---

## 2. Environment — plaintext is fine

### Both `aria-api` and `aria-worker`

| Variable | Value | Notes |
|---|---|---|
| `DA_ENV` | `dev` \| `qa` \| `prod` | Must **not** be `local`. See §4. |
| `ROLE` | `api` or `worker` | Selects the process. See §3. |
| `STORAGE_BACKEND` | **`s3`** | Defaults to `local`. **Silent data loss if omitted** — see §6. |
| `S3_BUCKET` | e.g. `ir-doc-authoring` | |
| `S3_PREFIX` | per environment | |
| `AWS_REGION` | `us-east-1` | |
| `CMCDW_DSN` | Oracle descriptor — **must use the FQDN** | See §7 |
| `APP_BASE_URL` | the public URL of this environment | Goes into notification email links. Default is the dev EC2's IP. |
| `LOG_LEVEL` | `INFO` | |
| `MAX_UPLOAD_MB` | `200` | Must not exceed the ALB/nginx limits |
| `STALE_CLAIM_TIMEOUT_S` | `900` | Must exceed the longest silent stage |
| `LLM_MAX_CONCURRENCY` | see §3 | **Per process**, not cluster-wide |
| `TEXTRACT_MAX_CONCURRENCY` | `4` | Per process |

### `aria-web` only

| Variable | Value | Notes |
|---|---|---|
| `API_SERVER_URL` | the backend's internal ALB URL, no trailing slash | nginx proxies `/api` here. Without it the frontend cannot reach the API. |
| `PORT` | `8080` | Already the image default |

Optional frontend tuning, all with working defaults:
`CLIENT_MAX_BODY_SIZE` (`256m`), `PROXY_TIMEOUT` (`300s`), `DNS_RESOLVER`
(auto-detected from `/etc/resolv.conf`).

---

## 3. Two services from one image

The API and worker share the entire codebase and ship as a single image; the role
is chosen at container start (ARIA design decision D22).

**`ROLE` is an environment variable rather than a command override**, because the
Ocean starter template injects `Environment` but never overrides `Command`. An
explicit command argument is also honoured, so `Command: ["worker"]` works if that
is easier.

**Worker sizing.** The dev EC2 runs five worker processes (`aria-worker@1..5`)
because a report takes 1–3 minutes and the worker is single-threaded by design.
Suggested starting point: **desired count 5**.

`LLM_MAX_CONCURRENCY` is per process, so five tasks at the default of 8 means up to
**40 concurrent requests** on the Iliad gateway. Set it to the global budget
divided by the task count.

**`StopTimeout: 120` recommended** on the worker. It installs a SIGTERM handler
that lets the current stage finish; systemd uses `TimeoutStopSec=300`, and 120 is
the Fargate maximum. Below that, SIGKILL lands mid-stage — nothing is lost, the
reaper re-queues stale claims, but that is a 15-minute delay.

---

## 4. `DA_ENV` does three jobs — read this before setting it

Setting `DA_ENV` to anything other than `local` changes three behaviours at once:

1. **The database schema is no longer created.** `create_all()` runs only when
   `DA_ENV=local`. See §8.
2. **`cookie_secure` becomes true.** Browsers will not send a `Secure` cookie over
   plain HTTP, so **login breaks** unless the site is served over HTTPS. The
   starter samconfig sets `AcmCertificateArn=''`, which yields an HTTP-only
   listener. Either provide an ACM certificate, or set `COOKIE_SECURE=false`
   explicitly. `deploy/README.md` records having hit this.
3. **`JWT_SECRET` becomes mandatory** rather than falling back to a dev constant.

These are not three separate findings — they are one setting with three
consequences.

---

## 5. `DATABASE_URL` must be assembled

Ocean's `rds-postgresql.yaml` creates a secret named
`${ServiceName}-${Environment}-db-credentials` containing **components**:

```json
{"host": "...", "port": 5432, "dbname": "...", "username": "...",
 "password": "...", "engine": "postgresql"}
```

ARIA reads a **single assembled SQLAlchemy URL** and nothing else — there are no
host/user/password variables in the code. ECS `valueFrom` can extract one JSON key
per variable but cannot concatenate.

So a **second secret** is needed, holding the full URL:

```
postgresql+psycopg2://<user>:<password>@<host>:5432/<dbname>?sslmode=require
```

Verified working against a real RDS instance from inside the container
(PostgreSQL 15.17, TLS, via a source-compiled `psycopg2`).

---

## 6. Must NOT be set

```
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN
```

`credentials.py` passes explicit keys only when both an access key and a secret are
present; otherwise it uses the default boto3 chain and picks up the **task role**,
which refreshes itself. Static STS credentials cannot renew — this exact failure
(`ExpiredToken` on `PutObject`) is what `deploy/sql/001` was written to defend
against, and we reproduced it locally.

The task role needs: `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on the bucket
and prefix, and `textract:AnalyzeDocument`.

---

## 7. `CMCDW_DSN` must use the FQDN

The DSN currently in circulation uses the bare hostname `uq00604p`, which **no
container can resolve**. Use:

```
(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=uq00604p.abbvienet.com)(PORT=1521))(CONNECT_DATA=(SID=...)))
```

A DNS search domain is not a reliable substitute: Docker's embedded DNS sets
`options ndots:0`, which suppresses search-suffix expansion for single-label names.
Verified — bare name fails, FQDN resolves to `10.72.24.196`.

---

## 8. Database schema — not provided by the images

`create_all()` runs only when `DA_ENV=local`. There is no Alembic setup. The SQL
files in `deploy/sql/` are **incremental** — `002` does `ALTER TABLE users`, `003`
references `users(id)` — so none of them creates the base tables.

On a fresh database the API starts, **`/api/healthz` returns 200** (it only runs
`SELECT 1`, which succeeds against an empty schema), the task goes healthy and
joins the load balancer, and then every real query fails.

**A green health check does not mean the schema exists.** That is inherent to the
application's health endpoint, not something the container adds or can fix.

Owners: the ARIA developers for a repeatable schema definition (already planned —
`db/session.py:44` notes "Alembic owns schema changes from the dev environment
onward"); the deployer for running it. Migrations run from the pipeline, so the
image deliberately does not ship `psql`.

### ⚠ Do not apply `deploy/sql/001_worker_allowlist_guard.sql`

It installs a trigger on `runs` rejecting claims from hosts outside an allowlist,
currently containing only the dev EC2's hostname prefix. A container's hostname is
its task ID — random hex — and ECS does not support the `hostname` field in
`awsvpc` mode, so no stable prefix exists.

Failure mode: the worker catches the exception, logs it, sleeps 2s and retries
forever. The task stays `RUNNING`, has no port to health-check, emits roughly
43,000 tracebacks per worker per day, and **every run sits in `queued` forever**.
No alarm fires.

Note that Ocean's `migrate.sh` runs `find sql/ -name "*.sql" | sort` — *all* files
— so `001` would be applied automatically if `deploy/sql/` becomes the migration
source. It must be excluded deliberately.

The credential-expiry problem `001` guards against **cannot occur on Fargate**,
because the task role renews itself.

---

## 9. Build-time requirements

| | |
|---|---|
| Platform | **`linux/amd64`** — the task definition pins `X86_64 / LINUX` |
| Frontend registry credential | **Required.** All 408 packages in `package-lock.json` resolve to `abbvie.jfrog.io` and `@abbvie-unity/react` exists nowhere else; anonymous read returns 401. Pass `--build-arg NPM_TOKEN=<token>` and the Dockerfile writes the `.npmrc` itself — nothing needs to exist in the repository. **A service account is needed; do not depend on a personal token.** |
| Python package index | The CA bundle committed at `ecs/python/certs/ca-bundle.pem` is enough to install from pypi.org through the corporate TLS-intercepting proxy — **no credential required**. To use Artifactory or CodeArtifact instead, pass `--build-arg PIP_INDEX_URL=...`. |

For contrast, the PDF Redaction frontend needs no registry credential: all 213 of
its packages resolve from public `registry.npmjs.org`.

---

## 10. Network egress

Tasks run in private subnets with `AssignPublicIp: DISABLED`, and the starter
templates create no NAT gateway or VPC endpoints.

| Target | Port | Resolves to |
|---|---|---|
| RDS PostgreSQL | 5432 | in-VPC |
| `*.s3.<region>.amazonaws.com` | 443 | AWS |
| `textract.<region>.amazonaws.com` | 443 | AWS |
| `iliad-emerging-api.abbvienet.com` | 443 | internal |
| `ldap-ad.abbvienet.com` | 636 | **10.72.249.44** (private) |
| `uq00604p.abbvienet.com` | 1521 | **10.72.24.196** (private) |
| `smtp.abbvienet.com` | 25 | internal — no auth, no STARTTLS |

The two `10.72.x` addresses are private on-prem hosts, so the VPC needs a route to
on-prem **and** Route 53 Resolver rules able to resolve `abbvienet.com`.

---

## 11. Load balancer and health checks

| Setting | Backend | Frontend |
|---|---|---|
| `ContainerPort` | 8080 | 8080 |
| `HealthCheckPath` | **`/api/healthz`** | **`/healthz`** |
| `HealthCheckHttpCode` | 200 | 200 |

**`HealthCheckPath` must not be left at the template default of `/`.** Every ARIA
backend route is mounted under `/api`; `GET /` returns **404** (verified). With the
default the task fails its health check, is killed, and crash-loops forever.

Also recommended: raise the **ALB idle timeout** above the 60s default, since
document downloads stream through the connection.

---

## 12. Variables that need no attention

These already have correct defaults, either in the application or baked into the
images:

```
LDAP_URL  LDAP_DOMAIN  AUTH_PROVIDER  ILIAD_BASE_URL  LLM_TEXT_MODEL
LLM_VISION_MODEL  SMTP_HOST  SMTP_PORT  MAIL_FROM  CMCDW_SCHEMA
CMCDW_RESULTS_OBJECT  JWT_TTL_HOURS  WORKER_HEARTBEAT_S  ENABLED_SILOS
CA_BUNDLE_PATH  SILOS_DIR  STORAGE_DIR  UVICORN_WORKERS
```

---

## 13. Failure modes, ranked by how hard they are to diagnose

| Variable | Wrong/missing behaviour | Loud? |
|---|---|---|
| `STORAGE_BACKEND` | Uploads written to ephemeral disk and lost on redeploy | **Silent** |
| — (schema) | Health check passes, every real query fails | **Silent** |
| — (`deploy/sql/001` applied) | Runs queue forever, task reports healthy | **Silent** |
| `DATABASE_URL` | Falls back to SQLite inside the container; api and worker stop sharing a queue | Quiet |
| `APP_BASE_URL` | Notification emails link to the dev EC2 | Quiet |
| `COOKIE_SECURE` / no HTTPS | Login appears to work, then every request 401s | Confusing |
| `HealthCheckPath` | Task crash-loops | Loud |
| `JWT_SECRET` | Container will not start | **Loud** |

The silent ones are worth the most attention: the application looks healthy in
every case.
