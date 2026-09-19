#!/bin/sh
set -eu

env_file=${1:-.env.production}
if [ ! -f "$env_file" ]; then
  echo "missing production env file: $env_file" >&2
  exit 1
fi

docker compose --env-file "$env_file" config --quiet
docker compose --env-file "$env_file" up -d --no-build
docker compose --env-file "$env_file" ps

attempt=0
until docker compose --env-file "$env_file" ps --status running --services | grep -qx worker; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 20 ]; then
    echo "worker did not enter running state" >&2
    exit 1
  fi
  sleep 3
done

attempt=0
while :; do
  api_container=$(docker compose --env-file "$env_file" ps -q api)
  api_health=""
  if [ -n "$api_container" ]; then
    api_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$api_container" 2>/dev/null || true)
  fi
  if [ "$api_health" = "healthy" ]; then
    break
  fi
  if [ "$api_health" = "unhealthy" ]; then
    echo "api healthcheck failed" >&2
    docker compose --env-file "$env_file" logs --tail 100 api >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 40 ]; then
    echo "api did not become healthy (last status: ${api_health:-missing})" >&2
    docker compose --env-file "$env_file" logs --tail 100 api >&2
    exit 1
  fi
  sleep 3
done

bootstrap_key=$(sed -n 's/^CUEKB_BOOTSTRAP_API_KEY=//p' "$env_file")
curl --fail-with-body --retry 20 --retry-delay 3 \
  -H "Authorization: Bearer $bootstrap_key" \
  http://127.0.0.1:8085/v1/ready
