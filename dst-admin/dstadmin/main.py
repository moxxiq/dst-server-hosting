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
