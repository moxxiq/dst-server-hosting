# DST dedicated-server hosting — design

**Status:** approved in chat on 2026-09-18 (design + backup-policy refinement). Implementation plan follows via `superpowers:writing-plans`.

**Goal:** a from-scratch, single-VPS Don't Starve Together (DST) dedicated-server stack managed with `podman compose`: three containers (`dst-server`, `dst-saves`, `dst-admin`), local + Cloudflare R2 backups, a web admin panel, a one-line Vultr bootstrap, and host scripts for backup/restore. Public repo: `github.com/moxxiq/dst-server-hosting`.

**Not derived from earlier attempts** in sibling folders (user directive). Only DST/Steam/Podman facts are reused.

---

## 1. Locked decisions

| Topic | Decision |
| --- | --- |
| Container engine | Rootless Podman, `podman compose` (podman-compose 1.6.0 via pipx on VPS; docker-compose provider on Mac is fine) |
| Base image for DST | `docker.io/cm2network/steamcmd:latest` pinned by digest. Debian 13, i386 libs present, ships `steam` user UID 1000. Runs as `steam`, never root. |
| VPS | Vultr, Ubuntu 26.04 LTS, x86_64, ≥2 GB RAM. Bootstrap also works on 24.04. |
| Local dev | Apple Silicon Mac, podman machine with Rosetta. steamcmd (32-bit) segfaults under `qemu-i386`, so DST files are fetched with DepotDownloader instead; the 64-bit DST binary runs under Rosetta. |
| Backup destination | Cloudflare R2 only (S3 API via boto3). No other providers. |
| Backup triggers | in-game day rollover, server became empty, SIGTERM stop, manual, pre-activate |
| R2 policy | upload every `R2_EVERY_DAYS` (5) in-game days, on empty (`R2_ON_EMPTY=1`), on manual/pre-activate. Stop zips stay local. |
| Retention | local newest 30; R2 newest 30 automatic, `manual`/`pre-activate` never pruned |
| Cluster creation | never auto-generated. Operator uploads a zip, restores a backup, or runs the wizard. |
| Uploads | park-and-pick: zips land in `data/parked/`, operator activates one explicitly |
| Admin auth | HTTP Basic. `ADMIN_USER` default `dst`. Missing `ADMIN_PASSWORD` → every endpoint returns 500 (fail-closed). |
| adminlist.txt | KU_ IDs only, one per line |
| Mods | required. Workshop mods derived from `modoverrides.lua`; local mods from `data/mods/`. |
| Tests | no unit tests. Verification = shellcheck, `podman compose config`, local smoke, VPS smoke, real client join. |
| Web stack | FastAPI + Jinja2 + vendored Pico.css (dark) + small vanilla JS. No build step. |

---

## 2. Architecture

```
                    Vultr VPS (Ubuntu 26.04, rootless podman, user `dst`)
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  :8080 ──► dst-admin (FastAPI) ──libpod REST over podman.sock──► start/stop │
 │               │  HTTP :8081                                                 │
 │               ▼                                                             │
 │            dst-saves (Python) ──── boto3 ────► Cloudflare R2                 │
 │               │ poll: c_eval via FIFO, read server_log.txt                  │
 │               ▼                                                             │
 │            dst-server (cm2network/steamcmd, user steam)                     │
 │               Master :10999/udp   Caves :10998/udp                          │
 │                                                                             │
 │  bind mounts: data/saves data/parked data/backups data/mods                 │
 │  volumes:     dst-install (~2 GB)  steam-home  ctl (FIFOs + state.json)     │
 └─────────────────────────────────────────────────────────────────────────────┘
```

Every container runs as UID 1000 with `userns_mode: keep-id:uid=1000,gid=1000`, so all files under `data/` are owned by the host `dst` user and every container agrees on ownership. No `:U` flags on shared mounts.

Secrets are scoped per service with `environment:` interpolation from `.env` (compose loads `.env` for `${VAR}` substitution):

| Service | Receives |
| --- | --- |
| dst-server | `CLUSTER_NAME`, `CLUSTER_TOKEN`, `AUTO_UPDATE` |
| dst-saves | `CLUSTER_NAME`, `R2_*`, `R2_EVERY_DAYS`, `R2_ON_EMPTY`, `R2_KEEP`, `LOCAL_KEEP`, `AUTO_RESTORE` |
| dst-admin | `CLUSTER_NAME`, `ADMIN_USER`, `ADMIN_PASSWORD`, `DST_SAVES_URL`, `DST_CONTAINER` |

---

## 3. Repository layout

```
dst-server-hosting/
  README.md
  bootstrap.sh                  # curl | sudo bash — public, no secrets
  compose.yaml                  # production stack
  compose.local.yaml            # Mac overrides (platform, AUTO_UPDATE=0, VM socket path)
  .env.example
  dst-server/
    Dockerfile
    entrypoint.sh               # lifecycle (see §4)
    lib/dst.sh                  # shard discovery, FIFO helpers, stop-zip
  dst-saves/
    Dockerfile
    requirements.txt
    dstsaves/  __init__.py  main.py (FastAPI + loop)  poll.py  backup.py  r2.py  state.py
  dst-admin/
    Dockerfile
    requirements.txt
    dstadmin/  __init__.py  main.py  auth.py  podman.py  saves_client.py  cluster.py  wizard.py  mods.py  archive.py
    dstadmin/templates/  base.html  dashboard.html  clusters.html  wizard.html  mods.html  admins.html  console.html
    dstadmin/static/  pico.min.css  app.css  app.js
  scripts/
    backup.sh  restore.sh  console.sh  local-fetch-dst.sh
  tools/depotdownloader/Dockerfile
  docs/superpowers/specs/…  docs/superpowers/plans/…
  data/                         # gitignored: saves/ parked/ backups/ mods/
```

`.gitignore` excludes `.env`, `data/`, `*.zip`, `*.tar.gz`, `.claude/`, `.DS_Store`, `__MACOSX/`, Python caches.

---

## 4. dst-server

**Image.** `FROM docker.io/cm2network/steamcmd:latest@sha256:…` (digest resolved and pinned during implementation). As root: `apt-get install -y --no-install-recommends libcurl3-gnutls tini inotify-tools procps zip unzip ca-certificates`, copy `entrypoint.sh` + `lib/`, create `/data/klei /data/backups /ctl` owned by `steam`. `USER steam`. `ENTRYPOINT ["/usr/bin/tini","--","/usr/local/bin/entrypoint.sh"]`.

**Paths inside the container.**

| Path | Mount | Purpose |
| --- | --- | --- |
| `/home/steam/steamcmd` | image | steamcmd bootstrap (from base image) |
| `/opt/dst` | volume `dst-install` | DST install (`bin64/`, `mods/`, `ugc_mods/`) |
| `/home/steam/Steam` | volume `steam-home` | steam client cache |
| `/data/klei/DoNotStarveTogether` | bind `data/saves` | clusters; active one is `<CLUSTER_NAME>/` |
| `/data/mods` | bind `data/mods` | local (non-workshop) mods, one folder each |
| `/data/backups` | bind `data/backups` | local zips (stop zips written here) |
| `/ctl` | volume `ctl` | `<shard>.cmd` FIFOs, `state.json` |

**Lifecycle (`entrypoint.sh`).**

1. `steamcmd +force_install_dir /opt/dst +login anonymous +app_update 343050 validate +quit` when `AUTO_UPDATE=1` (default) or when `bin64/dontstarve_dedicated_server_nullrenderer_x64` is missing. Non-zero exit aborts the container (podman restarts it, log shows why).
2. Wait for cluster: until `<cluster>/cluster.ini` and at least one `<cluster>/<shard>/server.ini` exist. Uses `inotifywait -t 60` on the saves dir plus a 60 s heartbeat log line. Never generates a world.
3. `cluster_token.txt`: if missing or empty and `CLUSTER_TOKEN` set, write it (mode 0600).
4. Mods sync: collect every `workshop-<id>` from `<cluster>/*/modoverrides.lua`, write `/opt/dst/mods/dedicated_server_mods_setup.lua` with one `ServerModSetup("<id>")` per id (DST downloads them at boot). Copy each `/data/mods/<name>/` (must contain `modinfo.lua`) to `/opt/dst/mods/<name>/` (rsync-style replace).
5. Shard discovery: every subdirectory with `server.ini`. Master = the one with `is_master = true`. Launch order: Master first, then others.
6. Per shard: `mkfifo /ctl/<shard>.cmd`, open it O_RDWR on a dedicated fd (`exec {fd}<> fifo`) so the write side never sees EOF and the open never blocks, then `cd /opt/dst/bin64 && ./dontstarve_dedicated_server_nullrenderer_x64 -persistent_storage_root /data/klei -conf_dir DoNotStarveTogether -cluster "$CLUSTER_NAME" -shard "<shard>" < /ctl/<shard>.cmd &`. Shard stdout/stderr go to the container log prefixed with the shard name. Each shard also writes its own `<shard>/server_log.txt`.
7. `wait -n` on the shard PIDs. If any shard exits, soft-stop the others (`c_save()`, `c_shutdown(true)`, wait ≤60 s, TERM, KILL), write a stop zip (step 9), exit with the first non-zero code. `restart: unless-stopped` relaunches.
8. SIGTERM/SIGINT trap: for every shard in parallel: `c_save()` → 3 s → `c_shutdown(true)` → wait ≤60 s → TERM → 5 s → KILL. Compose `stop_grace_period: 150s` covers this.
9. Stop zip after all shards exited: `data/backups/<UTC>_day<NNNN>_stop.zip` (day from `/ctl/state.json` if present, otherwise `<UTC>_stop.zip`). Same exclusions as dst-saves (`*/backup/`, `.DS_Store`, `__MACOSX`). Does not upload to R2.
10. Exit 0.

Console commands can be sent by any container that mounts `ctl`: `echo 'c_announce("hi")' > /ctl/Master.cmd`.

**Env.** `CLUSTER_NAME` (required), `CLUSTER_TOKEN` (optional if file exists), `AUTO_UPDATE` (default 1).

---

## 5. dst-saves

Python 3.13 slim image, `boto3`, `fastapi`, `uvicorn`. Runs as UID 1000. Mounts `data/saves` (rw), `data/backups` (rw), `data/parked` (rw), volume `ctl` (rw). Listens on `:8081`; published as `127.0.0.1:8081:8081` on the host so scripts can reach it; dst-admin reaches it as `http://dst-saves:8081`.

**Startup.** If `AUTO_RESTORE=1` (default) and the active cluster dir has no `cluster.ini`: list R2, download the newest `*.zip` into `data/parked/`, extract it into `data/saves/<CLUSTER_NAME>/` (top-level folder renamed to `CLUSTER_NAME`, junk stripped). If R2 has nothing, log "no backup in R2, waiting for operator" and continue. dst-server's inotify wait picks the files up.

**Poll loop (every 30 s).** For every `/ctl/<shard>.cmd` FIFO: open `O_WRONLY|O_NONBLOCK` (ENXIO means the shard is not running → skip), write

```
c_eval([[print("DSTSAVES <nonce> cycles=" .. tostring(TheWorld and TheWorld.state.cycles or -1) .. " players=" .. tostring(#AllPlayers))]])
```

then for up to 6 s read the tail of `<shard>/server_log.txt` for `DSTSAVES <nonce> cycles=N players=M`. `cycles` is taken from Master, `players` is summed across shards. Result written to `/ctl/state.json` (`ts, cycles, players, shards{name: {alive, players}}`) and kept in memory.

**Triggers.**

| Trigger | Condition | Local zip | R2 upload |
| --- | --- | --- | --- |
| `day` | Master cycles increased vs last poll | yes | only if `cycles - last_r2_cycles >= R2_EVERY_DAYS` |
| `empty` | players went from >0 to 0 | yes | if `R2_ON_EMPTY=1` |
| `manual` | `POST /backup?tag=manual` (admin button, `scripts/backup.sh`) | yes | always |
| `pre-activate` | `POST /backup?tag=pre-activate` (admin activate flow) | yes | always, never pruned |
| `stop` | written by dst-server on shutdown | (written by dst-server) | no |

First successful poll only records state (no backup). `last_r2_cycles` and the last poll state persist in `data/backups/state.json` so restarts do not re-trigger uploads.

**Backup procedure.** Send `c_save()` to every live shard; wait until no file under `<cluster>/*/save/` was modified in the last 3 s (max 30 s); zip `data/saves/<CLUSTER_NAME>` with Python `zipfile` (deflate) excluding `*/backup/`, `.DS_Store`, `__MACOSX`; write to `data/backups/<UTC yyyymmddThhmmssZ>_day<NNNN>_<trigger>.zip`; upload to R2 key `clusters/<CLUSTER_NAME>/backups/<same name>` when the policy says so; then prune.

**Retention.** Local: keep newest `LOCAL_KEEP` (30) zips by name, all tags. R2: keep newest `R2_KEEP` (30) whose tag is `day`/`empty`; `manual` and `pre-activate` are never deleted.

**API (JSON, no auth — reachable only from the compose network and host loopback).**

| Route | Purpose |
| --- | --- |
| `GET /status` | last poll state, last backup, last R2 upload, policy values |
| `GET /backups` | `{local: [...], r2: [...]}` each with name, size, tag, day, ts |
| `POST /backup?tag=manual\|pre-activate` | run backup now, returns name + whether uploaded |
| `POST /upload/{name}` | upload an existing local zip to R2 (promote) |
| `POST /restore?source=r2\|local&name=…` | copy/download zip into `data/parked/`; never touches the active cluster |
| `DELETE /backups/{source}/{name}` | delete one backup |

**SIGTERM.** Finish an in-flight upload (bounded by `stop_grace_period: 60s`), then exit.

**Env.** `CLUSTER_NAME`, `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (all required; missing → exit 1 with the list of missing names), `R2_EVERY_DAYS=5`, `R2_ON_EMPTY=1`, `R2_KEEP=30`, `LOCAL_KEEP=30`, `AUTO_RESTORE=1`, `POLL_SECONDS=30`.

R2 client: `boto3.client("s3", endpoint_url="https://<account>.r2.cloudflarestorage.com", region_name="auto")`. Listing uses `list_objects_v2` with the `clusters/<name>/backups/` prefix. No bucket-existence probe (scoped tokens lack that permission).

---

## 6. dst-admin

Python 3.13 slim, `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `httpx`. UID 1000. Mounts `data/saves`, `data/parked`, `data/mods`, `data/backups` (rw), volume `ctl` (rw), and the host podman socket at `/run/podman/podman.sock`. Port `8080`.

**Auth.** HTTP Basic on every route (including static). `ADMIN_USER` defaults to `dst`. If `ADMIN_PASSWORD` is unset or empty, every request returns 500 with body `ADMIN_PASSWORD is not set`. Comparison via `secrets.compare_digest`.

**Podman control.** libpod REST over the unix socket with httpx (`transport=httpx.HTTPTransport(uds=...)`): container inspect (state, started-at), start, `stop?timeout=150`, `restart?t=150`, logs tail. Container name from `DST_CONTAINER` (default `dst-server`). Errors from the socket propagate as 502 with the podman message.

**Pages.**

| Page | Content / actions |
| --- | --- |
| Dashboard `/` | dst-server state + uptime, per-shard alive/players, in-game day, last backup, last R2 upload. Buttons: Start, Stop (graceful), Restart, Backup now (manual → R2). Log tails: container log, Master `server_log.txt`, Caves `server_log.txt` (last 200 lines), refreshed every 5 s via `/api/status` and `/api/logs/{source}`. |
| Clusters `/clusters` | Active cluster summary (name, shards, day, mods count). Parked list: upload zip (multipart), activate, delete, download. Backups: local + R2 lists from dst-saves with restore-to-parked, upload-to-R2 (local only), delete. |
| Wizard `/wizard` | Form prefilled from live `cluster.ini` via `read_active_cluster_settings()` (configparser) or `WIZARD_DEFAULTS` when none. Fields: display name, description, password, max players, game mode, pvp, intention, pause when empty, vote enabled, caves enabled, max snapshots. Submit on an existing cluster rewrites `cluster.ini` only. Submit with no active cluster creates `cluster.ini`, `Master/server.ini`, optional `Caves/server.ini` + `Caves/worldgenoverride.lua` (`preset = "DST_CAVE"`), empty `modoverrides.lua` per shard, `adminlist.txt`, `cluster_token.txt` from env. A parse error in the existing `cluster.ini` renders a banner and disables submit. |
| Mods `/mods` | Per-shard workshop list parsed from `modoverrides.lua` (id, enabled, link). Add by workshop id or URL (appends `["workshop-<id>"]={enabled=true, configuration_options={}}` to every shard). Remove. Raw editor per shard. Local mods: list `data/mods/*`, upload zip (must contain `modinfo.lua` at top or one level down), delete. "Save" and "Save and restart" buttons. |
| Admins `/admins` | Textarea for `adminlist.txt`; on save keep only lines matching `^KU_[A-Za-z0-9_-]+$`. |
| Console `/console` | Shard selector + command input; writes the line to `/ctl/<shard>.cmd` (nonblocking open; 409 if shard not running); shows the last 50 log lines after 2 s. |

**Activate flow** (`POST /clusters/activate?name=`): 1) stop dst-server gracefully (dst-server writes a stop zip), 2) `POST dst-saves /backup?tag=pre-activate` if an active cluster exists, 3) delete `data/saves/<CLUSTER_NAME>/`, 4) extract the parked zip there (strip `__MACOSX`/`.DS_Store`; if the archive has a single top-level folder, hoist it; reject path traversal), 5) validate `cluster.ini` + at least one `server.ini`, 6) start dst-server. Failures return 500 with the step name; nothing is retried silently.

**Park current** (`POST /clusters/park`): `POST dst-saves /backup?tag=manual` then copy the zip into `data/parked/`.

**UI.** Pico.css dark theme vendored in `static/`, one small `app.css`, `app.js` for polling and confirm dialogs. Status pills, cards, monospace log panes. No framework, no build.

---

## 7. Host data layout and volumes

```
/home/dst/dst-server-hosting/
  .env
  data/saves/<CLUSTER_NAME>/{cluster.ini, cluster_token.txt, adminlist.txt, Master/, Caves/}
  data/parked/*.zip
  data/backups/*.zip  + state.json
  data/mods/<modname>/modinfo.lua …
```

Compose volumes: `dst-install`, `steam-home`, `ctl`. Container names fixed: `dst-server`, `dst-saves`, `dst-admin`. All `restart: unless-stopped`. `dst-server` `depends_on: dst-saves` so a stack start restores from R2 before the server looks for a cluster, and a stack stop stops the server before dst-saves.

Ports: `10999/udp`, `10998/udp`, `27016-27018/udp`, `8766-8768/udp` on dst-server; `8080/tcp` on dst-admin; `127.0.0.1:8081/tcp` on dst-saves.

---

## 8. Backup naming and R2 layout

Zip name: `<UTC yyyymmddThhmmssZ>_day<NNNN>_<tag>.zip`, e.g. `20260918T203015Z_day0042_empty.zip`. `day` part omitted when unknown (`…_stop.zip`). Lexicographic order = chronological order. Zip root is the cluster folder (`<CLUSTER_NAME>/cluster.ini …`).

R2: `clusters/<CLUSTER_NAME>/backups/<zip name>`. Pre-existing objects under other prefixes are ignored.

---

## 9. Bootstrap (`bootstrap.sh`)

Run as root on a fresh Ubuntu 26.04/24.04 VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/moxxiq/dst-server-hosting/main/bootstrap.sh | sudo bash
```

Inputs: environment variables (`CLUSTER_NAME`, `CLUSTER_TOKEN`, `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD`, optional policy vars) or interactive prompts for any that are missing when stdin is a TTY. A Vultr Startup Script is the same one-liner preceded by `export VAR=…` lines. Secrets never enter the repo.

Steps, each idempotent (re-running reports "already done"):

1. `apt-get install -y podman uidmap slirp4netns passt fuse-overlayfs dbus-user-session pipx git curl unzip ufw jq`.
2. Create user `dst` (no password, no sudo), `loginctl enable-linger dst`.
3. As `dst`: `pipx install podman-compose==1.6.0`; `systemctl --user enable --now podman.socket podman-restart.service` (run with `XDG_RUNTIME_DIR=/run/user/<uid>`).
4. Clone or `git pull` the repo into `/home/dst/dst-server-hosting`.
5. Write `.env` (0600) with the collected values plus `PODMAN_SOCK=/run/user/<uid>/podman/podman.sock`. Existing `.env` is kept unless `--force-env`.
6. `podman compose build` and `podman compose up -d`.
7. `ufw allow` 22/tcp, 8080/tcp, 10999/udp, 10998/udp, 27016:27018/udp, 8766:8768/udp; `ufw --force enable`.
8. Print the admin URL, the admin user, and the follow-up commands (`podman compose logs -f dst-server`).

First boot with an empty `data/saves`: dst-saves restores the newest R2 zip if one exists; otherwise the server waits until the operator uploads a zip or runs the wizard in the panel.

README documents: restricting `:8080` to the operator's IP with the Vultr firewall (the panel speaks plain HTTP), updating (`git pull && podman compose up -d --build`), reboot recovery (`podman-restart.service`), and the `scripts/`.

---

## 10. Host scripts (`scripts/`)

| Script | Behaviour |
| --- | --- |
| `backup.sh [tag]` | `curl -fsS -X POST http://127.0.0.1:8081/backup?tag=<manual>`; prints the JSON result |
| `restore.sh [name\|latest] [--activate]` | no arg: lists R2 + local backups. With a name: `POST /restore` into `data/parked/`. `--activate` then calls the admin activate endpoint (asks for confirmation on a TTY). |
| `console.sh <shard> '<lua>'` | `podman exec dst-server sh -c 'echo "$1" > /ctl/$0.cmd'` |
| `local-fetch-dst.sh` | Mac only: builds `tools/depotdownloader` (Debian slim + DepotDownloader 3.4.0 for the host arch), runs it with `-app 343050 -os linux -osarch 64 -dir /opt/dst` into the `dst-install` volume. |

---

## 11. Local development on the Mac

1. `scripts/local-fetch-dst.sh` fills `dst-install` (DepotDownloader, anonymous login, native arm64).
2. `podman compose -f compose.yaml -f compose.local.yaml up -d --build`. The override sets `platform: linux/amd64` on dst-server, `AUTO_UPDATE=0`, and the VM socket path for dst-admin. Bind mounts stay under `./data/` (fallback to named volumes if virtiofs ownership misbehaves).
3. Open `http://localhost:8080`, upload `qkation-cooperative.zip`, activate.
4. Definition of done (local): both shards log `Sim paused`; `data/backups/` and R2 receive zips on the expected triggers; graceful stop writes a stop zip and both shards exit cleanly within the grace period; the operator joins from their DST client with `c_connect("127.0.0.1", 10999, "qka")` and mods load.

---

## 12. Error handling policy

- No silent `except: pass`, no default fallbacks that hide a broken config. Failures surface as container exit (server/saves) or 5xx with the underlying message (admin).
- Subprocess/HTTP wrappers return results; callers decide. Log lines go to stdout so `podman compose logs` is the single place to look.
- Archive extraction rejects path traversal and non-zip input with 400.
- dst-saves aborts at startup if any R2 variable is missing (prints the names).

---

## 13. Verification (no unit tests)

- `shellcheck` clean on every `.sh`; `podman compose -f compose.yaml config` renders; `python -m compileall` for both apps.
- Local smoke per §11. VPS smoke: bootstrap on a fresh VPS, `podman ps` shows three containers Up, panel reachable, upload + activate, shards live, R2 receives a manual backup, `sudo reboot` brings the stack back.

---

## 14. Out of scope / risks

Out of scope: HTTPS/reverse proxy, scheduled kicks or restarts, Lua hook mod, monitoring, multi-cluster hosting.

Risks: DST x64 under Rosetta is untested (fallback: VPS smoke only); gvproxy UDP forwarding for a local client join (fallback: verify join on the VPS); `keep-id` with virtiofs bind mounts on the Mac (fallback: named volumes in `compose.local.yaml`).
