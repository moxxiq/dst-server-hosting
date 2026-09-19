#!/usr/bin/env bash
# Send one Lua console line to a running shard. Usage: scripts/console.sh Master 'c_announce("hi")'
# The shard must already be running: this will not wait for one to start.
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 <Shard> '<lua line>'" >&2; exit 2; }
if ! printf '%s\n' "$2" | timeout 5 podman exec -i dst-server sh -c 'cat > "/ctl/$1.cmd"' sh "$1"; then
  echo "console: $1 is not running (or did not accept the line within 5s)" >&2
  exit 1
fi
