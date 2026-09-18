#!/usr/bin/env bash
# Send one Lua console line to a running shard. Usage: scripts/console.sh Master 'c_announce("hi")'
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 <Shard> '<lua line>'" >&2; exit 2; }
printf '%s\n' "$2" | podman exec -i dst-server sh -c 'cat > "/ctl/$1.cmd"' sh "$1"
