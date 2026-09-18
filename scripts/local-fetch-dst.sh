#!/usr/bin/env bash
# Mac/local only. Fills the compose volume dst_dst-install with the DST dedicated
# server using DepotDownloader (anonymous). Run once, then start the stack with
# AUTO_UPDATE=0 in .env. On the VPS dst-server runs steamcmd itself.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

VOLUME="${VOLUME:-dst_dst-install}"
podman build -t localhost/depotdownloader:3.4.0 tools/depotdownloader
podman volume create "$VOLUME" >/dev/null 2>&1 || true
podman run --rm -it --userns=keep-id:uid=1000,gid=1000 \
  -v "$VOLUME:/opt/dst" localhost/depotdownloader:3.4.0 \
  -app 343050 -os linux -osarch 64 -dir /opt/dst -max-downloads 8 "$@"
podman run --rm --userns=keep-id:uid=1000,gid=1000 -v "$VOLUME:/opt/dst" docker.io/library/alpine:3.20 \
  sh -c 'ls -l /opt/dst/bin64/dontstarve_dedicated_server_nullrenderer_x64 && du -sh /opt/dst'
