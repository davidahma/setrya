#!/usr/bin/env bash
# Install the upstream Linux x64 release on an ephemeral GitHub-hosted runner.
set -euo pipefail

version=8.30.1
sha256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
archive="gitleaks_${version}_linux_x64.tar.gz"
tool_dir="${RUNNER_TEMP:?missing runner temporary directory}/setrya-tools"
mkdir -p "$tool_dir"
cd "$tool_dir"
curl --fail --silent --show-error --location --retry 3 \
  "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${archive}" \
  --output "$archive"
printf '%s  %s\n' "$sha256" "$archive" | sha256sum -c -
tar -xzf "$archive" gitleaks
