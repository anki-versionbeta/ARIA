# ARIA frontend — container image

nginx serving the Vite production build, proxying `/api` to the backend.

nginx per spec decision D20, the Ocean team's call, and `deploy/README.md` Known
gaps: *"The frontend is a Vite dev server ... replace with `vite build` behind
nginx."*

Deployment config (env vars, health check paths, ALB) →
[`../OCEAN_ENV_VARS.md`](../OCEAN_ENV_VARS.md). This file: build and run only.

---

## Build

```bash
docker build --platform linux/amd64 \
  --build-arg NPM_TOKEN=<jfrog token> \
  -t aria-frontend nodejs/
```

Context = this directory. `--platform linux/amd64` required; ECS pins
`X86_64 / LINUX`.

### NPM_TOKEN is mandatory

All 408 `resolved` URLs in `package-lock.json` → `abbvie.jfrog.io`.
`@abbvie-unity/react` exists nowhere else. Anonymous read = **HTTP 401**.

Two ways in:

| | |
|---|---|
| **CI** | `--build-arg NPM_TOKEN=<token>` — Dockerfile writes `.npmrc` itself. Nothing needed in the repo. |
| **Local** | `nodejs/.npmrc` present → used as-is, omit `NPM_TOKEN`. |

`.npmrc` is gitignored, so a CI checkout has none — hence the build arg. Neither
present → build prints a warning, then 401s.

Token lives only in the builder stage, which is discarded. Verify:

```bash
docker run --rm --entrypoint sh aria-frontend -c 'find / -name .npmrc'   # empty
```

Prefer a BuildKit secret where available:
`--mount=type=secret,id=npmrc,target=/root/.npmrc`. Build args are visible in the
builder stage's history (never in the published image).

Contrast: PDF Redaction's frontend needs no credential — all 213 of its packages
resolve from public `registry.npmjs.org`.

### CA bundle

`certs/ca-bundle.pem` — a copy of `ecs/python/certs/ca-bundle.pem`. Duplicated
because Ocean builds each app from its own directory; `COPY` cannot reach above the
build context. Public trust material only (147 certs, no private keys).

Set as `NODE_EXTRA_CA_CERTS`. Without it the proxy's re-signed TLS fails with
`SELF_SIGNED_CERT_IN_CHAIN`.

### Multi-stage, and why devDependencies are installed

`npm ci` installs **everything**, including dev. Do not "optimise" to `--omit=dev`:
`build` is `vite build && tsc`, `tsconfig.json` has no `exclude` and sets
`types: [..., "vitest/globals"]`, so `tsc` type-checks the test files too. Omitting
dev deps makes `npm run build` fail.

Type errors fail the image build. Intentional.

Only `dist/` crosses into the runtime image. No `node_modules`, no source, no TS.

`node:22-alpine` — `package.json` requires `^20.19.0 || >=22.12.0`; Vite 7 shares
that floor. Alpine is safe: the lockfile carries every Linux platform binary
including musl (`@tailwindcss/oxide-linux-x64-musl`, `@rollup/rollup-linux-x64-musl`).

---

## Run

```bash
docker run -d --name aria-web -p 8088:8080 \
  -e API_SERVER_URL=http://aria-api:8080 \
  aria-frontend
```

`API_SERVER_URL` — backend base URL, no trailing slash (stripped anyway). Must be
reachable from the container; on a user-defined Docker network use the container
name.

Host port 8088 in examples because 8080 was taken locally. Container always 8080.

---

## Config is rendered at start, not baked

`nginx.conf.template` + `envsubst` in `docker-entrypoint.sh`. One image promotes
across all environments (spec D21). `nginx -t` runs before `exec`, so a bad config
fails immediately rather than after the ALB starts health-checking.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | listen port |
| `API_SERVER_URL` | `http://localhost:8000` | proxy target |
| `CLIENT_MAX_BODY_SIZE` | `256m` | above backend `MAX_UPLOAD_MB` (200) |
| `PROXY_TIMEOUT` | `300s` | read + send |
| `DNS_RESOLVER` | first nameserver in `/etc/resolv.conf` | see below |

envsubst is given an explicit variable list. Without it, nginx's own `$uri`,
`$request_uri`, `$remote_addr` would be blanked.

---

## nginx behaviours that are load-bearing

Each one caused, or would have caused, a real failure.

| Location | Why it exists |
|---|---|
| `= /healthz` → 200 | ALB health check target. No filesystem dependency. |
| `= /config.json` → `try_files $uri =404` | `src/config/runtime.ts` fetches this at boot and parses JSON. **Must never fall through to the SPA fallback** — HTML here means the app renders only "Application configuration failed to load", a blank page with nothing useful in the console. `no-store` so a promoted image cannot serve a stale `apiBaseUrl`. |
| `/assets/` → `immutable`, 1yr | Vite fingerprints filenames. |
| `/api/` → `proxy_pass` | Same-origin required: login sets an httpOnly cookie and the backend has **no CORS middleware** (`da_platform/main.py:25-28`). Cross-origin would need CORS + `SameSite=None` — backend changes. |
| `/` → `try_files $uri $uri/ /index.html` | TanStack Router uses browser history. Without it, deep links and F5 return 404. Ordered last; exact matches above take precedence. |

Other non-obvious settings:

- **`types { application/javascript mjs; }`** — nginx's stock `mime.types` has **no
  `.mjs` entry**, so the pdf.js worker (`assets/pdf.worker.min-<hash>.mjs`) was
  served as `application/octet-stream`, and browsers refuse to execute an ES module
  with that type. Symptom: *"Failed to fetch dynamically imported module"*, PDF
  preview broken, everything else fine.
- **`client_max_body_size`** — nginx default is **1 MB**. A real 37 MB BOP upload
  would 413 at the proxy without this.
- **`proxy_request_buffering off`** — streams uploads instead of spooling the whole
  body to disk first.
- **`resolver` + variable `proxy_pass`** — nginx resolves a literal `proxy_pass`
  host once at startup and caches it forever. An internal ALB's IPs rotate → later
  intermittent 502s. A variable upstream forces re-resolution. `$request_uri` must be
  appended explicitly, since a variable upstream does not pass the URI implicitly.
- **`pid /tmp/nginx.pid`** and `chown /var/cache/nginx` — in `nginx:1.27-alpine`,
  `/var/cache/nginx` is root-owned and `/var/run` is not writable by `nginx`
  (verified). Both are needed for a genuinely non-root nginx.
- **`absolute_redirect off; port_in_redirect off;`** — otherwise a redirect leaks
  `:8080` into a `Location` header the browser cannot reach through the ALB.

---

## Guarantees

- `0.0.0.0:${PORT}`, plain HTTP
- Non-root `nginx` (uid 101)
- nginx is **PID 1** — SIGTERM reaches it. Graceful stop, exit 0, ~3s.
- Logs to stdout/stderr
- gzip on text assets (945 KB JS → 341 KB over the wire)
- 51.6 MB, `linux/amd64`
- Contains only `dist/` — no `.npmrc`, no `node_modules`, no source
- **No `HEALTHCHECK`** — Fargate ignores the image-level directive

---

## Caching gotcha

`/assets/` is `immutable`, `max-age=31536000`. Correct for fingerprinted files, but:
**changing a header on an already-served asset will not reach a browser that cached
it.** Filename unchanged → nothing to invalidate.

The `.mjs` MIME fix needed DevTools "Disable cache" or incognito. A hard reload was
**not** enough: the pdf.js worker is fetched by a runtime dynamic `import()`, and
`Ctrl+Shift+R` only bypasses cache for the navigation and its discovered
subresources.

Not an issue in Ocean — each deploy produces fresh hashes.

---

## Not our concern, but visible

`index.html` loads the FontAwesome Pro kit and Google Fonts from public CDNs at page
load. The **browser** fetches those, not the container. Icons degrade if clients
cannot reach them. Pre-existing app behaviour.
