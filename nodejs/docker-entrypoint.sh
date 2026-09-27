#!/bin/sh
# ARIA frontend entrypoint.
#
# nginx cannot read environment variables in its configuration, so the config is
# shipped as a template and rendered here at container start. That is what lets a
# single built image be promoted from dev to prod with only the ECS task
# definition changing (ARIA spec decision D21).
set -eu

: "${PORT:=8080}"
: "${API_SERVER_URL:=http://localhost:8000}"
: "${CLIENT_MAX_BODY_SIZE:=256m}"
: "${PROXY_TIMEOUT:=300s}"

# A trailing slash would produce `proxy_pass http://host//api/...`, so strip it.
API_SERVER_URL="${API_SERVER_URL%/}"

# nginx's `resolver` needs an explicit nameserver — it does not read
# /etc/resolv.conf and it ignores /etc/hosts. Taking the first nameserver from
# resolv.conf gives Docker's embedded DNS locally and the VPC resolver on
# Fargate, with no per-environment configuration.
if [ -z "${DNS_RESOLVER:-}" ]; then
    DNS_RESOLVER="$(awk '/^nameserver/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)"
fi
if [ -z "${DNS_RESOLVER:-}" ]; then
    DNS_RESOLVER="127.0.0.11"
    echo "entrypoint: no nameserver in /etc/resolv.conf, defaulting resolver to ${DNS_RESOLVER}" >&2
fi

export PORT API_SERVER_URL CLIENT_MAX_BODY_SIZE PROXY_TIMEOUT DNS_RESOLVER

# The variable list is deliberate and must stay in sync with the template. Without
# it, envsubst would also replace nginx's own runtime variables — $uri,
# $request_uri, $remote_addr — with empty strings, producing a config that either
# fails to load or silently misroutes every request.
envsubst '${PORT} ${API_SERVER_URL} ${CLIENT_MAX_BODY_SIZE} ${PROXY_TIMEOUT} ${DNS_RESOLVER}' \
    < /etc/nginx/nginx.conf.template \
    > /tmp/nginx.conf

echo "entrypoint: port=${PORT} api=${API_SERVER_URL} resolver=${DNS_RESOLVER}" \
     "max_body=${CLIENT_MAX_BODY_SIZE} proxy_timeout=${PROXY_TIMEOUT}"

# Fail loudly and immediately on a bad config, rather than after the ALB has
# already started health-checking a container that will never answer.
nginx -t -c /tmp/nginx.conf

# exec so nginx becomes PID 1 and receives SIGTERM from `docker stop` or an ECS
# task drain directly. Without it the shell is PID 1, does not forward signals,
# and the container is SIGKILLed when the stop timeout expires.
#
# `daemon off` keeps nginx in the foreground, which is what a container needs.
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
