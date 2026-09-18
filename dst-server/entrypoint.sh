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
  local shard="$1" fifo fd
  fifo="$CTL_DIR/$shard.cmd"
  rm -f "$fifo"
  mkfifo "$fifo"
  # O_RDWR so this open never blocks and the shard never sees EOF on stdin.
  exec {fd}<>"$fifo"
  # shellcheck disable=SC2034
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
