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
