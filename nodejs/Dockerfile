# ARIA frontend — the Vite build served by nginx, with /api proxied to the backend.
#
# nginx rather than a Node server, per ARIA spec decision D20 ("Frontend is built
# and shipped as an nginx container serving static assets"), the Ocean team's
# decision, and deploy/README.md's own Known gaps entry: "The frontend is a Vite
# dev server ... replace with `vite build` behind nginx."
#
# Build context is this directory:
#   docker build --platform linux/amd64 -t aria-frontend nodejs/
#
# `npm ci` needs TWO things, and they are separate problems:
#
#   1. The AbbVie CA bundle. The corporate proxy re-signs outbound TLS, so a stock
#      node image fails with SELF_SIGNED_CERT_IN_CHAIN. Handled below.
#   2. Credentials for abbvie.jfrog.io. Every one of the 408 "resolved" URLs in
#      package-lock.json points there, @abbvie-unity/react exists nowhere else,
#      and anonymous read returns HTTP 401. Not something the image can supply.
#
# Pass the credential as a BuildKit secret so it never lands in a layer:
#
#   DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
#       --secret id=npmrc,src=/path/to/.npmrc \
#       -t aria-frontend nodejs/
#
# The mount is `required=false`, so the build still works unchanged against a
# registry that needs no authentication.

# ---------------------------------------------------------------------------
# Stage 1 — build the bundle
# ---------------------------------------------------------------------------
# node 22 rather than 20: package.json requires "^20.19.0 || >=22.12.0" and Vite 7
# shares that floor. Alpine is safe here — package-lock.json contains every Linux
# platform binary, musl included (@tailwindcss/oxide-linux-x64-musl,
# @rollup/rollup-linux-x64-musl), so npm ci resolves natively.
FROM node:22-alpine AS builder

WORKDIR /app

# A copy of ecs/python/certs/ca-bundle.pem. Duplicated because Ocean builds each
# app from its own directory as the build context, so the frontend cannot reach
# into the backend's tree. Public trust material only — 147 certificates, no
# private keys — so committing it is not a secret exposure.
COPY certs/ca-bundle.pem /tmp/ca-bundle.pem
ENV NODE_EXTRA_CA_CERTS=/tmp/ca-bundle.pem

# Copied alone so the dependency layer caches independently of application code.
COPY package.json package-lock.json ./

# ---------------------------------------------------------------------------
# Artifactory credentials — REQUIRED. This is not optional for ARIA.
# ---------------------------------------------------------------------------
# All 408 "resolved" URLs in package-lock.json point at abbvie.jfrog.io, and
# @abbvie-unity/react exists nowhere else. Anonymous read returns HTTP 401, so
# `npm ci` cannot succeed without a credential. (Contrast the PDF Redaction
# frontend, whose 213 packages all resolve from public registry.npmjs.org and
# therefore needs none.)
#
# TWO ways to supply it, because .npmrc is gitignored and a CI checkout will not
# have one:
#
#   1. CI / Ocean — pass a token and this stage writes the .npmrc itself, so
#      nothing needs to exist in the repository:
#        docker build --build-arg NPM_TOKEN=<jfrog token> -t aria-frontend nodejs/
#
#   2. Local development — if nodejs/.npmrc exists it is used as-is and
#      NPM_TOKEN can be omitted.
#
# Either way the credential exists only inside this builder stage, which is
# discarded: only dist/ crosses into the runtime image. Verify with
#   docker run --rm --entrypoint sh aria-frontend -c 'find / -name .npmrc'
#
# A BuildKit secret mount is better still where available, since a build arg is
# visible in this stage's history (never in the published image). If Ocean's
# builder has buildx:
#   RUN --mount=type=secret,id=npmrc,target=/root/.npmrc,required=false \
#       npm ci --no-audit --no-fund
#   DOCKER_BUILDKIT=1 docker build --secret id=npmrc,src=$HOME/.npmrc ...
ARG NPM_REGISTRY=https://abbvie.jfrog.io/artifactory/api/npm/npm/
ARG NPM_TOKEN=""

COPY .npmrc* ./

# A FULL install, devDependencies included — do not "optimise" this to
# --omit=dev. The build script is `vite build && tsc`, tsconfig.json has no
# `exclude` and sets types: [..., "vitest/globals"], so tsc type-checks the test
# files as well. Omitting dev dependencies makes `npm run build` fail.
RUN if [ -n "$NPM_TOKEN" ]; then \
        printf 'registry=%s\n%s:_authToken=%s\nalways-auth=true\n' \
            "$NPM_REGISTRY" "${NPM_REGISTRY#https:}" "$NPM_TOKEN" > .npmrc; \
        echo "npm: using credential from the NPM_TOKEN build arg"; \
    elif [ -f .npmrc ]; then \
        echo "npm: using the .npmrc from the build context"; \
    else \
        echo "npm: WARNING - no credential supplied; expect HTTP 401 from Artifactory" >&2; \
    fi \
    && npm ci --no-audit --no-fund \
    && rm -f .npmrc

COPY . .

# `vite build && tsc`. tsc runs second and only type-checks (noEmit), but the npm
# script still exits non-zero on a type error, so type errors fail the image
# build. That is intentional.
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — serve
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

# Rendered by the entrypoint at container start; see nginx.conf.template.
COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# In nginx:1.27-alpine /var/cache/nginx is root-owned and /var/run is not writable
# by the nginx user (verified), so a genuinely non-root nginx needs both handled:
# the cache directories are chowned here, and the pid file is moved to /tmp by
# nginx.conf.template.
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /var/cache/nginx \
    && chown -R nginx:nginx /var/cache/nginx

# Only the build output. No node_modules, no source, no TypeScript, no test files.
COPY --from=builder --chown=nginx:nginx /app/dist /usr/share/nginx/html

# All overridable by the ECS task definition.
#   PORT                  matches the container-ecs-alb ContainerPort default.
#   API_SERVER_URL        the backend's internal ALB. MUST be set per environment;
#                         the default here only makes local `docker run` sensible.
#   CLIENT_MAX_BODY_SIZE  above the backend's MAX_UPLOAD_MB (200) to allow for the
#                         multipart envelope. nginx's own default of 1m would
#                         reject every real document upload with a 413.
#   PROXY_TIMEOUT         nginx defaults to 60s; document downloads stream.
ENV PORT=8080 \
    API_SERVER_URL=http://localhost:8000 \
    CLIENT_MAX_BODY_SIZE=256m \
    PROXY_TIMEOUT=300s

EXPOSE 8080

# No HEALTHCHECK: Fargate ignores the image-level instruction and honours only the
# task definition's healthCheck. Point that at /healthz.

USER nginx

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
