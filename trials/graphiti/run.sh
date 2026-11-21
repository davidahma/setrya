#!/usr/bin/env bash
set -eu
folder=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$folder/../.." && pwd)
out=${1:?supply a fresh output directory}
mkdir "$out"
work=$(mktemp -d)
node=""
cleanup() {
  if test -n "$node"; then docker stop "$node" >/dev/null; fi
  rm -rf "$work"
}
trap cleanup EXIT
node=$(docker run --rm -d -p 127.0.0.1::7687 -e NEO4J_AUTH=none -e NEO4J_server_memory_heap_max__size=256m \
  -e NEO4J_server_memory_pagecache_size=64m neo4j@sha256:51d0314ef86074f519003da8dd4daaa9fff72b8479672b742d7cab7f3064e3ed)
for attempt in {1..60}; do
  if docker exec "$node" cypher-shell 'RETURN 1' >/dev/null 2>&1; then break; fi
  sleep 1
done
endpoint=$(docker port "$node" 7687/tcp)
for spec in 0.31.1:5961293865a57844fe13d4eb061deb3501971fb2:1 0.31.2:d72407dcedac911e1be9fc15b55e8c05a3efcc36:0; do
  IFS=: read -r version revision expected <<< "$spec"
  python3 -m venv "$work/$version"
  py="$work/$version/bin/python"
  "$py" -m pip install --quiet "graphiti-core @ git+https://github.com/getzep/graphiti.git@$revision" "openai<3" "$repo"
  code=0
  "$py" "$folder/trial.py" "$out/$version.json" --port "${endpoint##*:}" || code=$?
  test "$code" -eq "$expected"
  code=0
  "$py" -I -m setrya replay "$out/$version.receipt" || code=$?
  test "$code" -eq "$expected"
done
