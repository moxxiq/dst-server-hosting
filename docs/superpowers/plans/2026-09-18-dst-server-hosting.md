# DST Server Hosting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the three-container Don't Starve Together hosting stack (`dst-server`, `dst-saves`, `dst-admin`) with `podman compose`, local-first backups gated to Cloudflare R2, a FastAPI admin panel, host scripts and a Vultr bootstrap, verified by real local and VPS smoke runs.

**Architecture:** `dst-server` runs the DST shards from the `cm2network/steamcmd` image as user `steam`, exposing each shard's stdin as a FIFO in the shared `ctl` volume. `dst-saves` polls the shards through those FIFOs, writes local zips on every trigger and uploads to R2 only when the policy says so; it is the only holder of R2 credentials and the only place that extracts cluster zips. `dst-admin` is a Basic-auth FastAPI panel that drives `dst-server` through the rootless podman socket and talks to `dst-saves` over HTTP.

**Tech Stack:** Podman 5 (rootless on VPS, rootful podman machine on the Mac), podman-compose 1.6.0, bash + shellcheck, Python 3.13, FastAPI, uvicorn, boto3 (R2 S3 API), httpx, Jinja2, Pico.css 2.1.1, DepotDownloader 3.4.0 (local dev only).

**Spec:** `docs/superpowers/specs/2026-09-18-dst-server-hosting-design.md`

## Global Constraints

- No unit tests. Verification is shellcheck, `podman compose config`, `python -m compileall`, container smoke runs, and a real DST client join. Every task ends with commands and their expected output.
- Containers never run as root. All three run UID 1000 (`steam`) with `userns_mode: "keep-id:uid=1000,gid=1000"`.
- Base image for DST is `docker.io/cm2network/steamcmd:latest@sha256:45f6515d6c4dcde659c9ad6872bdbeacd1bf5c4e7f241829c4d2f28fb5eda581` (Debian 13, package `libcurl3t64-gnutls`).
- R2 only. Client: boto3 with `endpoint_url=https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, `region_name="auto"`, checksums `when_required`.
- Backup zip name: `<UTC yyyymmddThhmmssZ>_day<NNNN>_<tag>.zip`, `_day<NNNN>` omitted when unknown. Tags: `day`, `empty`, `stop`, `manual`, `pre-activate`. R2 key prefix: `clusters/<CLUSTER_NAME>/backups/`.
- Policy defaults: `R2_EVERY_DAYS=5`, `R2_ON_EMPTY=1`, `R2_KEEP=30`, `LOCAL_KEEP=30`, `AUTO_RESTORE=1`, `POLL_SECONDS=30`.
- Admin auth: HTTP Basic, `ADMIN_USER` default `dst`, empty/missing `ADMIN_PASSWORD` → 500 `ADMIN_PASSWORD is not set` on every request.
- Never generate a world automatically. Never overwrite the active cluster except through the activate flow.
- `adminlist.txt` keeps only lines matching `^KU_[A-Za-z0-9_-]+$`.
- Never copy code from `../steamCMD` (user directive).
- Commit format: Conventional Commits, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Use `git -c user.name=moxxiq -c user.email=dimanavsisto@gmail.com commit` if git identity is unset.
- Local commands run from `/Users/mox/Projects/Claude1/dst_server_hosting`. Use `PODMAN_COMPOSE_PROVIDER=podman-compose podman compose …` on the Mac (docker-compose is the default provider there and is not what the VPS uses).

---

### Task 1: Repository scaffold, `.env.example`, `compose.yaml`

**Files:**
- Create: `.env.example`, `compose.yaml`, `README.md` (stub, finished in Task 8)
- Modify: `.env` (local only, not committed): set `AUTO_UPDATE=0`, add `PODMAN_SOCK=/run/podman/podman.sock`

**Interfaces:**
- Produces: compose service names `dst-saves`, `dst-server`, `dst-admin`; container names identical; volumes `dst-install`, `steam-home`, `ctl`; bind mounts `./data/{saves,parked,backups,mods}`; project name `dst` (volumes appear as `dst_dst-install`, `dst_steam-home`, `dst_ctl`).

- [ ] **Step 1: Write `.env.example`**

```dotenv
# Copy to .env and fill in. Never commit .env (it is in .gitignore).
# On the VPS bootstrap.sh writes this file for you.

# --- Cluster ---
# Folder name under data/saves/. Also the R2 prefix clusters/<CLUSTER_NAME>/.
CLUSTER_NAME=qkation-cooperative
# From https://accounts.klei.com/account/game/servers (Add New Server → copy token).
# Optional when data/saves/<CLUSTER_NAME>/cluster_token.txt already exists.
CLUSTER_TOKEN=
# 1 = run steamcmd app_update on every dst-server start (recommended on the VPS).
# 0 = skip (Mac: DST files come from scripts/local-fetch-dst.sh, steamcmd cannot run there).
AUTO_UPDATE=1

# --- Cloudflare R2 (required, dst-saves refuses to start without all four) ---
# Dashboard → R2 → Manage API tokens → Object Read & Write on the bucket.
# Use the S3 Access Key ID / Secret Access Key pair, not the Cloudflare API token.
R2_ACCOUNT_ID=
R2_BUCKET=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=

# --- Backup policy (dst-saves) ---
# Local zips are written on every trigger (in-game day, server empty, stop, manual).
# R2 upload happens only when R2_EVERY_DAYS in-game days passed since the last upload,
# when the server just emptied (R2_ON_EMPTY=1), or on manual / pre-activate backups.
R2_EVERY_DAYS=5
R2_ON_EMPTY=1
# Newest automatic zips to keep. manual/pre-activate zips are never pruned from R2.
R2_KEEP=30
LOCAL_KEEP=30
# 1 = on start, if data/saves/<CLUSTER_NAME> has no cluster.ini, restore the newest R2 zip.
AUTO_RESTORE=1
POLL_SECONDS=30

# --- Admin panel (dst-admin on :8080) ---
ADMIN_USER=dst
# Required. Empty → every panel request returns 500.
ADMIN_PASSWORD=

# --- Host podman socket mounted into dst-admin ---
# VPS (rootless, user dst): /run/user/<uid of dst>/podman/podman.sock (bootstrap.sh fills it)
# Mac podman machine (rootful): /run/podman/podman.sock
PODMAN_SOCK=/run/user/1000/podman/podman.sock
```

- [ ] **Step 2: Write `compose.yaml`**

```yaml
name: dst

x-common: &common
  restart: unless-stopped
  userns_mode: "keep-id:uid=1000,gid=1000"

services:
  dst-saves:
    <<: *common
    build: ./dst-saves
    image: localhost/dst-saves:latest
    container_name: dst-saves
    environment:
      CLUSTER_NAME: ${CLUSTER_NAME:?CLUSTER_NAME missing in .env}
      R2_ACCOUNT_ID: ${R2_ACCOUNT_ID:?R2_ACCOUNT_ID missing in .env}
      R2_BUCKET: ${R2_BUCKET:?R2_BUCKET missing in .env}
      R2_ACCESS_KEY_ID: ${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID missing in .env}
      R2_SECRET_ACCESS_KEY: ${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY missing in .env}
      R2_EVERY_DAYS: ${R2_EVERY_DAYS:-5}
      R2_ON_EMPTY: ${R2_ON_EMPTY:-1}
      R2_KEEP: ${R2_KEEP:-30}
      LOCAL_KEEP: ${LOCAL_KEEP:-30}
      AUTO_RESTORE: ${AUTO_RESTORE:-1}
      POLL_SECONDS: ${POLL_SECONDS:-30}
    volumes:
      - ./data/saves:/data/klei/DoNotStarveTogether
      - ./data/backups:/data/backups
      - ./data/parked:/data/parked
      - ctl:/ctl
    ports:
      - "127.0.0.1:8081:8081"
    stop_grace_period: 60s

  dst-server:
    <<: *common
    build: ./dst-server
    image: localhost/dst-server:latest
    container_name: dst-server
    platform: linux/amd64
    depends_on:
      - dst-saves
    environment:
      CLUSTER_NAME: ${CLUSTER_NAME:?CLUSTER_NAME missing in .env}
      CLUSTER_TOKEN: ${CLUSTER_TOKEN:-}
      AUTO_UPDATE: ${AUTO_UPDATE:-1}
    volumes:
      - dst-install:/opt/dst
      - steam-home:/home/steam/Steam
      - ./data/saves:/data/klei/DoNotStarveTogether
      - ./data/mods:/data/mods
      - ./data/backups:/data/backups
      - ctl:/ctl
    ports:
      - "10999:10999/udp"
      - "10998:10998/udp"
      - "27016-27018:27016-27018/udp"
      - "8766-8768:8766-8768/udp"
    stop_grace_period: 150s

  dst-admin:
    <<: *common
    build: ./dst-admin
    image: localhost/dst-admin:latest
    container_name: dst-admin
    depends_on:
      - dst-saves
    environment:
      CLUSTER_NAME: ${CLUSTER_NAME:?CLUSTER_NAME missing in .env}
      ADMIN_USER: ${ADMIN_USER:-dst}
      ADMIN_PASSWORD: ${ADMIN_PASSWORD:-}
      DST_SAVES_URL: http://dst-saves:8081
      DST_CONTAINER: dst-server
    volumes:
      - ./data/saves:/data/klei/DoNotStarveTogether
      - ./data/parked:/data/parked
      - ./data/mods:/data/mods
      - ./data/backups:/data/backups
      - ctl:/ctl
      - ${PODMAN_SOCK:?PODMAN_SOCK missing in .env}:/run/podman/podman.sock
    ports:
      - "8080:8080"

volumes:
  dst-install:
  steam-home:
  ctl:
```

- [ ] **Step 3: Write the README stub**

```markdown
# dst-server-hosting

Don't Starve Together dedicated server on one VPS: `podman compose` stack with
`dst-server` (game), `dst-saves` (local + Cloudflare R2 backups), `dst-admin`
(web panel on :8080), a Vultr bootstrap and host scripts.

Work in progress — see `docs/superpowers/specs/` for the design.
```

- [ ] **Step 4: Prepare the local `.env` and data dirs (not committed)**

Run:
```bash
sed -i '' 's/^AUTO_UPDATE=.*/AUTO_UPDATE=0/' .env && grep -q '^PODMAN_SOCK=' .env || printf '\nPODMAN_SOCK=/run/podman/podman.sock\n' >> .env; mkdir -p data/saves data/parked data/backups data/mods; grep -E '^(AUTO_UPDATE|PODMAN_SOCK)=' .env
```
Expected: `AUTO_UPDATE=0` and `PODMAN_SOCK=/run/podman/podman.sock`.

- [ ] **Step 5: Validate the compose file**

Run: `PODMAN_COMPOSE_PROVIDER=podman-compose podman compose config >/dev/null && echo CONFIG_OK`
Expected: `CONFIG_OK` (build contexts do not need to exist for `config`). If podman-compose rejects `name:`, `stop_grace_period`, or `userns_mode`, stop and report — those are load-bearing.

- [ ] **Step 6: Commit**

```bash
git add .env.example compose.yaml README.md
git commit -m "feat: compose stack skeleton and .env.example"
```

---

### Task 2: `dst-server` image and entrypoint

**Files:**
- Create: `dst-server/Dockerfile`, `dst-server/entrypoint.sh`, `dst-server/lib/dst.sh`

**Interfaces:**
- Consumes: env `CLUSTER_NAME`, `CLUSTER_TOKEN`, `AUTO_UPDATE`; mounts from Task 1.
- Produces: FIFOs `/ctl/<Shard>.cmd` (one per shard dir that has `server.ini`), each held open O_RDWR by the entrypoint while that shard runs; reads `/ctl/state.json` (`"cycles": N`) for the stop zip name; writes `/data/backups/<ts>[_dayNNNN]_stop.zip`; regenerates `/opt/dst/mods/dedicated_server_mods_setup.lua`.

- [ ] **Step 1: Write `dst-server/Dockerfile`**

```dockerfile
# DST dedicated server. Base image ships steamcmd with its i386 libraries and the
# non-root `steam` user (UID 1000). Pinned by digest; bump deliberately.
FROM docker.io/cm2network/steamcmd:latest@sha256:45f6515d6c4dcde659c9ad6872bdbeacd1bf5c4e7f241829c4d2f28fb5eda581

USER root
# libcurl3t64-gnutls: the DST binary dlopens libcurl-gnutls.so.4 (Debian 13 name).
# tini: PID 1 that forwards SIGTERM to the entrypoint. inotify-tools: cluster wait.
# zip: stop backups. procps/unzip: debugging convenience.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates libcurl3t64-gnutls tini inotify-tools procps zip unzip \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o steam -g steam \
      /opt/dst /data/klei/DoNotStarveTogether /data/mods /data/backups /ctl

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=0644 lib/dst.sh /usr/local/lib/dst/dst.sh

USER steam
WORKDIR /home/steam
ENV STEAMCMD=/home/steam/steamcmd/steamcmd.sh \
    DST_DIR=/opt/dst \
    KLEI_DIR=/data/klei \
    CTL_DIR=/ctl \
    BACKUP_DIR=/data/backups \
    LOCAL_MODS_DIR=/data/mods

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
```

- [ ] **Step 2: Write `dst-server/lib/dst.sh`**

```bash
#!/usr/bin/env bash
# Helpers sourced by entrypoint.sh. Expects: CLUSTER_NAME CLUSTER_DIR KLEI_DIR
# DST_DIR CTL_DIR BACKUP_DIR LOCAL_MODS_DIR, arrays SHARDS SHARD_FD SHARD_PID.

log() { printf '[dst-server %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# cluster.ini plus at least one <shard>/server.ini.
cluster_ready() {
  [[ -f "$CLUSTER_DIR/cluster.ini" ]] || return 1
  local f
  for f in "$CLUSTER_DIR"/*/server.ini; do [[ -f "$f" ]] && return 0; done
  return 1
}

# Prints shard dir names, master first (is_master = true in server.ini).
discover_shards() {
  local f d master="" others=()
  for f in "$CLUSTER_DIR"/*/server.ini; do
    [[ -f "$f" ]] || continue
    d="$(basename "$(dirname "$f")")"
    if grep -qiE '^\s*is_master\s*=\s*true' "$f"; then master="$d"; else others+=("$d"); fi
  done
  [[ -n "$master" ]] && echo "$master"
  local o
  for o in "${others[@]}"; do echo "$o"; done
}

write_cluster_token() {
  local tok="$CLUSTER_DIR/cluster_token.txt"
  if [[ -s "$tok" ]]; then return 0; fi
  if [[ -z "${CLUSTER_TOKEN:-}" ]]; then
    log "WARNING: no cluster_token.txt and CLUSTER_TOKEN is empty — players cannot join"
    return 0
  fi
  (umask 077 && printf '%s' "$CLUSTER_TOKEN" > "$tok")
  log "wrote cluster_token.txt from CLUSTER_TOKEN"
}

# dedicated_server_mods_setup.lua is derived from every workshop-<id> in the
# cluster's modoverrides.lua files, so operators only edit modoverrides.
sync_mods() {
  local setup="$DST_DIR/mods/dedicated_server_mods_setup.lua" f ids=() d name
  mkdir -p "$DST_DIR/mods"
  local overrides=()
  for f in "$CLUSTER_DIR"/*/modoverrides.lua; do [[ -f "$f" ]] && overrides+=("$f"); done
  if (( ${#overrides[@]} )); then
    mapfile -t ids < <(grep -hoE 'workshop-[0-9]+' "${overrides[@]}" | sed 's/workshop-//' | sort -u)
  fi
  {
    echo "-- generated by dst-server entrypoint $(date -u +%FT%TZ); edit <shard>/modoverrides.lua instead"
    for name in "${ids[@]}"; do echo "ServerModSetup(\"$name\")"; done
  } > "$setup"
  log "workshop mods requested: ${#ids[@]}"
  for d in "$LOCAL_MODS_DIR"/*/; do
    [[ -f "$d/modinfo.lua" ]] || continue
    name="$(basename "$d")"
    rm -rf "${DST_DIR:?}/mods/$name"
    cp -r "$d" "$DST_DIR/mods/$name"
    log "local mod synced: $name"
  done
}

# Write one console line to a shard through the fd we hold on its FIFO.
send_cmd() {
  local shard="$1" line="$2"
  printf '%s\n' "$line" >&"${SHARD_FD[$shard]}"
}

shard_alive() { kill -0 "${SHARD_PID[$1]}" 2>/dev/null; }

# Wait up to $2 seconds for shard $1 to exit, then TERM, then KILL.
wait_exit_or_kill() {
  local shard="$1" deadline="$2" i
  for (( i = 0; i < deadline; i++ )); do
    shard_alive "$shard" || return 0
    sleep 1
  done
  log "$shard did not exit within ${deadline}s — SIGTERM"
  kill -TERM "${SHARD_PID[$shard]}" 2>/dev/null || true
  sleep 5
  if shard_alive "$shard"; then
    log "$shard still alive — SIGKILL"
    kill -KILL "${SHARD_PID[$shard]}" 2>/dev/null || true
  fi
}

# c_save() everywhere, then c_shutdown(true) everywhere, then wait.
stop_all_shards() {
  local s
  for s in "${SHARDS[@]}"; do shard_alive "$s" && { log "$s: c_save()"; send_cmd "$s" "c_save()"; }; done
  sleep 3
  for s in "${SHARDS[@]}"; do shard_alive "$s" && { log "$s: c_shutdown(true)"; send_cmd "$s" "c_shutdown(true)"; }; done
  for s in "${SHARDS[@]}"; do wait_exit_or_kill "$s" 60; done
}

# Local zip after the shards are down. Day comes from dst-saves' state.json.
write_stop_zip() {
  [[ -f "$CLUSTER_DIR/cluster.ini" ]] || return 0
  local day="" ts name
  if [[ -f "$CTL_DIR/state.json" ]]; then
    day="$(sed -nE 's/.*"cycles": *([0-9]+).*/\1/p' "$CTL_DIR/state.json" | head -1)"
  fi
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -n "$day" ]]; then name="${ts}_day$(printf '%04d' "$day")_stop.zip"; else name="${ts}_stop.zip"; fi
  ( cd "$KLEI_DIR/DoNotStarveTogether" \
    && zip -qrX "$BACKUP_DIR/$name" "$CLUSTER_NAME" -x '*/backup/*' '*.DS_Store' '__MACOSX/*' )
  log "stop zip written: $name"
}
```

- [ ] **Step 3: Write `dst-server/entrypoint.sh`**

```bash
#!/usr/bin/env bash
# dst-server lifecycle:
#   steamcmd update (AUTO_UPDATE=1 or binary missing) → wait for cluster files
#   → cluster token → mods sync → one DST process per shard (stdin = /ctl/<shard>.cmd)
#   → wait; on SIGTERM or shard exit: c_save(), c_shutdown(true), stop zip, exit.
# Never generates a world: without cluster.ini it waits for dst-saves/dst-admin.
set -Eeuo pipefail
shopt -s nullglob

# shellcheck source=lib/dst.sh
. /usr/local/lib/dst/dst.sh

CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
AUTO_UPDATE="${AUTO_UPDATE:-1}"
CLUSTER_DIR="$KLEI_DIR/DoNotStarveTogether/$CLUSTER_NAME"
DST_BIN="$DST_DIR/bin64/dontstarve_dedicated_server_nullrenderer_x64"

declare -A SHARD_FD=() SHARD_PID=()
SHARDS=()
STOPPING=0

update_dst() {
  log "steamcmd +app_update 343050 validate → $DST_DIR"
  "$STEAMCMD" +force_install_dir "$DST_DIR" +login anonymous +app_update 343050 validate +quit
}

wait_for_cluster() {
  if cluster_ready; then
    log "cluster '$CLUSTER_NAME' present"
    return 0
  fi
  log "no cluster at $CLUSTER_DIR — waiting for an R2 restore, a zip upload, or the wizard (never generating a world)"
  mkdir -p "$KLEI_DIR/DoNotStarveTogether"
  until cluster_ready; do
    inotifywait -qq -t 60 -r -e create,close_write,moved_to "$KLEI_DIR/DoNotStarveTogether" || true
    cluster_ready || log "still waiting for $CLUSTER_DIR/cluster.ini"
  done
  sleep 5   # let an in-progress extraction finish
  log "cluster detected"
}

launch_shard() {
  local shard="$1" fifo="$CTL_DIR/$shard.cmd" fd
  rm -f "$fifo"
  mkfifo "$fifo"
  # O_RDWR so this open never blocks and the shard never sees EOF on stdin.
  exec {fd}<>"$fifo"
  SHARD_FD[$shard]=$fd
  ( cd "$DST_DIR/bin64" && exec ./dontstarve_dedicated_server_nullrenderer_x64 \
      -persistent_storage_root "$KLEI_DIR" -conf_dir DoNotStarveTogether \
      -cluster "$CLUSTER_NAME" -shard "$shard" \
      < "$fifo" > >(sed -u "s/^/[$shard] /") 2>&1 ) &
  SHARD_PID[$shard]=$!
  log "launched shard $shard (pid ${SHARD_PID[$shard]}, fifo $fifo)"
}

finish() {
  local rc="$1"
  STOPPING=1
  stop_all_shards
  write_stop_zip
  log "exiting rc=$rc"
  exit "$rc"
}

on_signal() {
  log "SIGTERM/SIGINT received — graceful shutdown"
  finish 0
}

main() {
  if [[ ! -x "$DST_BIN" ]]; then
    log "DST binary missing — running steamcmd"
    update_dst
  elif [[ "$AUTO_UPDATE" == "1" ]]; then
    update_dst
  else
    log "AUTO_UPDATE=0 — skipping steamcmd"
  fi
  [[ -x "$DST_BIN" ]] || { log "ERROR: $DST_BIN not found after update"; exit 1; }

  wait_for_cluster
  write_cluster_token
  sync_mods

  mapfile -t SHARDS < <(discover_shards)
  (( ${#SHARDS[@]} )) || { log "ERROR: no shard dirs with server.ini in $CLUSTER_DIR"; exit 1; }

  trap on_signal TERM INT
  local s
  for s in "${SHARDS[@]}"; do launch_shard "$s"; done

  local rc=0
  wait -n "${SHARD_PID[@]}" || rc=$?
  (( STOPPING )) && return 0   # trap already ran finish
  for s in "${SHARDS[@]}"; do
    shard_alive "$s" || log "shard $s exited (rc=$rc) — stopping the others"
  done
  finish "$rc"
}

main "$@"
```

- [ ] **Step 4: shellcheck**

Run: `shellcheck -x dst-server/entrypoint.sh dst-server/lib/dst.sh && echo SHELLCHECK_OK`
Expected: `SHELLCHECK_OK`. (`-x` follows the `source=` directive.)

- [ ] **Step 5: Build the image**

Run: `podman build --platform=linux/amd64 -t localhost/dst-server:latest dst-server && echo BUILD_OK`
Expected: last line `BUILD_OK`.

- [ ] **Step 6: Smoke the wait path (no DST binary, no cluster)**

Run:
```bash
podman run --rm --name dst-wait-smoke --platform=linux/amd64 -e CLUSTER_NAME=smoke -e AUTO_UPDATE=0 \
  -v smoke-install:/opt/dst localhost/dst-server:latest 2>&1 | head -5; podman volume rm smoke-install >/dev/null
```
Expected: `DST binary missing — running steamcmd`, then steamcmd output ending in `Segmentation fault` (Mac only: the 32-bit bootstrap runs under qemu-i386) and the container exits non-zero right there (`set -e` aborts on steamcmd's failure). This proves the binary check and that a failed update aborts the start; the real update path is verified on the VPS in Task 9.

- [ ] **Step 7: Commit**

```bash
git add dst-server
git commit -m "feat(dst-server): image, entrypoint with per-shard FIFOs, graceful stop and stop zip"
```

---

### Task 3: DST files on the Mac via DepotDownloader

**Files:**
- Create: `tools/depotdownloader/Dockerfile`, `scripts/local-fetch-dst.sh`

**Interfaces:**
- Produces: volume `dst_dst-install` containing `bin64/dontstarve_dedicated_server_nullrenderer_x64`, owned by UID 1000.

- [ ] **Step 1: Write `tools/depotdownloader/Dockerfile`**

```dockerfile
# Local-dev helper: DepotDownloader fetches the DST dedicated server (app 343050)
# with anonymous login. Needed on Apple Silicon because steamcmd (32-bit) cannot
# run under podman machine's qemu-i386. Not part of the VPS stack.
FROM mcr.microsoft.com/dotnet/runtime-deps:9.0
ARG DD_VERSION=3.4.0
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && case "$TARGETARCH" in amd64) a=x64 ;; arm64) a=arm64 ;; *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; esac \
 && curl -fsSL -o /tmp/dd.zip "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${DD_VERSION}/DepotDownloader-linux-${a}.zip" \
 && mkdir -p /opt/depotdownloader \
 && unzip -q /tmp/dd.zip -d /opt/depotdownloader \
 && chmod +x /opt/depotdownloader/DepotDownloader \
 && rm /tmp/dd.zip \
 && useradd -u 1000 -m steam \
 && install -d -o steam -g steam /opt/dst
USER steam
ENTRYPOINT ["/opt/depotdownloader/DepotDownloader"]
```

- [ ] **Step 2: Write `scripts/local-fetch-dst.sh`**

```bash
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
```

- [ ] **Step 3: shellcheck and run it**

Run: `shellcheck scripts/local-fetch-dst.sh && chmod +x scripts/local-fetch-dst.sh && scripts/local-fetch-dst.sh`
Expected: DepotDownloader prints depot download progress and ends with `Total downloaded: …`; the final `ls -l` shows the x64 binary owned by uid 1000 and `du` around 1.5–2.5G. Takes several minutes.

- [ ] **Step 4: Confirm the console functions used by dst-saves exist in this DST build**

Run:
```bash
podman run --rm --userns=keep-id:uid=1000,gid=1000 -v dst_dst-install:/opt/dst docker.io/library/alpine:3.20 sh -c \
 'cd /tmp && unzip -qo /opt/dst/data/databundles/scripts.zip "scripts/consolecommands.lua" && grep -nE "^function c_(save|shutdown|announce)\b" scripts/consolecommands.lua'
```
Expected: three lines showing `function c_save(`, `function c_shutdown(`, `function c_announce(`. If `scripts.zip` is missing, check `/opt/dst/data/scripts/consolecommands.lua` instead.

- [ ] **Step 5: Commit**

```bash
git add tools/depotdownloader scripts/local-fetch-dst.sh
git commit -m "feat(tools): DepotDownloader helper to fetch DST on Apple Silicon"
```

---

### Task 4: `dst-saves` service

**Files:**
- Create: `dst-saves/Dockerfile`, `dst-saves/requirements.txt`, `dst-saves/dstsaves/__init__.py`, `dst-saves/dstsaves/config.py`, `dst-saves/dstsaves/state.py`, `dst-saves/dstsaves/shards.py`, `dst-saves/dstsaves/backup.py`, `dst-saves/dstsaves/restore.py`, `dst-saves/dstsaves/r2.py`, `dst-saves/dstsaves/main.py`

**Interfaces:**
- Consumes: FIFOs `/ctl/<Shard>.cmd` from Task 2; `<cluster>/<Shard>/server_log.txt` written by DST.
- Produces: HTTP API on `:8081` — `GET /status`, `GET /backups`, `POST /backup?tag=`, `POST /upload/{name}`, `POST /restore?source=&name=`, `POST /activate?name=`, `DELETE /backups/{source}/{name}`; files `/ctl/state.json` (`{"polled_at","cycles","players","shards"}`), `/data/backups/state.json`, zips in `/data/backups/`.
- Python names used by Task 5's client: JSON shapes documented in each route below.

- [ ] **Step 1: `dst-saves/requirements.txt` and `Dockerfile`**

```text
fastapi~=0.116
uvicorn[standard]~=0.35
boto3~=1.40
```

```dockerfile
FROM docker.io/library/python:3.13-slim
RUN useradd -u 1000 -m steam \
 && install -d -o steam -g steam /data/klei/DoNotStarveTogether /data/backups /data/parked /ctl /app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dstsaves ./dstsaves
USER steam
EXPOSE 8081
CMD ["uvicorn", "dstsaves.main:app", "--host", "0.0.0.0", "--port", "8081", "--log-level", "warning"]
```

- [ ] **Step 2: `dstsaves/__init__.py` (empty) and `dstsaves/config.py`**

```python
"""Environment → Settings. Fails fast with the list of missing variables."""
import os
from dataclasses import dataclass
from pathlib import Path

REQUIRED = ("CLUSTER_NAME", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


@dataclass(frozen=True)
class Settings:
    cluster_name: str
    saves_root: Path
    backups_dir: Path
    parked_dir: Path
    ctl_dir: Path
    r2_account_id: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_every_days: int
    r2_on_empty: bool
    r2_keep: int
    local_keep: int
    auto_restore: bool
    poll_seconds: int

    @property
    def cluster_dir(self) -> Path:
        return self.saves_root / self.cluster_name


def load() -> Settings:
    env = os.environ
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"dst-saves: missing required environment: {', '.join(missing)}")
    return Settings(
        cluster_name=env["CLUSTER_NAME"],
        saves_root=Path(env.get("SAVES_ROOT", "/data/klei/DoNotStarveTogether")),
        backups_dir=Path(env.get("BACKUPS_DIR", "/data/backups")),
        parked_dir=Path(env.get("PARKED_DIR", "/data/parked")),
        ctl_dir=Path(env.get("CTL_DIR", "/ctl")),
        r2_account_id=env["R2_ACCOUNT_ID"],
        r2_bucket=env["R2_BUCKET"],
        r2_access_key_id=env["R2_ACCESS_KEY_ID"],
        r2_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        r2_every_days=int(env.get("R2_EVERY_DAYS", "5")),
        r2_on_empty=env.get("R2_ON_EMPTY", "1") == "1",
        r2_keep=int(env.get("R2_KEEP", "30")),
        local_keep=int(env.get("LOCAL_KEEP", "30")),
        auto_restore=env.get("AUTO_RESTORE", "1") == "1",
        poll_seconds=int(env.get("POLL_SECONDS", "30")),
    )
```

- [ ] **Step 3: `dstsaves/state.py`**

```python
"""Persisted poll/backup state (data/backups/state.json) and atomic JSON writes."""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class State:
    last_cycles: int = -1
    last_players: int = -1
    last_r2_cycles: int = -1
    last_backup: dict | None = None
    last_upload: dict | None = None


def save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    data = json.loads(path.read_text())
    defaults = asdict(State())
    return State(**{k: data.get(k, v) for k, v in defaults.items()})


def save_state(path: Path, state: State) -> None:
    save_json(path, asdict(state))
```

- [ ] **Step 4: `dstsaves/shards.py`**

```python
"""Talk to running DST shards: console lines through /ctl/<Shard>.cmd FIFOs,
answers read back from <Shard>/server_log.txt."""
import errno
import os
import re
import time
from pathlib import Path

LOG_TAIL_BYTES = 65536


def fifo_paths(ctl_dir: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(ctl_dir.glob("*.cmd"))}


def open_fifo_for_write(fifo: Path) -> int | None:
    """Non-blocking write open. None when the shard is not running (no reader) or no FIFO."""
    try:
        return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return None
        raise


def shard_running(fifo: Path) -> bool:
    fd = open_fifo_for_write(fifo)
    if fd is None:
        return False
    os.close(fd)
    return True


def send_line(fifo: Path, line: str) -> bool:
    fd = open_fifo_for_write(fifo)
    if fd is None:
        return False
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return True


def tail_text(path: Path, nbytes: int = LOG_TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def is_master(cluster_dir: Path, shard: str) -> bool:
    ini = cluster_dir / shard / "server.ini"
    if not ini.exists():
        return False
    return re.search(r"^\s*is_master\s*=\s*true", ini.read_text(errors="replace"), re.I | re.M) is not None


def query_shard(cluster_dir: Path, shard: str, fifo: Path, timeout: float = 6.0) -> tuple[int, int] | None:
    """Ask one shard for (cycles, players). None = not running or no answer in time."""
    nonce = f"{time.time_ns():x}"
    lua = (
        f'print("DSTSAVES {nonce} cycles=" .. tostring(TheWorld and TheWorld.state.cycles or -1)'
        f' .. " players=" .. tostring(AllPlayers and #AllPlayers or 0))'
    )
    if not send_line(fifo, lua):
        return None
    pattern = re.compile(rf"DSTSAVES {nonce} cycles=(-?\d+) players=(\d+)")
    log_path = cluster_dir / shard / "server_log.txt"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        m = pattern.search(tail_text(log_path))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def save_all(ctl_dir: Path) -> list[str]:
    """c_save() on every running shard. Returns the shard names that received it."""
    return [shard for shard, fifo in fifo_paths(ctl_dir).items() if send_line(fifo, "c_save()")]


def wait_quiet(cluster_dir: Path, quiet: float = 3.0, max_wait: float = 30.0) -> None:
    """Return once nothing under <Shard>/save/ changed for `quiet` seconds (or after max_wait)."""
    deadline = time.monotonic() + max_wait
    while True:
        newest = 0.0
        for p in cluster_dir.glob("*/save/**/*"):
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        if time.time() - newest >= quiet or time.monotonic() >= deadline:
            return
        time.sleep(1)
```

- [ ] **Step 5: `dstsaves/backup.py`**

```python
"""Backup naming, zip creation, local listing and pruning."""
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)(?:_day(?P<day>\d{4}))?_(?P<tag>[a-z][a-z-]*)\.zip$")
TAG_RE = re.compile(r"^[a-z][a-z-]{0,19}$")
AUTO_TAGS = {"day", "empty", "stop"}


def make_name(ts: datetime, day: int | None, tag: str) -> str:
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    daypart = f"_day{day:04d}" if day is not None and day >= 0 else ""
    return f"{stamp}{daypart}_{tag}.zip"


def parse_name(name: str) -> dict | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    return {"name": name, "ts": m["ts"], "day": int(m["day"]) if m["day"] else None, "tag": m["tag"]}


def zip_cluster(cluster_dir: Path, out: Path) -> int:
    """Zip <cluster>/ with the cluster folder as the archive root. Skips <Shard>/backup/
    (Klei's rotated logs), .DS_Store and __MACOSX. Returns the number of files."""
    count = 0
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(cluster_dir.rglob("*")):
            rel = p.relative_to(cluster_dir.parent)
            parts = rel.parts
            if "__MACOSX" in parts or p.name == ".DS_Store":
                continue
            if len(parts) >= 3 and parts[2] == "backup":
                continue
            if p.is_file():
                z.write(p, rel.as_posix())
                count += 1
    tmp.replace(out)
    return count


def list_local(backups_dir: Path) -> list[dict]:
    out = []
    for p in backups_dir.glob("*.zip"):
        info = parse_name(p.name)
        if info:
            out.append({**info, "size": p.stat().st_size})
    return sorted(out, key=lambda d: d["name"])


def prune_local(backups_dir: Path, keep: int) -> list[str]:
    entries = list_local(backups_dir)
    doomed = entries[:-keep] if keep > 0 and len(entries) > keep else []
    for e in doomed:
        (backups_dir / e["name"]).unlink()
    return [e["name"] for e in doomed]
```

- [ ] **Step 6: `dstsaves/restore.py`**

```python
"""Extract a cluster zip over the active cluster directory."""
import shutil
import zipfile
from pathlib import Path

JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db"}


def _is_junk(name: str) -> bool:
    parts = Path(name).parts
    return any(p in JUNK_DIRS for p in parts) or (len(parts) > 0 and parts[-1] in JUNK_FILES)


def cluster_prefix(z: zipfile.ZipFile) -> str:
    """'' when cluster.ini is at the archive root, '<folder>/' when one level down.
    ValueError for traversal, missing cluster.ini, or deeper nesting."""
    names = [n for n in z.namelist() if not _is_junk(n)]
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts:
            raise ValueError(f"unsafe path in archive: {n}")
    inis = [n for n in names if Path(n).name == "cluster.ini"]
    if not inis:
        raise ValueError("archive contains no cluster.ini")
    ini = min(inis, key=lambda n: len(Path(n).parts))
    if len(Path(ini).parts) > 2:
        raise ValueError(f"cluster.ini is nested too deep: {ini}")
    return ini[: -len("cluster.ini")]


def extract_cluster(zip_path: Path, dest: Path) -> int:
    """Replace `dest` with the cluster stored in `zip_path`. Returns the file count."""
    tmp = dest.parent / f".extracting-{dest.name}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        prefix = cluster_prefix(z)
        for info in z.infolist():
            n = info.filename
            if _is_junk(n) or not n.startswith(prefix) or n == prefix:
                continue
            target = tmp / n[len(prefix):]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return count
```

- [ ] **Step 7: `dstsaves/r2.py`**

```python
"""Cloudflare R2 via the S3 API (boto3). Keys: clusters/<cluster>/backups/<zip name>."""
from pathlib import Path

import boto3
from botocore.config import Config

from .backup import AUTO_TAGS, parse_name
from .config import Settings


class R2:
    def __init__(self, s: Settings):
        self.bucket = s.r2_bucket
        self.prefix = f"clusters/{s.cluster_name}/backups/"
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=s.r2_access_key_id,
            aws_secret_access_key=s.r2_secret_access_key,
            region_name="auto",
            config=Config(
                # R2 rejects the newer default of always sending CRC checksums.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 4, "mode": "standard"},
            ),
        )

    def list(self) -> list[dict]:
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                info = parse_name(obj["Key"][len(self.prefix):])
                if info:
                    out.append({**info, "size": obj["Size"]})
        return sorted(out, key=lambda d: d["name"])

    def upload(self, path: Path) -> str:
        key = self.prefix + path.name
        self.client.upload_file(str(path), self.bucket, key)
        return key

    def download(self, name: str, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        self.client.download_file(self.bucket, self.prefix + name, str(tmp))
        tmp.replace(dest)

    def delete(self, name: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.prefix + name)

    def prune(self, keep: int) -> list[str]:
        """Delete all but the newest `keep` automatic zips. manual/pre-activate are never touched."""
        auto = [e for e in self.list() if e["tag"] in AUTO_TAGS]
        doomed = auto[:-keep] if keep > 0 and len(auto) > keep else []
        for e in doomed:
            self.delete(e["name"])
        return [e["name"] for e in doomed]
```

- [ ] **Step 8: `dstsaves/main.py`**

```python
"""dst-saves: polls the shards, writes local backups on triggers, uploads to R2 by policy,
serves the backup/restore API used by dst-admin and the host scripts."""
import logging
import shutil
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import backup, restore, shards
from .config import Settings, load
from .r2 import R2
from .state import load_state, save_json, save_state

logging.basicConfig(level=logging.INFO, format="[dst-saves %(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dst-saves")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Service:
    def __init__(self, s: Settings):
        self.s = s
        self.r2 = R2(s)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state_path = s.backups_dir / "state.json"
        self.state = load_state(self.state_path)
        self.live: dict = {"polled_at": None, "cycles": -1, "players": 0, "shards": {}}

    # ---- startup restore -------------------------------------------------
    def restore_on_start(self) -> None:
        if not self.s.auto_restore:
            log.info("AUTO_RESTORE=0")
            return
        if (self.s.cluster_dir / "cluster.ini").exists():
            log.info("active cluster present at %s", self.s.cluster_dir)
            return
        entries = self.r2.list()
        if not entries:
            log.info("no active cluster and no R2 backups under %s — waiting for the operator (upload a zip or run the wizard)", self.r2.prefix)
            return
        newest = entries[-1]["name"]
        dest = self.s.parked_dir / newest
        log.info("restoring newest R2 backup %s", newest)
        self.r2.download(newest, dest)
        files = restore.extract_cluster(dest, self.s.cluster_dir)
        log.info("restored %s (%d files) into %s", newest, files, self.s.cluster_dir)

    # ---- polling ---------------------------------------------------------
    def poll_once(self) -> None:
        result: dict = {"polled_at": now_iso(), "cycles": -1, "players": 0, "shards": {}}
        master_cycles = -1
        for name, fifo in shards.fifo_paths(self.s.ctl_dir).items():
            answer = shards.query_shard(self.s.cluster_dir, name, fifo)
            result["shards"][name] = {"alive": answer is not None, "players": answer[1] if answer else 0}
            if answer is None:
                continue
            cycles, players = answer
            result["players"] += players
            if shards.is_master(self.s.cluster_dir, name):
                master_cycles = cycles
            elif cycles > result["cycles"]:
                result["cycles"] = cycles
        if master_cycles >= 0:
            result["cycles"] = master_cycles
        self.live = result
        save_json(self.s.ctl_dir / "state.json", result)

        cycles, players = result["cycles"], result["players"]
        if cycles < 0 or not any(v["alive"] for v in result["shards"].values()):
            return
        st = self.state
        trigger = None
        if st.last_cycles < 0:
            log.info("first reading: day=%s players=%s", cycles, players)
        elif cycles > st.last_cycles:
            trigger = "day"
        elif st.last_players > 0 and players == 0:
            trigger = "empty"
        st.last_cycles, st.last_players = cycles, players
        save_state(self.state_path, st)
        if trigger:
            log.info("trigger %s (day=%s players=%s)", trigger, cycles, players)
            self.run_backup(trigger)

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed")
            self.stop.wait(self.s.poll_seconds)

    # ---- backups ---------------------------------------------------------
    def should_upload(self, tag: str, cycles: int) -> bool:
        st = self.state
        if tag in ("manual", "pre-activate"):
            return True
        if tag == "empty":
            return self.s.r2_on_empty
        if tag == "day":
            if st.last_r2_cycles < 0 or cycles < st.last_r2_cycles:
                return True
            return cycles - st.last_r2_cycles >= self.s.r2_every_days
        return False

    def upload(self, path: Path) -> str:
        key = self.r2.upload(path)
        log.info("uploaded %s", key)
        cycles = self.live["cycles"]
        if cycles >= 0:
            self.state.last_r2_cycles = cycles
        self.state.last_upload = {"name": path.name, "key": key, "at": now_iso()}
        save_state(self.state_path, self.state)
        pruned = self.r2.prune(self.s.r2_keep)
        if pruned:
            log.info("pruned from R2: %s", ", ".join(pruned))
        return key

    def run_backup(self, tag: str, force_upload: bool | None = None) -> dict:
        if not backup.TAG_RE.match(tag):
            raise ValueError(f"bad tag {tag!r}")
        with self.lock:
            cluster = self.s.cluster_dir
            if not (cluster / "cluster.ini").exists():
                raise FileNotFoundError(f"no active cluster at {cluster}")
            if shards.save_all(self.s.ctl_dir):
                shards.wait_quiet(cluster)
            cycles = self.live["cycles"]
            name = backup.make_name(datetime.now(timezone.utc), cycles if cycles >= 0 else None, tag)
            out = self.s.backups_dir / name
            files = backup.zip_cluster(cluster, out)
            size = out.stat().st_size
            log.info("local backup %s (%d files, %.1f MB)", name, files, size / 1048576)
            pruned = backup.prune_local(self.s.backups_dir, self.s.local_keep)
            if pruned:
                log.info("pruned local: %s", ", ".join(pruned))
            upload = force_upload if force_upload is not None else self.should_upload(tag, cycles)
            uploaded = False
            if upload:
                self.upload(out)
                uploaded = True
            self.state.last_backup = {"name": name, "tag": tag, "uploaded": uploaded, "at": now_iso(), "size": size}
            save_state(self.state_path, self.state)
            return {"name": name, "tag": tag, "uploaded": uploaded, "files": files, "size": size}


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc = Service(load())
    app.state.svc = svc
    for d in (svc.s.saves_root, svc.s.backups_dir, svc.s.parked_dir, svc.s.ctl_dir):
        d.mkdir(parents=True, exist_ok=True)
    log.info("cluster=%s policy: every %d days, on_empty=%s, keep r2=%d local=%d",
             svc.s.cluster_name, svc.s.r2_every_days, svc.s.r2_on_empty, svc.s.r2_keep, svc.s.local_keep)
    try:
        svc.restore_on_start()
    except Exception:
        log.exception("restore on start failed — the server will wait for an operator action")
    threading.Thread(target=svc.loop, name="poll", daemon=True).start()
    yield
    svc.stop.set()


app = FastAPI(title="dst-saves", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("request failed: %s %s", request.method, request.url.path)
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


def svc() -> Service:
    return app.state.svc


def check_zip_name(name: str) -> str:
    if Path(name).name != name or not name.endswith(".zip") or len(name) < 5:
        raise HTTPException(400, f"bad zip name {name!r}")
    return name


def local_path(name: str) -> Path:
    p = svc().s.backups_dir / check_zip_name(name)
    if not p.exists():
        raise HTTPException(404, f"no local backup {name}")
    return p


@app.get("/status")
def status():
    s = svc()
    return {
        "live": s.live,
        "cluster_ready": (s.s.cluster_dir / "cluster.ini").exists(),
        "last_backup": s.state.last_backup,
        "last_upload": s.state.last_upload,
        "last_r2_cycles": s.state.last_r2_cycles,
        "policy": {
            "r2_every_days": s.s.r2_every_days,
            "r2_on_empty": s.s.r2_on_empty,
            "r2_keep": s.s.r2_keep,
            "local_keep": s.s.local_keep,
        },
    }


@app.get("/backups")
def backups():
    s = svc()
    return {"local": backup.list_local(s.s.backups_dir), "r2": s.r2.list()}


@app.post("/backup")
def do_backup(tag: str = Query("manual", pattern=r"^[a-z][a-z-]{0,19}$")):
    try:
        return svc().run_backup(tag)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/upload/{name}")
def do_upload(name: str):
    s = svc()
    path = local_path(name)
    with s.lock:
        key = s.upload(path)
    return {"name": name, "key": key}


@app.post("/restore")
def do_restore(source: str = Query(pattern="^(r2|local)$"), name: str = Query(...)):
    s = svc()
    check_zip_name(name)
    dest = s.s.parked_dir / name
    if source == "local":
        shutil.copy2(local_path(name), dest)
    else:
        s.r2.download(name, dest)
    return {"parked": name}


@app.post("/activate")
def do_activate(name: str = Query(...)):
    s = svc()
    path = s.s.parked_dir / check_zip_name(name)
    if not path.exists():
        raise HTTPException(404, f"{name} is not in parked/")
    running = [sh for sh, fifo in shards.fifo_paths(s.s.ctl_dir).items() if shards.shard_running(fifo)]
    if running:
        raise HTTPException(409, f"shards still running: {', '.join(running)} — stop dst-server first")
    with s.lock:
        try:
            files = restore.extract_cluster(path, s.s.cluster_dir)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    log.info("activated %s (%d files)", name, files)
    return {"cluster": str(s.s.cluster_dir), "files": files}


@app.delete("/backups/{source}/{name}")
def do_delete(source: str, name: str):
    s = svc()
    if source == "local":
        local_path(name).unlink()
    elif source == "r2":
        s.r2.delete(check_zip_name(name))
    else:
        raise HTTPException(404, f"unknown source {source}")
    return {"deleted": name, "source": source}
```

- [ ] **Step 9: Compile and build**

Run: `python3 -m compileall -q dst-saves/dstsaves && podman build -t localhost/dst-saves:latest dst-saves && echo BUILD_OK`
Expected: no compile errors, last line `BUILD_OK`.

- [ ] **Step 10: Smoke with a fake shard (no DST needed)**

Uses a throwaway cluster name so the real bucket only gets objects under `clusters/smoke-test/backups/`, deleted at the end. Write the fake shard script to the scratchpad (not committed):

```bash
S=/private/tmp/claude-501/-Users-mox-Projects-Claude1-dst-server-hosting/10c9878c-4138-4b2f-8835-325ebf36648c/scratchpad
mkdir -p "$S/smoke/saves/smoke-test/Master" "$S/smoke/backups" "$S/smoke/parked"
printf '[GAMEPLAY]\nmax_players = 6\n\n[NETWORK]\ncluster_name = smoke\n\n[SHARD]\nshard_enabled = true\n' > "$S/smoke/saves/smoke-test/cluster.ini"
printf '[SHARD]\nis_master = true\n' > "$S/smoke/saves/smoke-test/Master/server.ini"
cat > "$S/smoke/fake-shard.sh" <<'EOF'
#!/bin/sh
# Pretends to be the Master shard: answers DSTSAVES polls into server_log.txt.
# DAY and PLAYERS are read from /smoke/day and /smoke/players so the test can change them.
mkfifo /ctl/Master.cmd 2>/dev/null
exec 3<>/ctl/Master.cmd
while read -r line <&3; do
  nonce=$(printf '%s' "$line" | sed -nE 's/.*DSTSAVES ([0-9a-f]+) .*/\1/p')
  [ -n "$nonce" ] || { echo "cmd: $line"; continue; }
  echo "[00:00:00]: DSTSAVES $nonce cycles=$(cat /smoke/day) players=$(cat /smoke/players)" >> /data/klei/DoNotStarveTogether/smoke-test/Master/server_log.txt
done
EOF
echo 10 > "$S/smoke/day"; echo 1 > "$S/smoke/players"
podman volume rm -f smoke-ctl >/dev/null 2>&1; podman volume create smoke-ctl >/dev/null
podman run -d --rm --name fake-shard --userns=keep-id:uid=1000,gid=1000 -v smoke-ctl:/ctl -v "$S/smoke:/smoke" -v "$S/smoke/saves:/data/klei/DoNotStarveTogether" docker.io/library/alpine:3.20 sh /smoke/fake-shard.sh
set -a; . ./.env; set +a
podman run -d --rm --name saves-smoke --userns=keep-id:uid=1000,gid=1000 -p 127.0.0.1:8081:8081 \
  -e CLUSTER_NAME=smoke-test -e R2_ACCOUNT_ID -e R2_BUCKET -e R2_ACCESS_KEY_ID -e R2_SECRET_ACCESS_KEY \
  -e R2_EVERY_DAYS=5 -e R2_ON_EMPTY=1 -e R2_KEEP=2 -e LOCAL_KEEP=3 -e POLL_SECONDS=5 \
  -v smoke-ctl:/ctl -v "$S/smoke/saves:/data/klei/DoNotStarveTogether" -v "$S/smoke/backups:/data/backups" -v "$S/smoke/parked:/data/parked" \
  localhost/dst-saves:latest
sleep 12; curl -s localhost:8081/status | python3 -m json.tool | head -20
```
Expected: `live.shards.Master.alive` is `true`, `live.cycles` is 10, `live.players` 1, log line `first reading: day=10 players=1`.

Then drive the triggers:
```bash
echo 11 > "$S/smoke/day"; sleep 8; ls "$S/smoke/backups"          # expect <ts>_day0011_day.zip (first day trigger uploads: last_r2_cycles was -1)
echo 0 > "$S/smoke/players"; sleep 8; ls "$S/smoke/backups"        # expect a second zip tagged _empty (uploaded, R2_ON_EMPTY=1)
echo 1 > "$S/smoke/players"; echo 12 > "$S/smoke/day"; sleep 8     # day trigger, 12-11 < 5 → local only
curl -s localhost:8081/backups | python3 -m json.tool | grep -E '"name"|"tag"'
curl -s -X POST 'localhost:8081/backup?tag=manual' ; echo
curl -s -X POST 'localhost:8081/restore?source=r2&name='"$(ls "$S/smoke/backups" | grep _manual | tail -1)" ; echo; ls "$S/smoke/parked"
podman logs saves-smoke 2>&1 | tail -15
```
Expected: local list shows 3 zips at most (`LOCAL_KEEP=3` pruned the oldest), R2 list shows the uploaded ones with `manual` present, `pruned from R2` appears once R2 holds more than 2 automatic zips, the restored manual zip appears in `parked/`.

Cleanup (deletes only the smoke-test prefix in R2):
```bash
for n in $(curl -s localhost:8081/backups | python3 -c 'import json,sys; [print(e["name"]) for e in json.load(sys.stdin)["r2"]]'); do curl -s -X DELETE "localhost:8081/backups/r2/$n"; echo; done
podman rm -f saves-smoke fake-shard; podman volume rm smoke-ctl; rm -rf "$S/smoke"
```
Expected: each delete prints `{"deleted": ..., "source": "r2"}`; containers gone.

- [ ] **Step 11: Commit**

```bash
git add dst-saves
git commit -m "feat(dst-saves): poll shards, local backups on triggers, policy-gated R2 uploads, restore/activate API"
```

---

### Task 5: `dst-admin` panel

**Files:**
- Create: `dst-admin/Dockerfile`, `dst-admin/requirements.txt`, `dst-admin/dstadmin/__init__.py`, `dst-admin/dstadmin/config.py`, `dst-admin/dstadmin/auth.py`, `dst-admin/dstadmin/podman.py`, `dst-admin/dstadmin/saves_client.py`, `dst-admin/dstadmin/fifo.py`, `dst-admin/dstadmin/cluster.py`, `dst-admin/dstadmin/mods.py`, `dst-admin/dstadmin/archive.py`, `dst-admin/dstadmin/main.py`, `dst-admin/dstadmin/templates/{base,dashboard,clusters,wizard,mods,admins,console}.html`, `dst-admin/dstadmin/static/{pico.min.css,app.css,app.js}`

**Interfaces:**
- Consumes: dst-saves API (Task 4), FIFOs (Task 2), libpod REST on `/run/podman/podman.sock`.
- Produces: HTTP on `:8080`; routes listed in `main.py` below. `POST /clusters/activate` (form field `name`) is also used by `scripts/restore.sh --activate` (Task 7).

- [ ] **Step 1: `requirements.txt`, `Dockerfile`, vendored Pico**

```text
fastapi~=0.116
uvicorn[standard]~=0.35
jinja2~=3.1
python-multipart~=0.0.20
httpx~=0.28
```

```dockerfile
FROM docker.io/library/python:3.13-slim
RUN useradd -u 1000 -m steam \
 && install -d -o steam -g steam /data/klei/DoNotStarveTogether /data/backups /data/parked /data/mods /ctl /app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dstadmin ./dstadmin
USER steam
EXPOSE 8080
CMD ["uvicorn", "dstadmin.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
```

Run: `mkdir -p dst-admin/dstadmin/static && curl -fsSL -o dst-admin/dstadmin/static/pico.min.css https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css && head -c 200 dst-admin/dstadmin/static/pico.min.css`
Expected: file starts with `@charset "UTF-8";/*! Pico CSS ✨ v2.1.1`.

- [ ] **Step 2: `dstadmin/__init__.py` (empty), `config.py`, `auth.py`, `fifo.py`**

```python
"""config.py — environment → Settings (ADMIN_PASSWORD is read per request in auth.py)."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    cluster_name: str
    admin_user: str
    saves_url: str
    container: str
    saves_root: Path
    parked_dir: Path
    mods_dir: Path
    backups_dir: Path
    ctl_dir: Path
    podman_sock: str

    @property
    def cluster_dir(self) -> Path:
        return self.saves_root / self.cluster_name


def load() -> Settings:
    env = os.environ
    if not env.get("CLUSTER_NAME"):
        raise SystemExit("dst-admin: CLUSTER_NAME is required")
    return Settings(
        cluster_name=env["CLUSTER_NAME"],
        admin_user=env.get("ADMIN_USER") or "dst",
        saves_url=env.get("DST_SAVES_URL", "http://dst-saves:8081"),
        container=env.get("DST_CONTAINER", "dst-server"),
        saves_root=Path(env.get("SAVES_ROOT", "/data/klei/DoNotStarveTogether")),
        parked_dir=Path(env.get("PARKED_DIR", "/data/parked")),
        mods_dir=Path(env.get("MODS_DIR", "/data/mods")),
        backups_dir=Path(env.get("BACKUPS_DIR", "/data/backups")),
        ctl_dir=Path(env.get("CTL_DIR", "/ctl")),
        podman_sock=env.get("PODMAN_SOCK", "/run/podman/podman.sock"),
    )
```

```python
"""auth.py — HTTP Basic on every request. Empty ADMIN_PASSWORD fails closed with 500."""
import base64
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str):
        super().__init__(app)
        self.user = user.encode()

    async def dispatch(self, request, call_next):
        password = os.environ.get("ADMIN_PASSWORD", "").encode()
        if not password:
            return PlainTextResponse("ADMIN_PASSWORD is not set", status_code=500)
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).partition(b":")
            except ValueError:
                user = pwd = b""
            ok = secrets.compare_digest(user, self.user) and secrets.compare_digest(pwd, password)
        if not ok:
            return PlainTextResponse(
                "authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="dst-admin"'},
            )
        return await call_next(request)
```

```python
"""fifo.py — write console lines into a shard FIFO without blocking."""
import errno
import os
from pathlib import Path


def send_line(fifo: Path, line: str) -> bool:
    """False when the shard is not running (no reader) or the FIFO does not exist."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return True
```

- [ ] **Step 3: `dstadmin/podman.py`**

```python
"""Minimal libpod REST client over the unix socket (no podman CLI in the image)."""
from dataclasses import asdict, dataclass

import httpx


class PodmanError(RuntimeError):
    pass


@dataclass
class ContainerInfo:
    exists: bool
    status: str
    started_at: str | None
    exit_code: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def demux(data: bytes) -> str:
    """Log stream frames: 1 byte stream type, 3 zero bytes, 4-byte big-endian length, payload."""
    chunks = []
    i = 0
    while i + 8 <= len(data):
        n = int.from_bytes(data[i + 4:i + 8], "big")
        chunks.append(data[i + 8:i + 8 + n])
        i += 8 + n
    return b"".join(chunks).decode("utf-8", "replace")


class Podman:
    def __init__(self, sock: str, container: str):
        self.container = container
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=sock),
            base_url="http://podman",
            timeout=httpx.Timeout(10.0, read=200.0),
        )

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code not in (200, 204, 304):
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise PodmanError(f"podman {r.request.method} {r.request.url.path} → {r.status_code}: {msg}")
        return r

    def inspect(self) -> ContainerInfo:
        r = self.client.get(f"/libpod/containers/{self.container}/json")
        if r.status_code == 404:
            return ContainerInfo(False, "missing", None, None)
        state = self._check(r).json()["State"]
        return ContainerInfo(True, state.get("Status", "unknown"), state.get("StartedAt"), state.get("ExitCode"))

    def start(self) -> None:
        self._check(self.client.post(f"/libpod/containers/{self.container}/start"))

    def stop(self, timeout: int = 150) -> None:
        self._check(self.client.post(f"/libpod/containers/{self.container}/stop", params={"timeout": timeout}))

    def restart(self, timeout: int = 150) -> None:
        self._check(self.client.post(f"/libpod/containers/{self.container}/restart", params={"t": timeout}))

    def logs(self, tail: int = 200) -> str:
        r = self._check(self.client.get(
            f"/libpod/containers/{self.container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        ))
        return demux(r.content)
```

- [ ] **Step 4: `dstadmin/saves_client.py`**

```python
"""HTTP client for dst-saves."""
import httpx


class SavesError(RuntimeError):
    pass


class SavesClient:
    def __init__(self, base_url: str):
        self.client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, read=900.0))

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise SavesError(f"dst-saves unreachable: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise SavesError(f"dst-saves {method} {path} → {r.status_code}: {detail}")
        return r.json()

    def status(self) -> dict:
        return self._call("GET", "/status")

    def backups(self) -> dict:
        return self._call("GET", "/backups")

    def backup(self, tag: str = "manual") -> dict:
        return self._call("POST", "/backup", params={"tag": tag})

    def upload(self, name: str) -> dict:
        return self._call("POST", f"/upload/{name}")

    def restore(self, source: str, name: str) -> dict:
        return self._call("POST", "/restore", params={"source": source, "name": name})

    def activate(self, name: str) -> dict:
        return self._call("POST", "/activate", params={"name": name})

    def delete(self, source: str, name: str) -> dict:
        return self._call("DELETE", f"/backups/{source}/{name}")
```

- [ ] **Step 5: `dstadmin/cluster.py`**

```python
"""Active cluster files: shard discovery, cluster.ini read/write for the wizard,
new-cluster creation, adminlist, log tails."""
import configparser
import os
import re
import secrets
from pathlib import Path

WIZARD_DEFAULTS = {
    "cluster_name": "My DST Server",
    "cluster_description": "",
    "cluster_password": "",
    "max_players": 6,
    "game_mode": "endless",
    "pvp": False,
    "cluster_intention": "cooperative",
    "pause_when_empty": True,
    "vote_enabled": True,
    "caves": True,
    "max_snapshots": 6,
}
GAME_MODES = ("survival", "endless", "wilderness")
INTENTIONS = ("cooperative", "social", "competitive", "madness")
KU_RE = re.compile(r"^KU_[A-Za-z0-9_-]+$")

MASTER_SERVER_INI = """[NETWORK]
server_port = 10999

[SHARD]
is_master = true

[STEAM]
master_server_port = 27016
authentication_port = 8766

[ACCOUNT]
encode_user_path = true
"""
CAVES_SERVER_INI = """[NETWORK]
server_port = 10998

[SHARD]
is_master = false
name = Caves
id = {shard_id}

[STEAM]
master_server_port = 27017
authentication_port = 8767

[ACCOUNT]
encode_user_path = true
"""
CAVES_WORLDGEN = 'return {\n  override_enabled = true,\n  preset = "DST_CAVE",\n}\n'
EMPTY_MODOVERRIDES = "return {\n}\n"


def _parser() -> configparser.ConfigParser:
    return configparser.ConfigParser(interpolation=None, strict=False)


def _bool(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def shard_dirs(cluster_dir: Path) -> list[str]:
    """Shard folder names, master first."""
    master, others = [], []
    for ini in sorted(cluster_dir.glob("*/server.ini")):
        cp = _parser()
        cp.read(ini, encoding="utf-8")
        (master if _bool(cp.get("SHARD", "is_master", fallback="false")) else others).append(ini.parent.name)
    return master + others


def summary(cluster_dir: Path) -> dict:
    values, parse_error = read_active_cluster_settings(cluster_dir)
    return {
        "ready": (cluster_dir / "cluster.ini").exists() and bool(shard_dirs(cluster_dir)),
        "path": str(cluster_dir),
        "shards": shard_dirs(cluster_dir),
        "display_name": values["cluster_name"],
        "parse_error": parse_error,
        "has_token": (cluster_dir / "cluster_token.txt").exists(),
    }


def read_active_cluster_settings(cluster_dir: Path) -> tuple[dict, str | None]:
    """Wizard values prefilled from the live cluster.ini. (values, parse_error).
    Returns WIZARD_DEFAULTS when there is no active cluster."""
    ini = cluster_dir / "cluster.ini"
    values = dict(WIZARD_DEFAULTS)
    if not ini.exists():
        return values, None
    cp = _parser()
    try:
        cp.read(ini, encoding="utf-8")
        g = cp.get
        values.update({
            "cluster_name": g("NETWORK", "cluster_name", fallback=values["cluster_name"]),
            "cluster_description": g("NETWORK", "cluster_description", fallback=""),
            "cluster_password": g("NETWORK", "cluster_password", fallback=""),
            "cluster_intention": g("NETWORK", "cluster_intention", fallback="cooperative"),
            "max_players": int(g("GAMEPLAY", "max_players", fallback=6)),
            "game_mode": g("GAMEPLAY", "game_mode", fallback="endless"),
            "pvp": _bool(g("GAMEPLAY", "pvp", fallback="false")),
            "pause_when_empty": _bool(g("GAMEPLAY", "pause_when_empty", fallback="true")),
            "vote_enabled": _bool(g("GAMEPLAY", "vote_enabled", fallback="true")),
            "max_snapshots": int(g("MISC", "max_snapshots", fallback=6)),
            "caves": (cluster_dir / "Caves" / "server.ini").exists(),
        })
    except (configparser.Error, ValueError) as e:
        return dict(WIZARD_DEFAULTS), str(e)
    return values, None


def write_cluster_ini(cluster_dir: Path, v: dict) -> None:
    """Create or update cluster.ini with the wizard values, keeping unrelated keys."""
    ini = cluster_dir / "cluster.ini"
    cp = _parser()
    if ini.exists():
        cp.read(ini, encoding="utf-8")

    def put(section: str, key: str, val) -> None:
        if not cp.has_section(section):
            cp.add_section(section)
        cp.set(section, key, str(val).lower() if isinstance(val, bool) else str(val))

    def default(section: str, key: str, val) -> None:
        if not cp.has_option(section, key):
            put(section, key, val)

    put("GAMEPLAY", "game_mode", v["game_mode"])
    put("GAMEPLAY", "max_players", v["max_players"])
    put("GAMEPLAY", "pvp", v["pvp"])
    put("GAMEPLAY", "pause_when_empty", v["pause_when_empty"])
    put("GAMEPLAY", "vote_enabled", v["vote_enabled"])
    put("NETWORK", "cluster_name", v["cluster_name"])
    put("NETWORK", "cluster_description", v["cluster_description"])
    put("NETWORK", "cluster_password", v["cluster_password"])
    put("NETWORK", "cluster_intention", v["cluster_intention"])
    default("NETWORK", "lan_only_cluster", False)
    default("NETWORK", "offline_cluster", False)
    default("NETWORK", "tick_rate", 15)
    put("MISC", "console_enabled", True)
    put("MISC", "max_snapshots", v["max_snapshots"])
    put("SHARD", "shard_enabled", True)
    default("SHARD", "bind_ip", "127.0.0.1")
    default("SHARD", "master_ip", "127.0.0.1")
    default("SHARD", "master_port", 10888)
    default("SHARD", "cluster_key", secrets.token_hex(8))
    cluster_dir.mkdir(parents=True, exist_ok=True)
    with ini.open("w", encoding="utf-8") as f:
        cp.write(f)


def create_cluster(cluster_dir: Path, v: dict) -> None:
    """New cluster skeleton. DST generates the world on first launch; dst-server writes cluster_token.txt."""
    if (cluster_dir / "cluster.ini").exists():
        raise FileExistsError(f"{cluster_dir} already has a cluster.ini")
    write_cluster_ini(cluster_dir, v)
    master = cluster_dir / "Master"
    master.mkdir()
    (master / "server.ini").write_text(MASTER_SERVER_INI)
    (master / "modoverrides.lua").write_text(EMPTY_MODOVERRIDES)
    if v["caves"]:
        caves = cluster_dir / "Caves"
        caves.mkdir()
        (caves / "server.ini").write_text(CAVES_SERVER_INI.format(shard_id=1_000_000_000 + secrets.randbelow(1_000_000_000)))
        (caves / "worldgenoverride.lua").write_text(CAVES_WORLDGEN)
        (caves / "modoverrides.lua").write_text(EMPTY_MODOVERRIDES)
    (cluster_dir / "adminlist.txt").write_text("")


def read_adminlist(cluster_dir: Path) -> str:
    p = cluster_dir / "adminlist.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_adminlist(cluster_dir: Path, text: str) -> list[str]:
    kept = [line.strip() for line in text.splitlines() if KU_RE.match(line.strip())]
    (cluster_dir / "adminlist.txt").write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return kept


def tail_shard_log(cluster_dir: Path, shard: str, lines: int = 200) -> str:
    p = cluster_dir / shard / "server_log.txt"
    if not p.exists():
        return f"(no {shard}/server_log.txt yet)"
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 65536))
        text = f.read().decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])
```

- [ ] **Step 6: `dstadmin/mods.py`**

```python
"""modoverrides.lua editing (workshop entries) and local mod folders."""
import re
from pathlib import Path

ENTRY_RE = re.compile(r'\[\s*"(workshop-\d+)"\s*\]\s*=\s*\{')
ENABLED_RE = re.compile(r"\benabled\s*=\s*(true|false)")


def _block_end(text: str, open_idx: int) -> int:
    """Index just past the '}' matching the '{' at open_idx. Skips Lua strings and -- comments."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            i += 1
            while i < n and text[i] != c:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces in modoverrides.lua")


def entries(text: str) -> list[dict]:
    out = []
    for m in ENTRY_RE.finditer(text):
        end = _block_end(text, m.end() - 1)
        body = text[m.end():end]
        flags = ENABLED_RE.findall(body)
        out.append({
            "id": m.group(1).removeprefix("workshop-"),
            "enabled": not flags or flags[-1] == "true",
            "start": m.start(),
            "end": end,
        })
    return out


def parse_workshop_id(s: str) -> str:
    """'123456', 'workshop-123456', or a steamcommunity.com URL with ?id=123456."""
    m = re.search(r"[?&]id=(\d+)", s) or re.search(r"(\d{5,})", s)
    if not m:
        raise ValueError(f"no workshop id found in {s!r}")
    return m.group(1)


def add(text: str, wid: str) -> str:
    if any(e["id"] == wid for e in entries(text)):
        return text
    line = f'  ["workshop-{wid}"]={{ configuration_options={{  }}, enabled=true }},\n'
    stripped = text.rstrip()
    if not stripped:
        return "return {\n" + line + "}\n"
    if not stripped.endswith("}"):
        raise ValueError("modoverrides.lua does not end with '}'")
    close = text.rfind("}")
    head = text[:close].rstrip()
    if not head.endswith(("{", ",")):
        head += ","
    return head + "\n" + line + text[close:]


def remove(text: str, wid: str) -> str:
    for e in entries(text):
        if e["id"] != wid:
            continue
        start, end = e["start"], e["end"]
        end += re.match(r"\s*,?[ \t]*\n?", text[end:]).end()
        line_start = text.rfind("\n", 0, start) + 1
        if not text[line_start:start].strip():
            start = line_start
        return text[:start] + text[end:]
    return text


def local_mods(mods_dir: Path) -> list[dict]:
    if not mods_dir.exists():
        return []
    return [{"name": d.name, "valid": (d / "modinfo.lua").exists()} for d in sorted(mods_dir.iterdir()) if d.is_dir()]
```

- [ ] **Step 7: `dstadmin/archive.py`**

```python
"""Zip validation for cluster uploads and extraction for local mod uploads."""
import re
import shutil
import zipfile
from pathlib import Path

JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db"}


def _members(z: zipfile.ZipFile) -> list[str]:
    names = []
    for n in z.namelist():
        parts = Path(n).parts
        if any(p in JUNK_DIRS for p in parts) or (parts and parts[-1] in JUNK_FILES):
            continue
        if n.startswith("/") or ".." in parts:
            raise ValueError(f"unsafe path in archive: {n}")
        names.append(n)
    return names


def validate_cluster_zip(path: Path) -> dict:
    """ValueError unless `path` is a zip with cluster.ini at depth 0 or 1. Returns {"prefix", "shards"}."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip file")
    with zipfile.ZipFile(path) as z:
        names = _members(z)
        inis = [n for n in names if Path(n).name == "cluster.ini"]
        if not inis:
            raise ValueError("no cluster.ini in archive")
        ini = min(inis, key=lambda n: len(Path(n).parts))
        if len(Path(ini).parts) > 2:
            raise ValueError(f"cluster.ini nested too deep: {ini}")
        prefix = ini[: -len("cluster.ini")]
        shards = sorted({
            Path(n[len(prefix):]).parts[0]
            for n in names
            if n.startswith(prefix) and Path(n).name == "server.ini" and len(Path(n[len(prefix):]).parts) == 2
        })
        return {"prefix": prefix, "shards": shards}


def extract_mod_zip(path: Path, mods_dir: Path) -> str:
    """Extract a mod zip into mods_dir/<name>/ with modinfo.lua at the top. Returns the folder name."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip file")
    with zipfile.ZipFile(path) as z:
        names = _members(z)
        infos = [n for n in names if Path(n).name == "modinfo.lua"]
        if not infos:
            raise ValueError("no modinfo.lua in archive")
        top = min(infos, key=lambda n: len(Path(n).parts))
        parts = Path(top).parts
        if len(parts) > 2:
            raise ValueError(f"modinfo.lua nested too deep: {top}")
        prefix = top[: -len("modinfo.lua")]
        raw_name = parts[0] if len(parts) == 2 else path.stem
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_name)
        dest = mods_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for info in z.infolist():
            n = info.filename
            if n not in names or not n.startswith(prefix) or n == prefix:
                continue
            target = dest / n[len(prefix):]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    return name
```

- [ ] **Step 8: `dstadmin/main.py`**

```python
"""dst-admin: FastAPI routes. Logic lives in cluster.py / mods.py / archive.py;
container control in podman.py; backups in saves_client.py."""
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import archive, cluster, mods
from .auth import BasicAuthMiddleware
from .config import load
from .fifo import send_line
from .podman import Podman, PodmanError
from .saves_client import SavesClient, SavesError

settings = load()
app = FastAPI(title="dst-admin")
app.add_middleware(BasicAuthMiddleware, user=settings.admin_user)
BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
podman = Podman(settings.podman_sock, settings.container)
saves = SavesClient(settings.saves_url)

ACTION_ERRORS = (PodmanError, SavesError, ValueError, FileNotFoundError, FileExistsError)
NAV = [("/", "Dashboard"), ("/clusters", "Clusters"), ("/wizard", "Wizard"),
       ("/mods", "Mods"), ("/admins", "Admins"), ("/console", "Console")]


def redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = []
    if msg:
        q.append("msg=" + quote(msg))
    if err:
        q.append("err=" + quote(err))
    return RedirectResponse(path + ("?" + "&".join(q) if q else ""), status_code=303)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, nav=NAV, cluster_name=settings.cluster_name,
               msg=request.query_params.get("msg"), err=request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def run_action(back: str, ok: str, fn):
    """Run fn(); redirect back with a green message or the error text."""
    try:
        result = fn()
    except ACTION_ERRORS as e:
        return redirect(back, err=str(e))
    return redirect(back, msg=ok.format(result=result))


def maybe_restart(restart: bool) -> str:
    if not restart:
        return ""
    podman.restart(150)
    return " — dst-server restarted"


def parked_path(name: str) -> Path:
    if Path(name).name != name or not name.endswith(".zip"):
        raise ValueError(f"bad name {name!r}")
    p = settings.parked_dir / name
    if not p.exists():
        raise FileNotFoundError(f"{name} is not in parked/")
    return p


def shard_or_400(shard: str) -> str:
    if shard not in cluster.shard_dirs(settings.cluster_dir):
        raise ValueError(f"unknown shard {shard!r}")
    return shard


# ---- dashboard ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", shards=cluster.shard_dirs(settings.cluster_dir))


@app.get("/api/status")
def api_status():
    try:
        status, saves_error = saves.status(), None
    except SavesError as e:
        status, saves_error = None, str(e)
    try:
        container, podman_error = podman.inspect().as_dict(), None
    except PodmanError as e:
        container, podman_error = {"exists": False, "status": "unknown", "started_at": None, "exit_code": None}, str(e)
    return {"container": container, "podman_error": podman_error, "saves": status,
            "saves_error": saves_error, "cluster": cluster.summary(settings.cluster_dir)}


@app.get("/api/logs/{source}", response_class=PlainTextResponse)
def api_logs(source: str):
    if source == "container":
        try:
            return podman.logs(200)
        except PodmanError as e:
            return str(e)
    if source in cluster.shard_dirs(settings.cluster_dir):
        return cluster.tail_shard_log(settings.cluster_dir, source)
    raise HTTPException(404, f"unknown log source {source}")


@app.post("/server/{action}")
def server_action(action: str):
    actions = {"start": podman.start, "stop": lambda: podman.stop(150), "restart": lambda: podman.restart(150)}
    if action not in actions:
        raise HTTPException(404)
    return run_action("/", f"dst-server {action}: done", actions[action])


@app.post("/backup")
def backup_now():
    return run_action("/", "backup {result[name]} written, uploaded to R2: {result[uploaded]}",
                      lambda: saves.backup("manual"))


# ---- clusters ----------------------------------------------------------------
@app.get("/clusters", response_class=HTMLResponse)
def clusters_page(request: Request):
    parked = []
    for p in sorted(settings.parked_dir.glob("*.zip")):
        try:
            v = archive.validate_cluster_zip(p)
            parked.append({"name": p.name, "size": p.stat().st_size, "shards": v["shards"], "error": None})
        except ValueError as e:
            parked.append({"name": p.name, "size": p.stat().st_size, "shards": [], "error": str(e)})
    try:
        backups, backups_error = saves.backups(), None
    except SavesError as e:
        backups, backups_error = {"local": [], "r2": []}, str(e)
    return render(request, "clusters.html", active=cluster.summary(settings.cluster_dir),
                  parked=parked, backups=backups, backups_error=backups_error)


@app.post("/clusters/upload")
def clusters_upload(file: UploadFile = File(...)):
    name = Path(file.filename or "upload.zip").name
    if not name.endswith(".zip"):
        return redirect("/clusters", err="only .zip uploads are accepted")
    dest = settings.parked_dir / name
    tmp = dest.with_name(name + ".part")
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        v = archive.validate_cluster_zip(tmp)
    except ValueError as e:
        tmp.unlink()
        return redirect("/clusters", err=f"{name}: {e}")
    tmp.replace(dest)
    return redirect("/clusters", msg=f"parked {name} (shards: {', '.join(v['shards']) or 'none found'})")


@app.post("/clusters/activate")
def clusters_activate(name: str = Form(...)):
    def go():
        archive.validate_cluster_zip(parked_path(name))
        podman.stop(150)
        if (settings.cluster_dir / "cluster.ini").exists():
            saves.backup("pre-activate")
        result = saves.activate(name)
        podman.start()
        return result
    return run_action("/clusters", f"activated {name} ({{result[files]}} files) — dst-server started", go)


@app.post("/clusters/delete")
def clusters_delete(name: str = Form(...)):
    return run_action("/clusters", f"deleted {name}", lambda: parked_path(name).unlink())


@app.get("/clusters/download/{name}")
def clusters_download(name: str):
    try:
        p = parked_path(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(p, filename=name, media_type="application/zip")


@app.post("/clusters/park")
def clusters_park():
    def go():
        r = saves.backup("manual")
        shutil.copy2(settings.backups_dir / r["name"], settings.parked_dir / r["name"])
        return r
    return run_action("/clusters", "parked a copy of the active cluster as {result[name]}", go)


@app.post("/backups/restore")
def backups_restore(source: str = Form(...), name: str = Form(...)):
    return run_action("/clusters", "{result[parked]} is now in parked/ — activate it when ready",
                      lambda: saves.restore(source, name))


@app.post("/backups/upload")
def backups_upload(name: str = Form(...)):
    return run_action("/clusters", "uploaded {result[key]}", lambda: saves.upload(name))


@app.post("/backups/delete")
def backups_delete(source: str = Form(...), name: str = Form(...)):
    return run_action("/clusters", "deleted {result[deleted]} from {result[source]}",
                      lambda: saves.delete(source, name))


# ---- wizard ------------------------------------------------------------------
@app.get("/wizard", response_class=HTMLResponse)
def wizard_page(request: Request):
    values, parse_error = cluster.read_active_cluster_settings(settings.cluster_dir)
    return render(request, "wizard.html", values=values, parse_error=parse_error,
                  exists=(settings.cluster_dir / "cluster.ini").exists(),
                  game_modes=cluster.GAME_MODES, intentions=cluster.INTENTIONS)


@app.post("/wizard")
def wizard_submit(
    cluster_display_name: str = Form(...), cluster_description: str = Form(""),
    cluster_password: str = Form(""), max_players: int = Form(...), game_mode: str = Form(...),
    pvp: bool = Form(False), cluster_intention: str = Form(...), pause_when_empty: bool = Form(False),
    vote_enabled: bool = Form(False), caves: bool = Form(False), max_snapshots: int = Form(6),
    restart: bool = Form(False),
):
    values = {
        "cluster_name": cluster_display_name.strip(), "cluster_description": cluster_description.strip(),
        "cluster_password": cluster_password, "max_players": max_players, "game_mode": game_mode,
        "pvp": pvp, "cluster_intention": cluster_intention, "pause_when_empty": pause_when_empty,
        "vote_enabled": vote_enabled, "caves": caves, "max_snapshots": max_snapshots,
    }

    def go():
        if not values["cluster_name"]:
            raise ValueError("server name is required")
        if game_mode not in cluster.GAME_MODES or cluster_intention not in cluster.INTENTIONS:
            raise ValueError("invalid game mode or intention")
        if not 1 <= max_players <= 64:
            raise ValueError("max players must be 1–64")
        _, parse_error = cluster.read_active_cluster_settings(settings.cluster_dir)
        if parse_error:
            raise ValueError(f"cluster.ini has a parse error, refusing to overwrite it: {parse_error}")
        if (settings.cluster_dir / "cluster.ini").exists():
            cluster.write_cluster_ini(settings.cluster_dir, values)
            return "cluster.ini updated" + maybe_restart(restart)
        cluster.create_cluster(settings.cluster_dir, values)
        return "cluster created — dst-server will generate the world on its next start"
    return run_action("/wizard", "{result}", go)


# ---- mods --------------------------------------------------------------------
def _overrides_path(shard: str) -> Path:
    return settings.cluster_dir / shard / "modoverrides.lua"


def _read_overrides(shard: str) -> str:
    p = _overrides_path(shard)
    return p.read_text(encoding="utf-8") if p.exists() else ""


@app.get("/mods", response_class=HTMLResponse)
def mods_page(request: Request):
    per_shard = {}
    for shard in cluster.shard_dirs(settings.cluster_dir):
        text = _read_overrides(shard)
        try:
            ent, perr = mods.entries(text), None
        except ValueError as e:
            ent, perr = [], str(e)
        per_shard[shard] = {"text": text, "entries": ent, "error": perr}
    return render(request, "mods.html", shards=per_shard, local=mods.local_mods(settings.mods_dir))


@app.post("/mods/add")
def mods_add(workshop: str = Form(...), restart: bool = Form(False)):
    def go():
        wid = mods.parse_workshop_id(workshop)
        for shard in cluster.shard_dirs(settings.cluster_dir):
            _overrides_path(shard).write_text(mods.add(_read_overrides(shard), wid), encoding="utf-8")
        return f"workshop-{wid} added to every shard" + maybe_restart(restart)
    return run_action("/mods", "{result}", go)


@app.post("/mods/remove")
def mods_remove(wid: str = Form(...), restart: bool = Form(False)):
    def go():
        for shard in cluster.shard_dirs(settings.cluster_dir):
            _overrides_path(shard).write_text(mods.remove(_read_overrides(shard), wid), encoding="utf-8")
        return f"workshop-{wid} removed" + maybe_restart(restart)
    return run_action("/mods", "{result}", go)


@app.post("/mods/raw")
def mods_raw(shard: str = Form(...), text: str = Form(...), restart: bool = Form(False)):
    def go():
        shard_or_400(shard)
        mods.entries(text)  # syntax sanity: balanced braces
        _overrides_path(shard).write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        return f"{shard}/modoverrides.lua saved" + maybe_restart(restart)
    return run_action("/mods", "{result}", go)


@app.post("/mods/upload")
def mods_upload(file: UploadFile = File(...), restart: bool = Form(False)):
    tmp = settings.mods_dir / f".upload-{int(time.time())}.zip"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    def go():
        try:
            name = archive.extract_mod_zip(tmp, settings.mods_dir)
        finally:
            tmp.unlink()
        return f"local mod {name} installed" + maybe_restart(restart)
    return run_action("/mods", "{result}", go)


@app.post("/mods/delete-local")
def mods_delete_local(name: str = Form(...)):
    def go():
        if Path(name).name != name or name.startswith("."):
            raise ValueError(f"bad mod name {name!r}")
        target = settings.mods_dir / name
        if not target.is_dir():
            raise FileNotFoundError(f"no local mod {name}")
        shutil.rmtree(target)
        return name
    return run_action("/mods", "removed local mod {result} (takes effect on next restart)", go)


# ---- admins ------------------------------------------------------------------
@app.get("/admins", response_class=HTMLResponse)
def admins_page(request: Request):
    return render(request, "admins.html", text=cluster.read_adminlist(settings.cluster_dir),
                  exists=(settings.cluster_dir / "cluster.ini").exists())


@app.post("/admins")
def admins_save(text: str = Form("")):
    def go():
        if not (settings.cluster_dir / "cluster.ini").exists():
            raise FileNotFoundError("no active cluster")
        return cluster.write_adminlist(settings.cluster_dir, text)
    return run_action("/admins", "saved {result} — DST reads adminlist.txt on the next restart", go)


# ---- console -----------------------------------------------------------------
@app.get("/console", response_class=HTMLResponse)
def console_page(request: Request):
    return render(request, "console.html", shards=cluster.shard_dirs(settings.cluster_dir))


@app.post("/console")
def console_send(shard: str = Form(...), command: str = Form(...)):
    def go():
        shard_or_400(shard)
        line = command.strip()
        if not line or "\n" in line:
            raise ValueError("one non-empty line, please")
        if not send_line(settings.ctl_dir / f"{shard}.cmd", line):
            raise ValueError(f"{shard} is not running")
        time.sleep(2)
        return f"{shard}: {line}"
    return run_action("/console", "sent {result}", go)
```

- [ ] **Step 9: templates**

`templates/base.html`:
```html
<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>dst-admin · {{ cluster_name }}</title>
  <link rel="stylesheet" href="/static/pico.min.css">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="container">
  <nav>
    <ul><li><strong>dst-admin</strong> <small class="muted">{{ cluster_name }}</small></li></ul>
    <ul>
      {% for href, label in nav %}
      <li><a href="{{ href }}" {% if request.url.path == href %}aria-current="page"{% endif %}>{{ label }}</a></li>
      {% endfor %}
    </ul>
  </nav>
</header>
<main class="container">
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="flash bad">{{ err }}</div>{% endif %}
  {% block content %}{% endblock %}
</main>
<script src="/static/app.js"></script>
{% block scripts %}{% endblock %}
</body>
</html>
```

`templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block content %}
<div class="grid">
  <article>
    <header>dst-server</header>
    <p><span class="pill" id="c-state">…</span> <span class="muted" id="c-detail"></span></p>
    <footer class="actions">
      <form method="post" action="/server/start"><button>Start</button></form>
      <form method="post" action="/server/stop" onsubmit="return confirm('Graceful stop: c_save() and c_shutdown(true) on every shard, then a local stop zip. Continue?')"><button class="secondary">Stop</button></form>
      <form method="post" action="/server/restart" onsubmit="return confirm('Restart dst-server? Players get disconnected; the world is saved first.')"><button class="secondary">Restart</button></form>
    </footer>
  </article>
  <article>
    <header>World</header>
    <p>Day <strong id="day">–</strong> · players online <strong id="players">–</strong></p>
    <table id="shards"><thead><tr><th>Shard</th><th>State</th><th>Players</th></tr></thead><tbody></tbody></table>
  </article>
  <article>
    <header>Backups</header>
    <p>Last local: <span id="last-backup">–</span></p>
    <p>Last R2: <span id="last-upload">–</span></p>
    <p class="muted" id="policy"></p>
    <footer class="actions"><form method="post" action="/backup"><button>Backup now → R2</button></form></footer>
  </article>
</div>
<h3>Logs</h3>
<details open><summary>dst-server container</summary><pre class="log" data-log="container"></pre></details>
{% for s in shards %}
<details open><summary>{{ s }} · server_log.txt</summary><pre class="log" data-log="{{ s }}"></pre></details>
{% endfor %}
{% if not shards %}<p class="muted">No active cluster yet: upload a zip on the Clusters page, restore a backup, or run the Wizard.</p>{% endif %}
{% endblock %}
{% block scripts %}<script>dstAdmin.startDashboard();</script>{% endblock %}
```

`templates/clusters.html`:
```html
{% extends "base.html" %}
{% block content %}
<article>
  <header>Active cluster <code>{{ cluster_name }}</code></header>
  {% if active.ready %}
  <p>{{ active.display_name }} · shards: {{ active.shards | join(", ") }} · token: {{ "present" if active.has_token else "missing (set CLUSTER_TOKEN in .env)" }}</p>
  {% if active.parse_error %}<p class="flash bad">cluster.ini parse error: {{ active.parse_error }}</p>{% endif %}
  <footer class="actions">
    <form method="post" action="/clusters/park"><button class="secondary">Park a copy (backup now + copy to parked)</button></form>
  </footer>
  {% else %}
  <p class="muted">No active cluster. Upload a zip below and activate it, restore a backup, or use the Wizard.</p>
  {% endif %}
</article>

<article>
  <header>Parked clusters</header>
  <form method="post" action="/clusters/upload" enctype="multipart/form-data" class="inline">
    <input type="file" name="file" accept=".zip" required> <button>Upload zip</button>
  </form>
  {% if parked %}
  <table>
    <thead><tr><th>File</th><th>Size</th><th>Shards</th><th></th></tr></thead>
    <tbody>
    {% for p in parked %}
    <tr>
      <td><a href="/clusters/download/{{ p.name }}">{{ p.name }}</a></td>
      <td>{{ "%.1f" | format(p.size / 1048576) }} MB</td>
      <td>{% if p.error %}<span class="pill bad">{{ p.error }}</span>{% else %}{{ p.shards | join(", ") or "none" }}{% endif %}</td>
      <td class="actions">
        {% if not p.error %}
        <form method="post" action="/clusters/activate" onsubmit="return confirm('Activate {{ p.name }}? dst-server stops, the current cluster is backed up (pre-activate, kept forever in R2), then replaced.')">
          <input type="hidden" name="name" value="{{ p.name }}"><button>Activate</button></form>
        {% endif %}
        <form method="post" action="/clusters/delete" onsubmit="return confirm('Delete {{ p.name }} from parked?')">
          <input type="hidden" name="name" value="{{ p.name }}"><button class="secondary">Delete</button></form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}<p class="muted">Nothing parked.</p>{% endif %}
</article>

<article>
  <header>Backups</header>
  {% if backups_error %}<p class="flash bad">{{ backups_error }}</p>{% endif %}
  {% for source, label in [("local", "Local (data/backups)"), ("r2", "Cloudflare R2")] %}
  <h5>{{ label }}</h5>
  {% if backups[source] %}
  <table>
    <thead><tr><th>Name</th><th>Day</th><th>Tag</th><th>Size</th><th></th></tr></thead>
    <tbody>
    {% for b in backups[source] | reverse %}
    <tr>
      <td><code>{{ b.name }}</code></td>
      <td>{{ b.day + 1 if b.day is not none else "–" }}</td>
      <td><span class="pill">{{ b.tag }}</span></td>
      <td>{{ "%.1f" | format(b.size / 1048576) }} MB</td>
      <td class="actions">
        <form method="post" action="/backups/restore"><input type="hidden" name="source" value="{{ source }}"><input type="hidden" name="name" value="{{ b.name }}"><button>To parked</button></form>
        {% if source == "local" %}
        <form method="post" action="/backups/upload"><input type="hidden" name="name" value="{{ b.name }}"><button class="secondary">Upload to R2</button></form>
        {% endif %}
        <form method="post" action="/backups/delete" onsubmit="return confirm('Delete {{ b.name }} from {{ source }}?')"><input type="hidden" name="source" value="{{ source }}"><input type="hidden" name="name" value="{{ b.name }}"><button class="secondary">Delete</button></form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}<p class="muted">none</p>{% endif %}
  {% endfor %}
</article>
{% endblock %}
```

`templates/wizard.html`:
```html
{% extends "base.html" %}
{% block content %}
<article>
  <header>{% if exists %}Edit the active cluster's <code>cluster.ini</code>{% else %}Create a new cluster <code>{{ cluster_name }}</code>{% endif %}</header>
  {% if parse_error %}
  <p class="flash bad">cluster.ini could not be parsed: {{ parse_error }}. Fix the file by hand before using this form.</p>
  {% endif %}
  <form method="post" action="/wizard">
    <div class="grid">
      <label>Server name <input name="cluster_display_name" value="{{ values.cluster_name }}" required></label>
      <label>Password <input name="cluster_password" value="{{ values.cluster_password }}"></label>
    </div>
    <label>Description <input name="cluster_description" value="{{ values.cluster_description }}"></label>
    <div class="grid">
      <label>Max players <input type="number" name="max_players" min="1" max="64" value="{{ values.max_players }}"></label>
      <label>Game mode
        <select name="game_mode">{% for m in game_modes %}<option value="{{ m }}" {% if m == values.game_mode %}selected{% endif %}>{{ m }}</option>{% endfor %}</select>
      </label>
      <label>Intention
        <select name="cluster_intention">{% for i in intentions %}<option value="{{ i }}" {% if i == values.cluster_intention %}selected{% endif %}>{{ i }}</option>{% endfor %}</select>
      </label>
      <label>Max snapshots <input type="number" name="max_snapshots" min="1" max="50" value="{{ values.max_snapshots }}"></label>
    </div>
    <fieldset>
      <label><input type="checkbox" name="pvp" {% if values.pvp %}checked{% endif %}> PvP</label>
      <label><input type="checkbox" name="pause_when_empty" {% if values.pause_when_empty %}checked{% endif %}> Pause when empty</label>
      <label><input type="checkbox" name="vote_enabled" {% if values.vote_enabled %}checked{% endif %}> Voting enabled</label>
      <label><input type="checkbox" name="caves" {% if values.caves %}checked{% endif %} {% if exists %}disabled{% endif %}> Caves shard {% if exists %}<small class="muted">(fixed for an existing cluster)</small>{% endif %}</label>
      {% if exists %}<label><input type="checkbox" name="restart"> Restart dst-server after saving</label>{% endif %}
    </fieldset>
    <button {% if parse_error %}disabled{% endif %}>{% if exists %}Save cluster.ini{% else %}Create cluster{% endif %}</button>
  </form>
  {% if not exists %}<p class="muted">The world is generated by DST on the next dst-server start. The cluster token comes from <code>CLUSTER_TOKEN</code> in <code>.env</code>.</p>{% endif %}
</article>
{% endblock %}
```

`templates/mods.html`:
```html
{% extends "base.html" %}
{% block content %}
<article>
  <header>Steam Workshop mods</header>
  <p class="muted">Entries in each shard's <code>modoverrides.lua</code>. dst-server derives <code>dedicated_server_mods_setup.lua</code> from them on every start, so DST downloads whatever is listed here.</p>
  <form method="post" action="/mods/add" class="inline">
    <input name="workshop" placeholder="workshop id or steamcommunity URL" required>
    <label><input type="checkbox" name="restart"> restart</label>
    <button>Add to all shards</button>
  </form>
  {% for shard, info in shards.items() %}
  <h5>{{ shard }}</h5>
  {% if info.error %}<p class="flash bad">{{ info.error }}</p>{% endif %}
  {% if info.entries %}
  <table>
    <thead><tr><th>Workshop id</th><th>Enabled</th><th></th></tr></thead>
    <tbody>
    {% for e in info.entries %}
    <tr>
      <td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id={{ e.id }}" target="_blank" rel="noopener">{{ e.id }}</a></td>
      <td><span class="pill {{ 'good' if e.enabled else 'bad' }}">{{ "on" if e.enabled else "off" }}</span></td>
      <td class="actions"><form method="post" action="/mods/remove"><input type="hidden" name="wid" value="{{ e.id }}"><button class="secondary">Remove</button></form></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}<p class="muted">no workshop entries</p>{% endif %}
  <details>
    <summary>Raw {{ shard }}/modoverrides.lua</summary>
    <form method="post" action="/mods/raw">
      <input type="hidden" name="shard" value="{{ shard }}">
      <textarea name="text" rows="14" spellcheck="false">{{ info.text }}</textarea>
      <label><input type="checkbox" name="restart"> restart</label>
      <button>Save</button>
    </form>
  </details>
  {% endfor %}
  {% if not shards %}<p class="muted">No active cluster.</p>{% endif %}
</article>

<article>
  <header>Local mods (data/mods)</header>
  <p class="muted">Non-Workshop mods. Upload a zip containing <code>modinfo.lua</code> (at the root or inside one folder). Copied into the DST install on every start.</p>
  <form method="post" action="/mods/upload" enctype="multipart/form-data" class="inline">
    <input type="file" name="file" accept=".zip" required>
    <label><input type="checkbox" name="restart"> restart</label>
    <button>Upload mod zip</button>
  </form>
  {% if local %}
  <table><tbody>
  {% for m in local %}
  <tr><td>{{ m.name }}</td><td>{% if not m.valid %}<span class="pill bad">no modinfo.lua</span>{% endif %}</td>
      <td class="actions"><form method="post" action="/mods/delete-local" onsubmit="return confirm('Remove local mod {{ m.name }}?')"><input type="hidden" name="name" value="{{ m.name }}"><button class="secondary">Delete</button></form></td></tr>
  {% endfor %}
  </tbody></table>
  {% else %}<p class="muted">none</p>{% endif %}
</article>
{% endblock %}
```

`templates/admins.html`:
```html
{% extends "base.html" %}
{% block content %}
<article>
  <header>adminlist.txt</header>
  <p class="muted">One Klei user id per line (<code>KU_…</code>). Anything else is dropped on save. Players find their id in the game: Options → Account, or the console prints it on join.</p>
  {% if exists %}
  <form method="post" action="/admins">
    <textarea name="text" rows="10" spellcheck="false" placeholder="KU_xxxxxxxx">{{ text }}</textarea>
    <button>Save</button>
  </form>
  {% else %}<p class="muted">No active cluster.</p>{% endif %}
</article>
{% endblock %}
```

`templates/console.html`:
```html
{% extends "base.html" %}
{% block content %}
<article>
  <header>Console</header>
  <p class="muted">One Lua line goes straight into the shard's stdin. Examples: <code>c_announce("restart in 5 min")</code>, <code>c_save()</code>, <code>c_listallplayers()</code>, <code>c_regenerateworld()</code> (destructive).</p>
  {% if shards %}
  <form method="post" action="/console" class="inline">
    <select name="shard">{% for s in shards %}<option value="{{ s }}">{{ s }}</option>{% endfor %}</select>
    <input name="command" placeholder='c_announce("hello")' required autocomplete="off">
    <button>Send</button>
  </form>
  {% for s in shards %}
  <details open><summary>{{ s }} · server_log.txt</summary><pre class="log" data-log="{{ s }}"></pre></details>
  {% endfor %}
  {% else %}<p class="muted">No active cluster.</p>{% endif %}
</article>
{% endblock %}
{% block scripts %}<script>dstAdmin.startLogs();</script>{% endblock %}
```

- [ ] **Step 10: `static/app.css` and `static/app.js`**

```css
:root { --pico-font-size: 15px; }
.muted { color: var(--pico-muted-color); }
.flash { padding: .6rem 1rem; border-radius: var(--pico-border-radius); margin-bottom: 1rem; }
.flash.ok { background: #14532d; color: #dcfce7; }
.flash.bad { background: #7f1d1d; color: #fee2e2; }
.pill { display: inline-block; padding: .05rem .6rem; border-radius: 999px; font-size: .85em; background: #374151; color: #e5e7eb; }
.pill.good { background: #166534; color: #dcfce7; }
.pill.bad { background: #991b1b; color: #fee2e2; }
.actions { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; }
.actions form, table form { margin: 0; display: inline; }
.actions button, table button { margin: 0; width: auto; padding: .3rem .8rem; font-size: .85rem; }
form.inline { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; }
form.inline input, form.inline select, form.inline button { margin: 0; width: auto; }
form.inline input[type=text], form.inline input:not([type]) { flex: 1; min-width: 14rem; }
pre.log { max-height: 22rem; overflow: auto; font-size: .78rem; line-height: 1.35; white-space: pre-wrap; margin: 0 0 1rem; }
table td, table th { font-size: .9rem; vertical-align: middle; }
article header { font-weight: 600; }
details summary { cursor: pointer; }
textarea { font-family: var(--pico-font-family-monospace); font-size: .85rem; }
```

```javascript
const dstAdmin = {
  async fetchJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " → HTTP " + r.status);
    return r.json();
  },
  async fetchText(url) {
    const r = await fetch(url, { cache: "no-store" });
    return r.ok ? r.text() : "(HTTP " + r.status + ")";
  },
  setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  },
  async refreshStatus() {
    try {
      const s = await this.fetchJSON("/api/status");
      const c = s.container;
      const state = document.getElementById("c-state");
      state.textContent = c.status;
      state.className = "pill " + (c.status === "running" ? "good" : "bad");
      let detail = c.exists ? (c.status === "running" ? "since " + c.started_at : "exit code " + c.exit_code)
                            : "container not created — run: podman compose up -d";
      if (s.podman_error) detail = s.podman_error;
      this.setText("c-detail", detail);
      const live = s.saves ? s.saves.live : null;
      this.setText("day", live && live.cycles >= 0 ? String(live.cycles + 1) : "–");
      this.setText("players", live ? String(live.players) : "–");
      const tbody = document.querySelector("#shards tbody");
      if (tbody) {
        tbody.textContent = "";
        if (live) for (const [name, sh] of Object.entries(live.shards)) {
          const tr = document.createElement("tr");
          const pill = `<span class="pill ${sh.alive ? "good" : "bad"}">${sh.alive ? "live" : "down"}</span>`;
          tr.innerHTML = `<td>${name}</td><td>${pill}</td><td>${sh.players}</td>`;
          tbody.appendChild(tr);
        }
      }
      const lb = s.saves && s.saves.last_backup;
      this.setText("last-backup", lb ? `${lb.name} (${lb.tag}${lb.uploaded ? ", uploaded" : ""})` : "none yet");
      const lu = s.saves && s.saves.last_upload;
      this.setText("last-upload", lu ? `${lu.name} at ${lu.at}` : "none yet");
      if (s.saves) {
        const p = s.saves.policy;
        this.setText("policy", `R2 upload every ${p.r2_every_days} in-game days, on empty: ${p.r2_on_empty ? "yes" : "no"}; keep ${p.r2_keep} in R2, ${p.local_keep} local`);
      }
      if (s.saves_error) this.setText("policy", "dst-saves: " + s.saves_error);
    } catch (e) {
      this.setText("c-detail", String(e));
    }
  },
  async refreshLogs() {
    for (const pre of document.querySelectorAll("pre.log[data-log]")) {
      const text = await this.fetchText("/api/logs/" + encodeURIComponent(pre.dataset.log));
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40 || !pre.textContent;
      pre.textContent = text;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
  },
  startDashboard() {
    this.refreshStatus();
    this.refreshLogs();
    setInterval(() => this.refreshStatus(), 5000);
    setInterval(() => this.refreshLogs(), 5000);
  },
  startLogs() {
    this.refreshLogs();
    setInterval(() => this.refreshLogs(), 5000);
  },
};
```

- [ ] **Step 11: Compile, build, smoke auth**

Run: `python3 -m compileall -q dst-admin/dstadmin && podman build -t localhost/dst-admin:latest dst-admin && echo BUILD_OK`
Expected: `BUILD_OK`.

Run (fail-closed, then 401, then 200 against a stopped stack — dst-saves unreachable is reported, not fatal):
```bash
podman run -d --rm --name admin-smoke -p 127.0.0.1:8080:8080 -e CLUSTER_NAME=smoke -e ADMIN_USER=dst -v /run/podman/podman.sock:/run/podman/podman.sock localhost/dst-admin:latest
sleep 2; curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/           # 500 (no password)
podman rm -f admin-smoke >/dev/null
podman run -d --rm --name admin-smoke -p 127.0.0.1:8080:8080 -e CLUSTER_NAME=smoke -e ADMIN_USER=dst -e ADMIN_PASSWORD=pw -v /run/podman/podman.sock:/run/podman/podman.sock localhost/dst-admin:latest
sleep 2; curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/           # 401
curl -s -u dst:pw -o /dev/null -w '%{http_code}\n' localhost:8080/           # 200
curl -s -u dst:pw localhost:8080/api/status | python3 -m json.tool | head -12
podman rm -f admin-smoke >/dev/null
```
Expected: `500`, `401`, `200`, and JSON with `"container": {"exists": false, "status": "missing", …}` and `"saves_error": "dst-saves unreachable: …"`.

- [ ] **Step 12: Commit**

```bash
git add dst-admin
git commit -m "feat(dst-admin): Basic-auth FastAPI panel — dashboard, clusters, wizard, mods, admins, console"
```

---

### Task 6: Full local stack smoke on the Mac

**Files:** none new. Fixes discovered here are committed against the component they belong to.

**Interfaces:**
- Consumes: everything from Tasks 1–5, `qkation-cooperative.zip` in the project root, `.env` with `AUTO_UPDATE=0` and `PODMAN_SOCK=/run/podman/podman.sock`.

- [ ] **Step 1: Start the stack**

Run: `export PODMAN_COMPOSE_PROVIDER=podman-compose; podman compose up -d --build && podman ps --format '{{.Names}} {{.Status}}'`
Expected: three lines `dst-saves Up …`, `dst-server Up …`, `dst-admin Up …`. `podman volume ls` shows `dst_ctl`, `dst_dst-install`, `dst_steam-home`. If the install volume got a different project prefix, rerun `VOLUME=<actual name> scripts/local-fetch-dst.sh`.

- [ ] **Step 2: Confirm the waiting state**

Run: `podman logs dst-server 2>&1 | tail -3; podman logs dst-saves 2>&1 | tail -3`
Expected: dst-server: `AUTO_UPDATE=0 — skipping steamcmd` then `no cluster at /data/klei/DoNotStarveTogether/qkation-cooperative — waiting …`. dst-saves: `no active cluster and no R2 backups under clusters/qkation-cooperative/backups/ — waiting for the operator`.

- [ ] **Step 3: Upload and activate the test cluster through the panel**

Run (`ADMIN_USER`/`ADMIN_PASSWORD` from `.env`):
```bash
set -a; . ./.env; set +a
curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" -F file=@qkation-cooperative.zip localhost:8080/clusters/upload -o /dev/null -w '%{redirect_url}\n'
curl -s -u "$ADMIN_USER:$ADMIN_PASSWORD" -F name=qkation-cooperative.zip localhost:8080/clusters/activate -o /dev/null -w '%{redirect_url}\n'
```
Expected: first redirect `…/clusters?msg=parked%20qkation-cooperative.zip%20(shards%3A%20Caves%2C%20Master)`, second `…?msg=activated%20…dst-server%20started`. `ls data/saves/qkation-cooperative` shows `cluster.ini Master Caves …`.

- [ ] **Step 4: Watch the shards boot (Rosetta)**

Run: `podman logs -f dst-server 2>&1 | grep -E 'launched shard|Sim paused|Registered|ERROR|error|Segmentation|Downloading|mod' | head -40`
Expected within ~3 minutes: `launched shard Master`, `launched shard Caves`, workshop mods downloading lines, and `[Master] … Sim paused` plus `[Caves] … Sim paused`. If the x64 binary crashes under Rosetta, record the exact error in the README "Local development" section and continue with the VPS smoke (Task 9); the plumbing smoke of Task 4 still covers dst-saves.

- [ ] **Step 5: Verify polling, manual backup, and the panel**

Run:
```bash
sleep 40; curl -s localhost:8081/status | python3 -m json.tool | head -25
curl -s -X POST 'localhost:8081/backup?tag=manual'; echo; ls -la data/backups/
```
Expected: `live.shards.Master.alive: true`, `live.shards.Caves.alive: true`, `live.cycles >= 0`; manual backup returns `"uploaded": true`; the zip appears in `data/backups/` and in the panel's Clusters page under R2. Open `http://localhost:8080` in the browser (Basic auth) and check the dashboard pills, log panes, Mods page (lists the workshop ids from the zip), Admins page, Console (`c_announce("hi")` appears in the Master log).

- [ ] **Step 6: Graceful stop → stop zip**

Run: `time podman compose stop dst-server; podman logs dst-server 2>&1 | tail -8; ls data/backups/ | grep _stop`
Expected: log shows `Master: c_save()`, `Caves: c_save()`, `c_shutdown(true)` for both, `stop zip written: …_dayNNNN_stop.zip`, `exiting rc=0`; the stop zip exists; `time` well under 150 s. Then `podman compose start dst-server` brings both shards back to `Sim paused`.

- [ ] **Step 7: Client join**

Ask the operator to join from the DST client on this Mac: open the in-game console (`~`) and run `c_connect("127.0.0.1", 10999, "qka")`. Expected: joins the world; `curl -s localhost:8081/status` shows `players: 1`; leaving triggers an `empty` backup within ~35 s (`ls data/backups/ | grep _empty`). If UDP does not reach the VM (gvproxy), note it in the README and verify the join in Task 9 instead.

- [ ] **Step 8: Commit any fixes**

```bash
git add -A
git commit -m "fix: adjustments from local full-stack smoke"
```

---

### Task 7: Host scripts

**Files:**
- Create: `scripts/backup.sh`, `scripts/restore.sh`, `scripts/console.sh`

**Interfaces:**
- Consumes: dst-saves API on `127.0.0.1:8081`, dst-admin `POST /clusters/activate` with Basic auth from `.env`, container `dst-server`.

- [ ] **Step 1: `scripts/backup.sh`**

```bash
#!/usr/bin/env bash
# Backup now: local zip + R2 upload. Usage: scripts/backup.sh [tag]   (tag default: manual)
set -Eeuo pipefail
TAG="${1:-manual}"
curl -fsS -X POST "http://127.0.0.1:8081/backup?tag=${TAG}" | python3 -m json.tool
```

- [ ] **Step 2: `scripts/restore.sh`**

```bash
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
    print(f"== {src} ==")
    for e in d[src]:
        print(f"{e[\"name\"]:48} {e[\"size\"] / 1048576:7.1f} MB")'
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
  set -a; . ./.env; set +a
  curl -fsS -u "${ADMIN_USER:-dst}:${ADMIN_PASSWORD:?ADMIN_PASSWORD missing in .env}" -X POST -F "name=$NAME" \
    http://127.0.0.1:8080/clusters/activate -o /dev/null -w 'admin → %{redirect_url}\n'
fi
```

- [ ] **Step 3: `scripts/console.sh`**

```bash
#!/usr/bin/env bash
# Send one Lua console line to a running shard. Usage: scripts/console.sh Master 'c_announce("hi")'
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 <Shard> '<lua line>'" >&2; exit 2; }
printf '%s\n' "$2" | podman exec -i dst-server sh -c 'cat > "/ctl/$1.cmd"' sh "$1"
```

- [ ] **Step 4: shellcheck and try them against the running local stack**

Run:
```bash
chmod +x scripts/*.sh && shellcheck scripts/*.sh && echo SHELLCHECK_OK
scripts/backup.sh && scripts/restore.sh && scripts/console.sh Master 'c_announce("scripts ok")' && sleep 2 && tail -2 data/saves/qkation-cooperative/Master/server_log.txt
```
Expected: `SHELLCHECK_OK`; backup JSON with `"uploaded": true`; the list shows local and R2 entries; the Master log ends with the announcement.

- [ ] **Step 5: Commit**

```bash
git add scripts
git commit -m "feat(scripts): backup, restore and console helpers"
```

---

### Task 8: `bootstrap.sh` and README

**Files:**
- Create: `bootstrap.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: the public repo at `https://github.com/moxxiq/dst-server-hosting.git`; env vars `CLUSTER_NAME CLUSTER_TOKEN R2_ACCOUNT_ID R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY ADMIN_USER ADMIN_PASSWORD` (+ optional policy vars, `FORCE_ENV=1`, `BRANCH`, `REPO_URL`).
- Produces: user `dst`, `/home/dst/dst-server-hosting` with `.env`, running stack, ufw rules.

- [ ] **Step 1: Write `bootstrap.sh`**

```bash
#!/usr/bin/env bash
# bootstrap.sh — fresh Ubuntu 24.04/26.04 x86_64 VPS → running dst-server-hosting stack.
#
#   curl -fsSL https://raw.githubusercontent.com/moxxiq/dst-server-hosting/main/bootstrap.sh | sudo bash
#
# Secrets never live in this file. Provide them as environment variables
# (export VAR=... before the curl, or in a Vultr Startup Script) or answer the
# prompts on a terminal:
#   CLUSTER_NAME CLUSTER_TOKEN R2_ACCOUNT_ID R2_BUCKET R2_ACCESS_KEY_ID
#   R2_SECRET_ACCESS_KEY ADMIN_USER (default dst) ADMIN_PASSWORD
# Optional: AUTO_UPDATE R2_EVERY_DAYS R2_ON_EMPTY R2_KEEP LOCAL_KEEP AUTO_RESTORE
#           REPO_URL BRANCH COMPOSE_VERSION FORCE_ENV=1 (rewrite an existing .env)
# Idempotent: safe to re-run; each step reports what already existed.
set -Eeuo pipefail

DST_USER=dst
REPO_URL="${REPO_URL:-https://github.com/moxxiq/dst-server-hosting.git}"
BRANCH="${BRANCH:-main}"
COMPOSE_VERSION="${COMPOSE_VERSION:-1.6.0}"
HOME_DIR="/home/$DST_USER"
APP_DIR="$HOME_DIR/dst-server-hosting"

log() { printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[bootstrap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "run as root: curl ... | sudo bash"
[[ $(uname -m) == x86_64 ]] || die "DST needs x86_64, this host is $(uname -m)"
# shellcheck disable=SC1091
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || die "Ubuntu expected, found ${PRETTY_NAME:-unknown}"

# prompt VAR "label" [secret] — keeps an existing value, otherwise asks on /dev/tty.
prompt() {
  local var="$1" label="$2" secret="${3:-}" value
  [[ -z "${!var:-}" ]] || return 0
  [[ -r /dev/tty ]] || die "$var is not set and there is no terminal to ask — export it and re-run"
  if [[ -n "$secret" ]]; then
    read -r -s -p "$label: " value < /dev/tty; echo
  else
    read -r -p "$label: " value < /dev/tty
  fi
  [[ -n "$value" ]] || die "$var is required"
  printf -v "$var" '%s' "$value"
}

main() {
log "1/8 packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq podman uidmap slirp4netns passt fuse-overlayfs dbus-user-session \
  pipx git curl unzip jq ufw > /dev/null
log "podman $(podman --version | awk '{print $3}')"

log "2/8 user $DST_USER (rootless podman, lingering)"
if id "$DST_USER" > /dev/null 2>&1; then log "user exists"; else useradd -m -s /bin/bash "$DST_USER"; fi
DST_UID="$(id -u "$DST_USER")"
grep -q "^$DST_USER:" /etc/subuid || usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$DST_USER"
loginctl enable-linger "$DST_USER"
for _ in $(seq 1 30); do [[ -S "/run/user/$DST_UID/bus" ]] && break; sleep 1; done
[[ -S "/run/user/$DST_UID/bus" ]] || die "user session for $DST_USER did not start (no /run/user/$DST_UID/bus)"

as_dst() {
  sudo -u "$DST_USER" -H env \
    XDG_RUNTIME_DIR="/run/user/$DST_UID" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DST_UID/bus" \
    PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    "$@"
}

log "3/8 podman-compose $COMPOSE_VERSION, podman.socket, podman-restart.service"
as_dst pipx install --force "podman-compose==$COMPOSE_VERSION" > /dev/null
ln -sf "$HOME_DIR/.local/bin/podman-compose" /usr/local/bin/podman-compose
as_dst systemctl --user enable --now podman.socket podman-restart.service
PODMAN_SOCK="/run/user/$DST_UID/podman/podman.sock"
[[ -S "$PODMAN_SOCK" ]] || die "podman socket missing at $PODMAN_SOCK"

log "4/8 repository $REPO_URL ($BRANCH)"
if [[ -d "$APP_DIR/.git" ]]; then
  as_dst git -C "$APP_DIR" pull --ff-only
else
  as_dst git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
as_dst mkdir -p "$APP_DIR/data/saves" "$APP_DIR/data/parked" "$APP_DIR/data/backups" "$APP_DIR/data/mods"

log "5/8 .env"
if [[ -f "$APP_DIR/.env" && "${FORCE_ENV:-0}" != 1 ]]; then
  log ".env exists — keeping it (FORCE_ENV=1 to rewrite)"
  # shellcheck disable=SC1091
  ADMIN_USER="$(grep -E '^ADMIN_USER=' "$APP_DIR/.env" | cut -d= -f2- || true)"
else
  prompt CLUSTER_NAME "Cluster folder name (letters, digits, - and _)"
  prompt CLUSTER_TOKEN "Klei cluster token (accounts.klei.com → Servers)" secret
  prompt R2_ACCOUNT_ID "Cloudflare account id"
  prompt R2_BUCKET "R2 bucket"
  prompt R2_ACCESS_KEY_ID "R2 access key id"
  prompt R2_SECRET_ACCESS_KEY "R2 secret access key" secret
  ADMIN_USER="${ADMIN_USER:-dst}"
  prompt ADMIN_PASSWORD "Admin panel password" secret
  [[ "$CLUSTER_NAME" =~ ^[A-Za-z0-9_-]+$ ]] || die "CLUSTER_NAME must match ^[A-Za-z0-9_-]+$"
  install -m 600 -o "$DST_USER" -g "$DST_USER" /dev/stdin "$APP_DIR/.env" <<ENV
CLUSTER_NAME=$CLUSTER_NAME
CLUSTER_TOKEN=$CLUSTER_TOKEN
AUTO_UPDATE=${AUTO_UPDATE:-1}
R2_ACCOUNT_ID=$R2_ACCOUNT_ID
R2_BUCKET=$R2_BUCKET
R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_EVERY_DAYS=${R2_EVERY_DAYS:-5}
R2_ON_EMPTY=${R2_ON_EMPTY:-1}
R2_KEEP=${R2_KEEP:-30}
LOCAL_KEEP=${LOCAL_KEEP:-30}
AUTO_RESTORE=${AUTO_RESTORE:-1}
POLL_SECONDS=30
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASSWORD
PODMAN_SOCK=$PODMAN_SOCK
ENV
  log "wrote $APP_DIR/.env"
fi

log "6/8 build images and start the stack (first run downloads ~2 GB of DST)"
as_dst bash -c "cd '$APP_DIR' && podman compose build && podman compose up -d"

log "7/8 firewall"
ufw allow 22/tcp > /dev/null
ufw allow 8080/tcp > /dev/null
ufw allow 10999/udp > /dev/null
ufw allow 10998/udp > /dev/null
ufw allow 27016:27018/udp > /dev/null
ufw allow 8766:8768/udp > /dev/null
ufw --force enable > /dev/null
log "ufw: 22/tcp 8080/tcp 10999,10998/udp 27016-27018/udp 8766-8768/udp"

log "8/8 done"
IP="$(curl -fsS -4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
cat <<SUMMARY

  Admin panel : http://$IP:8080   (user: ${ADMIN_USER:-dst})
  Stack       : sudo -iu $DST_USER bash -c 'cd $APP_DIR && podman compose ps'
  Logs        : sudo -iu $DST_USER bash -c 'cd $APP_DIR && podman compose logs -f dst-server'
  Backup now  : sudo -iu $DST_USER $APP_DIR/scripts/backup.sh
  Restore     : sudo -iu $DST_USER $APP_DIR/scripts/restore.sh

  With an empty data/saves the stack restores the newest R2 backup; if R2 is
  empty, upload a cluster zip or run the Wizard in the panel. Restrict :8080
  to your IP in the Vultr firewall — the panel is plain HTTP with Basic auth.

SUMMARY
}

# Whole script is parsed before main runs, so `curl | bash` cannot be confused by
# commands that read stdin; prompts use /dev/tty.
main "$@" < /dev/null
```

- [ ] **Step 2: shellcheck and a dry syntax run**

Run: `chmod +x bootstrap.sh && shellcheck bootstrap.sh && bash -n bootstrap.sh && echo BOOTSTRAP_LINT_OK`
Expected: `BOOTSTRAP_LINT_OK`. (Real execution happens on the VPS in Task 9.)

- [ ] **Step 3: Write the full `README.md`**

```markdown
# dst-server-hosting

Don't Starve Together dedicated server on a single VPS, managed with `podman compose`:

| Container | Role |
| --- | --- |
| `dst-server` | DST shards (Master + Caves, or whatever the cluster has) from `cm2network/steamcmd`, user `steam`, steamcmd update on start |
| `dst-saves` | Polls the shards; local zip on every in-game day, on server-empty, on stop; Cloudflare R2 upload every 5 in-game days / on empty / manual; restores the newest R2 backup on a fresh host |
| `dst-admin` | Web panel on `:8080` (HTTP Basic): start/stop/restart, logs, cluster upload → park → activate, backups, wizard, mods, admin list, console |

## Fresh Vultr VPS (Ubuntu 26.04 or 24.04, x86_64, ≥ 2 GB RAM)

```bash
curl -fsSL https://raw.githubusercontent.com/moxxiq/dst-server-hosting/main/bootstrap.sh | sudo bash
```

The script asks for the cluster name, Klei cluster token, R2 keys and the admin password
(or reads them from exported variables — that is how a Vultr Startup Script runs it):

```bash
export CLUSTER_NAME=my-world CLUSTER_TOKEN=pds-g^KU_... \
       R2_ACCOUNT_ID=... R2_BUCKET=dst R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
       ADMIN_USER=dst ADMIN_PASSWORD=...
curl -fsSL https://raw.githubusercontent.com/moxxiq/dst-server-hosting/main/bootstrap.sh | sudo -E bash
```

It installs podman + podman-compose, creates the rootless `dst` user, clones this repo to
`/home/dst/dst-server-hosting`, writes `.env`, builds and starts the stack, and opens the
firewall (22/tcp, 8080/tcp, DST UDP ports). Re-running is safe.

First boot: if R2 already holds a backup under `clusters/<CLUSTER_NAME>/backups/`, the
newest one is restored automatically. Otherwise open the panel and either upload a cluster
zip (park → activate) or create one with the Wizard. The server never generates a world on
its own.

Prerequisites: a cluster token from https://accounts.klei.com/account/game/servers and an
R2 bucket with an API token scoped to Object Read & Write (use the S3 access key pair).

## Panel

`http://<vps>:8080`, Basic auth (`ADMIN_USER`, `ADMIN_PASSWORD` from `.env`). The panel is
plain HTTP: restrict port 8080 to your IP in the Vultr firewall.

- **Dashboard** — container and shard state, day, players, last backups, start/stop/restart,
  backup now, live logs.
- **Clusters** — upload a zip (the folder with `cluster.ini`, zipped with or without a
  wrapping folder), activate it, download or delete parked zips, restore local/R2 backups
  into parked, upload a local backup to R2, park a copy of the active cluster.
- **Wizard** — edit the active `cluster.ini` (prefilled) or create a new cluster.
- **Mods** — workshop ids per shard (`modoverrides.lua`), add/remove, raw editor, local mod
  zips. `dst-server` regenerates `dedicated_server_mods_setup.lua` from these on every start.
- **Admins** — `adminlist.txt`, one `KU_…` id per line.
- **Console** — one Lua line into a shard's stdin.

## Backups

- Every trigger writes `data/backups/<UTC>_day<NNNN>_<tag>.zip` (tags `day`, `empty`,
  `stop`, `manual`, `pre-activate`). Newest 30 are kept locally.
- R2 receives a zip when 5 in-game days passed since the last upload (`R2_EVERY_DAYS`),
  when the server just became empty (`R2_ON_EMPTY=1`), and for `manual` / `pre-activate`.
  Newest 30 automatic zips stay in R2; manual and pre-activate zips are never pruned.
- `scripts/backup.sh [tag]`, `scripts/restore.sh [name|latest] [--activate]`,
  `scripts/console.sh <Shard> '<lua>'` run on the VPS as the `dst` user.
- Before destroying a VPS run `scripts/backup.sh` so the last state is in R2.

## Operating

```bash
sudo -iu dst
cd ~/dst-server-hosting
podman compose ps
podman compose logs -f dst-server        # shard output is prefixed [Master] / [Caves]
podman compose stop dst-server           # graceful: c_save + c_shutdown per shard, stop zip
podman compose up -d --build             # after git pull
```

Containers restart on reboot through the user `podman-restart.service` (enabled by the
bootstrap). Klei updates: `dst-server` runs `steamcmd +app_update` on every start
(`AUTO_UPDATE=1`), so a Restart from the panel applies a new game version.

## Local development (Apple Silicon Mac)

The podman machine runs x86_64 code through Rosetta, but steamcmd's 32-bit bootstrap goes
through qemu and segfaults. Fetch DST once with DepotDownloader instead and disable
steamcmd:

```bash
scripts/local-fetch-dst.sh                        # fills the dst_dst-install volume
# .env: AUTO_UPDATE=0 and PODMAN_SOCK=/run/podman/podman.sock (rootful podman machine)
PODMAN_COMPOSE_PROVIDER=podman-compose podman compose up -d --build
```

Join from the game on the same Mac: console `c_connect("127.0.0.1", 10999, "<password>")`.

## Layout

```
bootstrap.sh        one-shot VPS provisioning (public, no secrets)
compose.yaml        the stack; .env holds secrets and per-host values
dst-server/         Dockerfile, entrypoint.sh, lib/dst.sh
dst-saves/          Python service: polling, backups, R2, restore, API on :8081
dst-admin/          FastAPI panel on :8080
scripts/            backup.sh restore.sh console.sh local-fetch-dst.sh
tools/depotdownloader/  local-dev image
data/               saves/ parked/ backups/ mods/  (gitignored)
```
```

- [ ] **Step 4: Commit and push**

```bash
git add bootstrap.sh README.md
git commit -m "feat(bootstrap): idempotent VPS provisioning script and README"
git push origin main
```

---

### Task 9: VPS smoke (needs a fresh Vultr VPS from the operator)

**Files:** none new; fixes go to their components.

- [ ] **Step 1: Ask the operator for a fresh Ubuntu 26.04 x86_64 VPS (≥ 2 GB RAM) and root SSH access, then run the one-liner from the README there** with the real values exported (the operator types the secrets or pastes them into a Vultr Startup Script).

- [ ] **Step 2: Verify**

Run on the VPS:
```bash
sudo -iu dst bash -c 'cd ~/dst-server-hosting && podman compose ps && podman compose logs --tail 20 dst-server'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/      # 401
```
Expected: three containers Up; dst-server log shows steamcmd finishing with `Success! App '343050' fully installed.` and either the shards reaching `Sim paused` (R2 had a backup) or `waiting for an R2 restore, a zip upload, or the wizard`.

- [ ] **Step 3: Upload → activate → join from the real game (Internet)**, then `sudo reboot` and confirm `podman ps` shows the stack back within two minutes (`podman-restart.service`).

- [ ] **Step 4: Re-run the bootstrap one-liner** — expected: exits 0, prints "exists/keeping" lines, stack unchanged.

- [ ] **Step 5: Commit fixes, push, and hand the operator the final one-liner.**
