#!/usr/bin/env bash
# Read the pinned images as registry data. The package inventory includes Go build dependencies.
set -euo pipefail

arch=${1:?supply amd64 or arm64}
out=${2:?supply an output directory}
case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac
mkdir -p "$out"
grype db status
docker compose -f examples/opendesk/compose.yaml config --no-interpolate --format json |
  jq -er '.services | to_entries[] | if (.key | test("^[a-zA-Z0-9_-]+$")) and
    (.value.image | test("^[^[:space:]]+@sha256:[0-9a-f]{64}$")) then
    [.key,.value.image] | @tsv else error("every service needs a digest-pinned image") end' > "$out/images.tsv"

known_image='ghcr.io/useopendesk/opendesk:v0.4.14@sha256:cb60703a043dc2afa9fb3266e93079813be3e171ed70177e2d4ac47cb266f4d8'
status=0
while IFS=$'\t' read -r service image; do
  sbom="$out/$service.sbom.json"
  report="$out/$service.grype.json"
  findings="$out/$service.high-critical.tsv"
  printf '\n%s linux/%s %s\n' "$service" "$arch" "$image"
  if ! syft scan --platform "linux/$arch" "registry:$image" -o "syft-json=$sbom" ||
     ! jq -e '(.artifacts | type == "array" and length > 0)' "$sbom" > /dev/null ||
     ! grype "sbom:$sbom" -o "json=$report"; then
    echo "Could not scan $service" >&2
    status=1
    continue
  fi
  if ! jq -e --arg arch "$arch" --arg image "$image" '
    .source.target.os == "linux" and .source.target.architecture == $arch and
    .source.target.userInput == $image and .descriptor.db.status.valid == true and
    (.matches | type == "array")' "$report" > /dev/null; then
    echo "Incomplete or unexpected scan for $service" >&2
    status=1
    continue
  fi

  # Keep the previously identified OpenDesk module match visible in both the report and the job log.
  jq -r --arg image "$image" --arg known "$known_image" --arg service "$service" '
    .matches[] | select(.vulnerability.severity == "High" or .vulnerability.severity == "Critical") |
    (if $service == "app" and $image == $known and .vulnerability.id == "GO-2026-6888" and
        .vulnerability.namespace == "govulndb:language:go" and .vulnerability.severity == "Critical" and
        .artifact.type == "go-module" and .artifact.name == "github.com/andybalholm/brotli" and
        .artifact.version == "v1.2.2" and ([.artifact.locations[].path] | unique) == ["/opendesk"]
      then "KNOWN" else "REVIEW" end) as $decision |
    [$decision,.vulnerability.severity,.vulnerability.id,.artifact.name,.artifact.version] | @tsv' \
    "$report" > "$findings"
  if [[ -s "$findings" ]]; then
    cat "$findings"
    if grep -q '^REVIEW' "$findings"; then status=1; fi
  else
    echo "No High or Critical matches."
  fi
done < "$out/images.tsv"
exit "$status"
