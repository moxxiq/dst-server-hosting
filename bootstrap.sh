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
  ( : < /dev/tty ) 2>/dev/null || die "$var is not set and there is no terminal to ask — export it and re-run"
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
if id "$DST_USER" > /dev/null 2>&1; then
  log "user exists (uid $(id -u "$DST_USER"))"
elif useradd -u 1000 -m -s /bin/bash "$DST_USER" 2>/dev/null; then
  log "created $DST_USER at uid 1000"
else
  # UID 1000 is taken (common on Ubuntu cloud images, which ship an `ubuntu` user).
  # keep-id maps whatever uid dst gets onto the images' steam user, so any uid works.
  useradd -m -s /bin/bash "$DST_USER"
  log "created $DST_USER at uid $(id -u "$DST_USER") (1000 was taken)"
fi
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
