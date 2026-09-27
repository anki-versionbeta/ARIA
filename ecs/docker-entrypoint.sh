#!/bin/sh
# ARIA backend entrypoint. Selects which process this container runs.
#
# Precedence: an explicit command argument wins, then the ROLE environment
# variable, then the API. Both mechanisms are supported because Ocean's starter
# template injects Environment but never overrides Command, while a local
# `docker run aria-backend worker` is the natural way to test the other role.
set -eu

role="${1:-${ROLE:-api}}"

case "$role" in
    api)
        # exec, so uvicorn replaces this shell and becomes PID 1. That is what
        # makes SIGTERM from `docker stop` or an ECS task drain reach uvicorn
        # directly; without it the shell is PID 1, does not forward signals, and
        # the container is SIGKILLed when the stop timeout expires.
        exec uvicorn api.app:app \
            --host 0.0.0.0 \
            --port "${PORT:-8080}" \
            --workers "${UVICORN_WORKERS:-2}" \
            --timeout-graceful-shutdown "${UVICORN_GRACEFUL_TIMEOUT:-30}"
        ;;

    worker)
        # api/backend/worker/main.py installs its own SIGTERM handler which lets the current
        # stage finish before exiting, so being PID 1 is a correctness
        # requirement here rather than a nicety.
        #
        # Note for deployment: a document-generation stage runs far longer than
        # Fargate's default 30s StopTimeout, so the handler will usually be cut
        # short by SIGKILL. Nothing is lost — the reaper re-queues claims whose
        # heartbeat is older than STALE_CLAIM_TIMEOUT_S (default 900s) — but that
        # is a 15 minute delay. Raising StopTimeout is on the Ocean change list.
        exec python -m api.backend.worker
        ;;

    migrate)
        echo "ROLE=migrate is not implemented: ARIA has no Alembic setup and" >&2
        echo "create_all() runs only when DA_ENV=local. Schema creation is an" >&2
        echo "open item — see README.Docker.md." >&2
        exit 1
        ;;

    *)
        # Anything unrecognised is run verbatim, so the image doubles as a
        # debugging shell:
        #   docker run --rm -it aria-backend python -m scripts.check_iliad
        #   docker run --rm -it aria-backend sh
        if [ "$#" -gt 0 ]; then
            exec "$@"
        fi
        echo "Unknown ROLE '$role'. Expected 'api' or 'worker'." >&2
        exit 64
        ;;
esac
