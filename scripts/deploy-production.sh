#!/bin/sh
set -eu

env_file=${1:-.env.production}
if [ ! -f "$env_file" ]; then
  echo "missing production env file: $env_file" >&2
  exit 1
fi

docker compose --env-file "$env_file" config --quiet
docker compose --env-file "$env_file" build
docker compose --env-file "$env_file" up -d
docker compose --env-file "$env_file" ps

attempt=0
until docker compose --env-file "$env_file" exec -T model python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    docker compose --env-file "$env_file" logs --tail 100 model >&2
    echo "model service did not become healthy within 15 minutes" >&2
    exit 1
  fi
  sleep 15
done

attempt=0
until docker compose --env-file "$env_file" ps --status running --services | grep -qx worker; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 20 ]; then
    echo "worker did not enter running state" >&2
    exit 1
  fi
  sleep 3
done

bootstrap_key=$(sed -n 's/^CUEKB_BOOTSTRAP_API_KEY=//p' "$env_file")
curl --fail-with-body --retry 20 --retry-delay 3 \
  -H "Authorization: Bearer $bootstrap_key" \
  http://127.0.0.1:8080/v1/ready
