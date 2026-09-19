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
