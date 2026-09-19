#!/usr/bin/env bash
# Send one Lua console line to a running shard. Usage: scripts/console.sh Master 'c_announce("hi")'
# The shard must already be running: this will not wait for one to start.
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 <Shard> '<lua line>'" >&2; exit 2; }
# The shard's stdin FIFO only exists while dst-server is running that shard.
# `timeout` runs inside the container (Debian coreutils) so the blocked writer
# dies with it; the host-side timeout is only a backstop for a hung podman.
# shellcheck disable=SC2016  # $1 is expanded by the inner `sh -c`, not by this shell.
if ! printf '%s\n' "$2" | timeout 10 podman exec -i dst-server \
     timeout 5 sh -c 'test -p "/ctl/$1.cmd" || { echo "no such running shard: $1" >&2; exit 3; }; cat > "/ctl/$1.cmd"' sh "$1"; then
  echo "console: could not send to $1 — is dst-server running that shard?" >&2
  exit 1
fi
