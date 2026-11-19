#!/usr/bin/env bash
# Inventory the two registry images and the actual API/worker binary built for the trial.
set -euo pipefail

arch=${1:?supply amd64 or arm64}
out=${2:?supply an output directory}
case "$arch" in amd64|arm64) ;; *) exit 1 ;; esac
mkdir -p "$out"
grype db status
context=setrya/examples/opendesk
docker compose -f "$context/compose.yaml" config --no-interpolate --format json |
  jq -er --arg context "$(cd "$context" && pwd)" '
    .services | if keys != ["app","database","model"] or
      .app.build != {context:$context, dockerfile:"Dockerfile.trial"} or .app.image != null then
      error("unexpected trial image configuration") else . end |
    del(.app) | to_entries[] | if (.value.image | test("^[^[:space:]]+@sha256:[0-9a-f]{64}$")) then
      [.key,.value.image] | @tsv else error("every registry image needs a digest") end' > "$out/images.tsv"

read -r binary brotli < <(python3 -c 'import sys
from setrya.examples.opendesk.verify_report import TRIAL_BINARIES, TRIAL_BUILD
print(TRIAL_BINARIES["linux/" + sys.argv[1]], TRIAL_BUILD["brotli"])' "$arch")
tag="setrya-opendesk-audit:$arch-$$"
trap 'docker image rm "$tag" >/dev/null 2>&1 || true' EXIT
docker buildx build --platform "linux/$arch" --load -t "$tag" -f "$context/Dockerfile.trial" "$context"
app_id=$(docker image inspect "$tag" --format '{{.Id}}')
printf 'app\t%s\n' "$app_id" >> "$out/images.tsv"

status=0
while IFS=$'\t' read -r service image; do
  sbom="$out/$service.sbom.json"
  report="$out/$service.grype.json"
  findings="$out/$service.high-critical.tsv"
  source="registry:$image"
  if [[ "$service" == app ]]; then source="docker:$image"; fi
  printf '\n%s linux/%s %s\n' "$service" "$arch" "$image"
  if ! syft scan --platform "linux/$arch" "$source" -o "syft-json=$sbom" ||
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
  if [[ "$service" == app ]] && ! jq -e --arg binary "$binary" --arg brotli "$brotli" '
    [.artifacts[] | select(.name == "github.com/andybalholm/brotli") | [.version, .type, .locations[].path]]
    == [[$brotli,"go-module","/opendesk"]] and
    ([.files[] | select(.location.path == "/opendesk") | .digests[] | select(.algorithm == "sha256").value] == [$binary])' "$sbom" > /dev/null; then
    echo "Executable digest or Brotli build evidence differs from the trial target" >&2
    status=1
  fi
  jq -r '.matches[] | select(.vulnerability.severity == "High" or .vulnerability.severity == "Critical") |
    [.vulnerability.severity,.vulnerability.id,.artifact.name,.artifact.version] | @tsv' "$report" > "$findings"
  if [[ -s "$findings" ]]; then
    cat "$findings"
    status=1
  else
    echo "No High or Critical matches."
  fi
done < "$out/images.tsv"
exit "$status"
