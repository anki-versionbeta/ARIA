# ARIA backend — one image, two roles.
#
# The API and the worker share the whole da_platform + silos codebase, so they
# ship as a single image and the role is chosen at container start (spec D22).
#   ROLE=api     uvicorn serving api.app:app          (behind the ALB)
#   ROLE=worker  python -m api.backend.worker          (no port, no ALB)
#
# Build context is ecs/ — this directory — and NOT ecs/python/. requirements.txt
# sits here while the source is under python/, and COPY cannot reach above the
# build context, so a python/ context could not see the requirements file.
#   docker build -t aria-backend ecs/
#
# Fargate runs X86_64/LINUX, so on an ARM machine build with
#   docker build --platform linux/amd64 -t aria-backend ecs/

# ---------------------------------------------------------------------------
# Stage 1 — build the dependency tree
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# psycopg2 (as opposed to psycopg2-binary) is published as a source
# distribution only, so it is compiled here: gcc/build-essential for the
# compiler, libpq-dev for pg_config and the libpq headers it links against.
# All of this stays in the builder and never reaches the shipped image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# The AbbVie proxy intercepts outbound TLS and re-signs it with a corporate CA.
# A stock python image does not trust that CA, so pip fails every index request
# with CERTIFICATE_VERIFY_FAILED even though pypi.org is reachable. The repo
# already ships the bundle that fixes it — certifi's public roots concatenated
# with the AbbVie chain — and requirements.txt documents the same requirement
# for local installs.
COPY python/certs/ca-bundle.pem /tmp/ca-bundle.pem

# Unset by default, so pip uses its normal index. Ocean's build can redirect to
# Artifactory or CodeArtifact without editing this file:
#   docker build --build-arg PIP_INDEX_URL=https://... ecs/
# pip reads both of these from the environment on its own.
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

# Copied on its own so the dependency layer is cached independently of the
# application code, which changes far more often.
COPY requirements.txt .
RUN pip install --cert /tmp/ca-bundle.pem --no-cache-dir --user -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — the runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# libpq5 is the runtime half of psycopg2 (the -dev headers stayed in stage 1).
# Everything else in requirements.txt resolves to a manylinux wheel — PyMuPDF,
# Pillow, pypdfium2 — which is why this is Debian slim and not Alpine: musl has
# no manylinux wheels, so Alpine would force a source build of MuPDF.
# oracledb runs in thin mode, so no Oracle Instant Client is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home appuser

# Left root-owned and world-readable: appuser needs to import these but has no
# reason to be able to modify them.
COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH

# The whole python/ tree is copied as one unit, and the depth MUST be preserved.
# settings.py derives two anchors from its own location:
#
#   PROJECT_ROOT = Path(__file__).resolve().parents[4]   -> .env, certs, dev.db
#   BACKEND_ROOT = Path(__file__).resolve().parents[1]   -> silos
#
# Copying python/ to /app puts settings.py at
# /app/src/api/backend/da_platform/settings.py, which makes PROJECT_ROOT=/app and
# BACKEND_ROOT=/app/src/api/backend — both correct. Flattening the packages into
# /app the way the pre-restructure image did would leave parents[4] running off the
# filesystem root and raise IndexError at import, before the app object exists.
#
# This brings src/ (the api and backend packages), certs/ and scripts/ in one COPY.
# tests/ and python/.env are excluded by .dockerignore.
COPY --chown=appuser:appgroup python/ ./

# Root-owned deliberately: the entrypoint should not be writable by the user it
# launches.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src:/app

# Defaults chosen so `docker run aria-backend` does the obvious thing. Every one
# of these is overridable by the ECS task definition.
#   ROLE  is how Ocean selects api vs worker. It is an environment variable
#         rather than a command override because the Ocean starter template
#         injects Environment but never overrides Command.
#   PORT  matches the container-ecs-alb ContainerPort default.
#   UVICORN_WORKERS defaults to 1 deliberately. With more than one, uvicorn runs
#         a supervisor that restarts dead children forever, so an import-time
#         failure (a missing JWT_SECRET, say) leaves a container that ECS still
#         reports as RUNNING while it serves nothing — verified, 147 identical
#         tracebacks and the container still "Up". With one worker the process
#         exits non-zero and ECS reports a real failure. Scale horizontally with
#         the service's desired count instead, which is the ECS-idiomatic model
#         and matters little here: the API only enqueues work, the worker does it.
#   STORAGE_DIR must point somewhere appuser can write. The default in
#         settings.py is PROJECT_ROOT/dev_storage — i.e. /app/dev_storage — and /app is
#         root-owned, so a container with no STORAGE_DIR set dies at boot with
#         PermissionError. That path is reached by create_all() (DA_ENV=local) and
#         by get_object_store() whenever STORAGE_BACKEND is not s3, and it fires
#         even when the backend IS s3 because db/session.py mkdirs
#         unconditionally. /tmp is honest about being ephemeral: on Fargate this
#         disk does not survive the task, which is exactly why STORAGE_BACKEND
#         must be s3 in every deployed environment.
ENV ROLE=api \
    PORT=8080 \
    UVICORN_WORKERS=1 \
    UVICORN_GRACEFUL_TIMEOUT=30 \
    SILOS_DIR=/app/src/api/backend/silos \
    STORAGE_DIR=/tmp/aria-storage \
    CA_BUNDLE_PATH=/app/certs/ca-bundle.pem

EXPOSE 8080

USER appuser

# No HEALTHCHECK: Fargate ignores the image-level instruction and honours only
# the task definition's healthCheck, so adding one here would mean installing
# curl or wget for something that never runs in the target environment.

# No CMD on purpose. With no CMD, "$1" is empty unless something explicitly
# overrides the command, which lets the entrypoint fall back to $ROLE. A
# CMD ["api"] would always occupy "$1" and make ROLE unreachable.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
