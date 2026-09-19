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
