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
