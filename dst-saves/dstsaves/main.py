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
        elif st.last_players > 0 and players == 0:
            # `empty` wins over `day` when both land in one poll: the last-player-left
            # snapshot is the one R2_ON_EMPTY promises to upload.
            trigger = "empty"
        elif cycles > st.last_cycles:
            trigger = "day"
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

    def upload(self, path: Path, *, advance_clock: bool) -> str:
        """Upload one local zip. Fresh backups of the live world (advance_clock=True) move
        the R2 day clock to the zip's own day; re-uploads of old zips leave it alone."""
        key = self.r2.upload(path)
        log.info("uploaded %s", key)
        if advance_clock:
            info = backup.parse_name(path.name)
            if info and info["day"] is not None:
                self.state.last_r2_cycles = info["day"]
        self.state.last_upload = {"name": path.name, "key": key, "at": now_iso()}
        save_state(self.state_path, self.state)
        pruned = self.r2.prune(self.s.r2_keep, protect=path.name)
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
                self.upload(out, advance_clock=True)
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
        key = s.upload(path, advance_clock=False)
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
