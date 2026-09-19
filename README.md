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

Known Rosetta quirk: the shards may segfault during their own shutdown teardown after the world has been saved — harmless locally (the stop zip is still written), not seen on x86_64 hosts.

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
