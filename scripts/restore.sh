#!/usr/bin/env bash
# List backups, or fetch one into data/parked/.
#   scripts/restore.sh                       list local + R2 backups
#   scripts/restore.sh latest                newest R2 zip → data/parked/
#   scripts/restore.sh <name> [--activate]   named zip → parked; --activate replaces the
#                                            active cluster (stop, pre-activate backup, extract, start)
set -Eeuo pipefail
cd "$(dirname "$0")/.."
API=http://127.0.0.1:8081

list() {
  curl -fsS "$API/backups" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for src in ("local", "r2"):
    print("== " + src + " ==")
    for e in d[src]:
        print("{:48} {:7.1f} MB".format(e["name"], e["size"] / 1048576))'
}

[[ $# -gt 0 ]] || { list; exit 0; }
NAME="$1"; shift
if [[ "$NAME" == latest ]]; then
  NAME="$(curl -fsS "$API/backups" | python3 -c 'import json,sys; r=json.load(sys.stdin)["r2"]; print(r[-1]["name"] if r else "")')"
  [[ -n "$NAME" ]] || { echo "no R2 backups" >&2; exit 1; }
fi
SOURCE=r2
[[ -f "data/backups/$NAME" ]] && SOURCE=local
curl -fsS -X POST "$API/restore?source=$SOURCE&name=$NAME" | python3 -m json.tool

if [[ "${1:-}" == --activate ]]; then
  if [[ -r /dev/tty ]]; then
    read -r -p "Replace the active cluster with $NAME? dst-server stops, current cluster is backed up first. [y/N] " answer < /dev/tty
    [[ "$answer" == y ]] || exit 1
  fi
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  curl -fsS -u "${ADMIN_USER:-dst}:${ADMIN_PASSWORD:?ADMIN_PASSWORD missing in .env}" -X POST -F "name=$NAME" \
    http://127.0.0.1:8080/clusters/activate -o /dev/null -w 'admin → %{redirect_url}\n'
fi
