#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
python_bin="${SETRYA_PYTHON:-$(command -v python3)}"
[[ "$python_bin" = /* ]] || { echo 'Set SETRYA_PYTHON to an absolute interpreter path.' >&2; exit 2; }

if [[ "$(uname -s)" == Darwin ]]; then
  exec /usr/bin/sandbox-exec -p '(version 1) (allow default) (deny network-outbound) (allow network-outbound (remote ip "localhost:*"))' \
    /bin/bash -c '"$1" scripts/verify-loopback.py && exec "$1" -m setrya qualify "${@:2}"' bash "$python_bin" "$@"
elif [[ "$(uname -s)" == Linux ]]; then
  caller_uid=$EUID
  caller_gid=$(command -p id -g)
  caller_groups=$(command -p id -G)
  caller_groups=${caller_groups// /,}
  system_path=/usr/sbin:/usr/bin:/sbin:/bin
  namespace=("$(PATH="$system_path" command -v unshare)" --net)
  if [[ "$caller_uid" != 0 ]]; then namespace=(sudo -n -- "${namespace[@]}"); fi
  # Configure the namespace with system tools. Drop privileges before invoking
  # the caller's interpreter or any code from this checkout.
  exec "${namespace[@]}" /bin/sh -ec '
    PATH=$1
    export PATH
    ip link set lo up
    uid=$2 gid=$3 groups=$4
    shift 4
    if [ "$uid" = 0 ]; then exec setpriv --no-new-privs -- "$@"; fi
    exec setpriv --reuid="$uid" --regid="$gid" --groups="$groups" --no-new-privs -- "$@"
  ' setrya "$system_path" "$caller_uid" "$caller_gid" "$caller_groups" \
    /usr/bin/env "PATH=$PATH" /bin/bash -ec '
      "$1" scripts/verify-loopback.py "$2" "$3" "$4"
      exec "$1" -m setrya qualify "${@:5}"
    ' setrya "$python_bin" "$caller_uid" "$caller_gid" "$caller_groups" "$@"
else
  echo 'Offline qualification harness supports Darwin and Linux.' >&2
  exit 2
fi
